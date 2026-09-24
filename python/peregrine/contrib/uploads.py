"""Resumable uploads for any ASGI application.

    from fastapi import FastAPI
    from peregrine.contrib.uploads import FileUploadStore, ResumableUploads, UploadLimits

    api = FastAPI()
    store = FileUploadStore("/var/lib/app/uploads")

    async def finished(upload):
        shutil.move(upload.path, destination_for(upload))
        upload.remove()
        return 201, [(b"content-type", b"application/json")], b'{"stored": true}'

    app = ResumableUploads(api, "/files", store=store,
                           limits=UploadLimits(max_size=10 << 30),
                           on_complete=finished)

Serve `app`. It answers the upload routes itself and hands everything else to
`api` unchanged, lifespan included.

This is the IETF resumable upload protocol (draft-ietf-httpbis-resumable-
upload, interop version 9), the same one Garuda's GarudaUploads serves. An
upload to `/files` whose client speaks it -- it sends `Upload-Complete` -- is
told where the upload lives with a 104 before a byte of the body is read, and
everything that reaches the server is kept as it arrives. If the connection
drops, the client asks the upload's URL how much arrived (HEAD) and sends the
rest from there (PATCH, as `application/partial-upload`), as many times as it
takes, over any protocol and on any worker. A client that does not speak it
gets an ordinary upload. Either way `on_complete` is called once, with the
whole body on disk, and its answer is the response to the request that
finished the upload.

The routes, below wherever the application is mounted:

    POST    <path>            create, with some or all of the body
    OPTIONS <path>            the limits, as Upload-Limit
    HEAD    <uploads>/<id>    the offset, whether it is complete, the length
    GET     <uploads>/<id>    the same, and once it is complete, the answer
                              `on_complete` gave, for a client whose
                              connection died before it arrived
    PATCH   <uploads>/<id>    append at the offset the client names
    DELETE  <uploads>/<id>    cancel

What the draft leaves to the server, decided as Garuda decides it:

  * A request for an upload another request is still appending to. The older
    one is usually a connection that died without either side noticing, and
    the client has come back on a new one. When the older request is on the
    same event loop it is ended, what it received is kept, and the new request
    goes on -- a HEAD included, as the draft recommends, which is why a client
    must not ask for the offset while it is still sending. When it is on
    another worker, the new request waits a moment for it and is answered 409
    (an append) or 503 (HEAD, GET, DELETE) with Retry-After if it is still
    going: an offset still growing is one the next append would be refused
    at.
  * An `on_complete` that raises. The upload stays complete and the client is
    answered 500, as the draft's own example of a failed completion is: every
    byte arrived, so there is nothing left to resume, and HEAD says so. The
    bytes stay in the store until `max_age`, for the application to reconcile.
  * An answer that never arrived. The answer to the request that completed an
    upload is remembered as it is sent, and given again to a GET of the
    upload's URL until `max_age`. It outlives the bytes, so a handler that
    files them away and removes the upload still answers the client that lost
    its answer.
  * What the application knows about an upload. The request that creates one
    and the request that completes it are different requests, so `on_create`
    is given the creating request's scope, and the dict of strings it returns
    is kept on the upload and handed to `on_complete` as `metadata`. The
    server writes it and no client can reach it, which is what makes it the
    place for whose upload this is. Raising `Refused` from it turns the upload
    away before anything is written.
  * Progress: a 104 with the current offset every `progress_interval` bytes.
  * Expiry: uploads older than `max_age` are removed, complete or not, when
    the next one is created. `on_complete` should move a finished upload's
    bytes to where they belong.

Interim responses need a server that sends them. Under Peregrine that is
`http.response.informational`; under a server without it the uploads still
work, and a client cut off during the request that created an upload simply
has no URL to resume, and starts again.

Writes are ordinary blocking system calls on the event loop's thread: for a
local disk that is the same trade as serving a static file.
"""

import asyncio
import base64
import dataclasses
import fcntl
import hashlib
import inspect
import json
import logging
import os
import secrets
import threading
import time

__all__ = [
    "ResumableUploads",
    "FileUploadStore",
    "UploadLimits",
    "UploadInfo",
    "CompletedUpload",
    "Refused",
    "UPLOAD_DRAFT_INTEROP_VERSION",
]

#: The draft iteration this implements (its Appendix B).
UPLOAD_DRAFT_INTEROP_VERSION = 9

_LOG = logging.getLogger("peregrine.uploads")

_MISMATCHING_OFFSET = "https://iana.org/assignments/http-problem-types#mismatching-upload-offset"
_INCONSISTENT_LENGTH = "https://iana.org/assignments/http-problem-types#inconsistent-upload-length"
# Not one of the draft's: bytes that are not what the client's digest said.
_MISMATCHING_DIGEST = "https://garuda.dev/problems/mismatching-digest"

# The most of a completed upload's answer that is kept to be given again.
_REMEMBERED_LIMIT = 1 << 20


class Refused(Exception):
    """Raised from `on_create` to turn an upload away before anything is
    written, answered with `status` and `body`."""

    def __init__(self, status=403, body=b""):
        super().__init__(status, body)
        self.status = status
        self.body = body.encode() if isinstance(body, str) else body


@dataclasses.dataclass(frozen=True)
class UploadLimits:
    """The limits a server advertises and enforces, as `Upload-Limit`.

    `min_append_size` is the least an *append* may carry, except the one that
    completes the upload: what stops a client resuming a gigabyte a byte at a
    time. Creating does not have to meet it. `max_age` is how long an upload
    is kept, in seconds, from when it was created.
    """

    max_size: "int | None" = None
    min_size: "int | None" = None
    max_append_size: "int | None" = None
    min_append_size: "int | None" = None
    max_age: int = 24 * 3600

    def __post_init__(self):
        if self.max_size is not None and self.min_size is not None \
                and self.max_size < self.min_size:
            raise ValueError("an upload cannot be both larger than max_size "
                             "and smaller than min_size")
        if self.max_append_size is not None and self.min_append_size is not None \
                and self.max_append_size < self.min_append_size:
            raise ValueError("a request cannot carry both more than max_append_size "
                             "and less than min_append_size")

    def field(self, remaining):
        """The Upload-Limit field, in the order the draft's Appendix A lists
        its members."""
        members = []
        for name, value in (("max-size", self.max_size), ("min-size", self.min_size),
                            ("max-append-size", self.max_append_size),
                            ("min-append-size", self.min_append_size)):
            if value is not None:
                members.append("%s=%d" % (name, value))
        members.append("max-age=%d" % max(0, remaining))
        return ", ".join(members)


# -------------------------------------------------------------------- fields
#
# Structured fields (RFC 9651), parsed strictly: a field that is not exactly
# what is expected is malformed and answered 400 rather than guessed at. An
# offset read generously is data written in the wrong place.


def _sf_boolean(text):
    value = text.strip(" \t")
    return {"?1": True, "?0": False}.get(value)


def _sf_integer(text):
    """A non-negative sf-integer: at most 15 digits, no sign, no fraction."""
    digits = text.strip(" \t")
    if not digits or len(digits) > 15 or not all("0" <= c <= "9" for c in digits):
        return None
    return int(digits)


def _sha256_of_field(text):
    """The SHA-256 a `Content-Digest` or `Repr-Digest` field names, or None
    when it names none this understands."""
    for member in text.split(","):
        name, sep, value = member.partition("=")
        if not sep or name.strip().lower() != "sha-256":
            continue
        value = value.strip()
        # A byte sequence is wrapped in colons; anything else is not a digest
        # to guess at.
        if len(value) <= 2 or value[0] != ":" or value[-1] != ":":
            return None
        try:
            digest = base64.b64decode(value[1:-1], validate=True)
        except ValueError:
            return None
        return digest if len(digest) == 32 else None
    return None


def _digest_field(digest):
    return "sha-256=:%s:" % base64.b64encode(digest).decode()


def _wants_sha256(text):
    """Whether `Want-Repr-Digest` asks for SHA-256. A preference of 0 is a
    client saying it does not want one."""
    for member in text.split(","):
        name, sep, value = member.partition("=")
        if name.strip().lower() != "sha-256":
            continue
        return not (sep and value.strip() == "0")
    return False


def _sha256_of_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(256 * 1024), b""):
                h.update(block)
    except OSError:
        return None
    return h.digest()


async def _hash_file(path):
    """`_sha256_of_file` on a thread. An upload can be gigabytes, and hashing
    it on the event loop would stall every other request the loop serves for
    as long as that takes; hashlib lets go of the GIL while it works."""
    return await asyncio.get_running_loop().run_in_executor(None, _sha256_of_file, path)


# --------------------------------------------------------------------- store


@dataclasses.dataclass
class UploadInfo:
    """What is known about one upload."""

    id: str
    #: Bytes received and stored.
    offset: int
    #: The total the client declared with `Upload-Length`, once it has.
    length: "int | None"
    #: The client sent the last of it, and the upload is whole.
    complete: bool
    #: Seconds since the epoch.
    created_at: int
    #: `Content-Type` and `Content-Disposition` from the request that created
    #: it, when it sent them. Whatever the client said: not to be trusted.
    content_type: "str | None" = None
    content_disposition: "str | None" = None
    #: The `Repr-Digest` the client declared for the whole upload.
    repr_digest: "str | None" = None
    #: What `on_create` recorded. Written by the server, never by a client.
    metadata: "dict | None" = None


@dataclasses.dataclass
class _Answer:
    status: int
    content_type: "str | None"
    location: "str | None"
    body: bytes
    created_at: int


class _Handle:
    """An upload held for appending. Released when the request is done."""

    def __init__(self, upload_id, fd, offset):
        self.id = upload_id
        self._fd = fd
        self.offset = offset

    def append(self, data):
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]
        self.offset += len(data)

    def truncate(self, offset):
        """Drops everything past `offset`, for a request whose bytes were not
        what its digest said. What was stored before it began is untouched."""
        os.ftruncate(self._fd, offset)
        self.offset = offset

    def release(self):
        if self._fd >= 0:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = -1

    # The requests release their handles in `finally`. This is only for one
    # that leaks anyway: the descriptor is a plain int, and without it the
    # lock would be held until the process exits.
    __del__ = release


class FileUploadStore:
    """Where uploads are kept while they are in progress.

    Workers are separate processes, and a client that resumes after a dropped
    connection is as likely to reach a different worker as the same one, so
    nothing about an upload lives in memory. Each is files in `directory`:

        <id>.data   the bytes received so far; its size is the offset
        <id>.info   the declared length, whether it is complete, when it was
                    created, and what was recorded with it
        <id>.done   what the completed upload was answered with, kept after
                    the handler has taken the bytes away

    The offset is the data file's size rather than a number written beside
    it, so a worker that dies part-way through an append leaves an upload
    whose offset is exactly what reached the disk. Only one request appends
    to an upload at a time, across processes and threads: appending takes an
    exclusive flock on the data file.
    """

    def __init__(self, directory):
        self.directory = directory.rstrip("/") or "/"
        os.makedirs(self.directory, mode=0o700, exist_ok=True)

    def data_path(self, upload_id):
        return os.path.join(self.directory, upload_id + ".data")

    def _info_path(self, upload_id):
        return os.path.join(self.directory, upload_id + ".info")

    def _done_path(self, upload_id):
        return os.path.join(self.directory, upload_id + ".done")

    @staticmethod
    def _valid(upload_id):
        return len(upload_id) == 32 and all(c in "0123456789abcdef" for c in upload_id)

    def create(self, length=None, content_type=None, content_disposition=None,
               repr_digest=None, metadata=None):
        """Starts an upload with nothing in it."""
        upload_id = secrets.token_hex(16)
        fd = os.open(self.data_path(upload_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        info = UploadInfo(id=upload_id, offset=0, length=length, complete=False,
                          created_at=int(time.time()), content_type=content_type,
                          content_disposition=content_disposition,
                          repr_digest=repr_digest, metadata=metadata or None)
        self._save(info)
        return info

    def info(self, upload_id):
        """The upload's state, with the offset read from the disk, or None for
        an id that is not one of this store's."""
        if not self._valid(upload_id):
            return None
        raw = self._read(self._info_path(upload_id))
        if raw is None:
            return None
        try:
            fields = json.loads(raw)
            info = UploadInfo(**fields)
            info.offset = os.stat(self.data_path(upload_id)).st_size
        except (ValueError, TypeError, OSError):
            return None
        return info

    def acquire(self, upload_id):
        """Takes the upload for appending, or None while another request has
        it. Raises FileNotFoundError for an upload that does not exist."""
        if not self._valid(upload_id):
            raise FileNotFoundError(upload_id)
        fd = os.open(self.data_path(upload_id), os.O_WRONLY | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return _Handle(upload_id, fd, os.fstat(fd).st_size)

    def busy(self, upload_id):
        """Whether a request, in this process or another, is appending."""
        if not self._valid(upload_id):
            return False
        try:
            fd = os.open(self.data_path(upload_id), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def update(self, upload_id, length, complete):
        """Records the declared length, or that the upload is whole."""
        info = self.info(upload_id)
        if info is None:
            raise FileNotFoundError(upload_id)
        info.length = length
        info.complete = complete
        self._save(info)

    def delete(self, upload_id):
        """Removes an upload and its bytes, keeping the answer it was given:
        the handler moving a finished upload's bytes elsewhere should not take
        away what a client is still owed."""
        if not self._valid(upload_id):
            raise FileNotFoundError(upload_id)
        try:
            os.unlink(self._info_path(upload_id))
        finally:
            try:
                os.unlink(self.data_path(upload_id))
            except FileNotFoundError:
                pass

    def forget(self, upload_id):
        """Removes an upload and everything remembered about it, its answer
        included: what a client asking for it to be gone means."""
        try:
            self.delete(upload_id)
        finally:
            if self._valid(upload_id):
                try:
                    os.unlink(self._done_path(upload_id))
                except FileNotFoundError:
                    pass

    def remember(self, upload_id, status, content_type, location, body, created_at):
        """Keeps what the completed upload was answered with. A header value
        cannot hold a newline, so the head needs no escaping."""
        head = "2\n%d %d %s\n%s\n" % (status, created_at, content_type or "", location or "")
        self._write(self._done_path(upload_id), head.encode("latin-1") + body)

    def answer(self, upload_id):
        """The answer a completed upload was given, or None."""
        if not self._valid(upload_id):
            return None
        raw = self._read(self._done_path(upload_id))
        if raw is None:
            return None
        parts = raw.split(b"\n", 3)
        if len(parts) != 4 or parts[0] != b"2":
            return None
        numbers = parts[1].decode("latin-1").split(" ", 2)
        if len(numbers) != 3 or not numbers[0].isdigit() or not numbers[1].isdigit():
            return None
        location = parts[2].decode("latin-1")
        return _Answer(status=int(numbers[0]), content_type=numbers[2] or None,
                       location=location or None, body=parts[3],
                       created_at=int(numbers[1]))

    def remove_expired(self, older_than):
        """Removes every upload created more than `older_than` seconds ago,
        and every remembered answer as old. Returns how many uploads went."""
        try:
            names = os.listdir(self.directory)
        except OSError:
            return 0
        cutoff = int(time.time()) - older_than
        ids = {n[:-5] for n in names if n.endswith(".info")}
        removed = 0
        for upload_id in ids:
            info = self.info(upload_id)
            if info is not None and info.created_at < cutoff:
                try:
                    self.forget(upload_id)
                    removed += 1
                except OSError:
                    pass
        # An upload whose handler removed its bytes leaves only the answer,
        # which expires by its own age.
        for name in names:
            upload_id = name[:-5]
            if name.endswith(".done") and upload_id not in ids:
                answer = self.answer(upload_id)
                if answer is not None and answer.created_at < cutoff:
                    try:
                        os.unlink(self._done_path(upload_id))
                    except OSError:
                        pass
            # What a process that died part-way leaves: bytes whose record was
            # never written or already removed, and a record written aside and
            # never renamed into place. Nothing refers to either, so they go
            # once they are as old as an upload would be.
            elif (name.endswith(".data") and upload_id not in ids) or name.endswith(".tmp"):
                path = os.path.join(self.directory, name)
                try:
                    if os.stat(path).st_mtime < cutoff:
                        os.unlink(path)
                except OSError:
                    pass
        return removed

    def _save(self, info):
        self._write(self._info_path(info.id),
                    json.dumps(dataclasses.asdict(info)).encode())

    def _write(self, path, data):
        """Written aside and renamed over, so a reader never sees half."""
        temporary = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(temporary, path)

    @staticmethod
    def _read(path):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None


@dataclasses.dataclass(frozen=True)
class CompletedUpload:
    """An upload that has all of its bytes, as `on_complete` is given it."""

    info: UploadInfo
    store: FileUploadStore
    #: The SHA-256 of the bytes, when it was computed already -- because the
    #: client declared one that was checked, or asked to be told it.
    sha256: "bytes | None" = None

    @property
    def path(self):
        """Where the bytes are."""
        return self.store.data_path(self.info.id)

    @property
    def length(self):
        return self.info.offset

    @property
    def metadata(self):
        """What `on_create` recorded. Unlike `info.content_type` and
        `info.content_disposition`, which are whatever the client sent, this
        came from the server: whose upload this is belongs here."""
        return dict(self.info.metadata or {})

    def digest(self):
        """The SHA-256 of the bytes, read from the file when it has not been
        computed already. None only if the file cannot be read."""
        return self.sha256 if self.sha256 is not None else _sha256_of_file(self.path)

    def remove(self):
        """Removes the upload and its bytes, once they have been moved."""
        self.store.delete(self.info.id)


# ------------------------------------------------------------------ requests


class _Request:
    """What the handlers need of one request: its fields and its body."""

    def __init__(self, scope, receive, send):
        self.scope = scope
        self.receive = receive
        self._send = send
        self.method = scope["method"]
        self.headers = {}
        for name, value in scope.get("headers") or ():
            key = name.decode("latin-1").lower()
            text = value.decode("latin-1")
            self.headers[key] = self.headers[key] + ", " + text if key in self.headers else text
        length = _sf_integer(self.headers.get("content-length", ""))
        self.expected_length = length
        self.informational = "http.response.informational" in (scope.get("extensions") or {})

    def header(self, name):
        return self.headers.get(name)

    def speaks_draft(self):
        text = self.header("upload-draft-interop-version")
        return text is not None and _sf_integer(text) == UPLOAD_DRAFT_INTEROP_VERSION

    async def interim(self, fields):
        """A 104, when the server can send one."""
        if not self.informational:
            return
        await self._send({"type": "http.response.informational", "status": 104,
                          "headers": [(k.encode("latin-1"), v.encode("latin-1"))
                                      for k, v in fields]})

    async def respond(self, status, headers=(), body=b""):
        encoded = [(k.encode("latin-1") if isinstance(k, str) else k,
                    v.encode("latin-1") if isinstance(v, str) else v) for k, v in headers]
        if isinstance(body, str):
            body = body.encode()
        if body and not any(k.lower() == b"content-type" for k, _ in encoded):
            encoded.append((b"content-type", b"text/plain; charset=utf-8"))
        await self._send({"type": "http.response.start", "status": status,
                          "headers": encoded})
        await self._send({"type": "http.response.body",
                          "body": b"" if self.method == "HEAD" else body})


class _Disconnected(Exception):
    """The client went away part-way through the body."""


class _Service:
    """The upload routes' shared state. One per application, shared by the
    event loops of a --free-threaded server, so what is touched from more than
    one of them is locked."""

    def __init__(self, store, limits, progress_interval, on_create, on_complete):
        self.store = store
        self.limits = limits
        self.progress_interval = progress_interval
        self.on_create = on_create
        self.on_complete = on_complete
        self._last_expiry = 0
        self._lock = threading.Lock()
        #: The task appending to each upload, by upload id, with its loop.
        self._appending = {}

    # --- who is appending

    def _claim(self, upload_id):
        task = asyncio.current_task()
        with self._lock:
            self._appending[upload_id] = (asyncio.get_running_loop(), task)
        return task

    def _unclaim(self, upload_id, task):
        with self._lock:
            held = self._appending.get(upload_id)
            if held is not None and held[1] is task:
                del self._appending[upload_id]

    def _older(self, upload_id):
        with self._lock:
            held = self._appending.get(upload_id)
        if held is None or held[0] is not asyncio.get_running_loop():
            return None
        return held[1]

    async def supersede(self, upload_id):
        """Ends a request on this event loop still appending to the upload,
        and waits for it to have stored what it received and let go. One on
        another worker is left to the lock."""
        older = self._older(upload_id)
        if older is None or older is asyncio.current_task():
            return
        older.cancel()
        for _ in range(200):
            if self._older(upload_id) is not older:
                return
            await asyncio.sleep(0.005)

    async def acquire(self, upload_id):
        await self.supersede(upload_id)
        for attempt in range(20):
            handle = self.store.acquire(upload_id)
            if handle is not None:
                return handle
            if attempt < 19:
                await asyncio.sleep(0.025)
        return None

    def live(self, upload_id):
        """The upload, unless it does not exist or has outlived `max_age`."""
        info = self.store.info(upload_id)
        if info is None:
            return None
        if int(time.time()) - info.created_at > self.limits.max_age:
            try:
                self.store.forget(upload_id)
            except OSError:
                pass
            return None
        return info

    # --- answers

    async def too_large(self, request):
        await request.respond(413, [("Upload-Limit", self.limits.field(self.limits.max_age))])

    async def too_small(self, request, why, headers=()):
        # HTTP has no opposite of 413, so this is a 400 carrying Upload-Limit:
        # the client sent what the limits had already said it should not.
        await request.respond(400, list(headers) + [
            ("Upload-Limit", self.limits.field(self.limits.max_age))], why)

    async def problem(self, request, status, kind, title, members=(), headers=()):
        document = {"type": kind, "title": title}
        document.update(members)
        await request.respond(status, list(headers) + [
            ("Content-Type", "application/problem+json")], json.dumps(document))

    def digest_of(self, request):
        """The SHA-256 this request says its own bytes are; None when it says
        nothing. ValueError when it says something this cannot check, which
        is not the same as saying nothing."""
        field = request.header("content-digest")
        if field is None:
            return None
        digest = _sha256_of_field(field)
        if digest is None:
            raise ValueError(field)
        return digest

    # --- creation

    async def create(self, request, location_prefix):
        resumable = False
        complete = True
        text = request.header("upload-complete")
        if text is not None:
            value = _sf_boolean(text)
            if value is None:
                return await request.respond(400, (), "Upload-Complete is not a Boolean")
            resumable, complete = True, value
        length = None
        text = request.header("upload-length")
        if text is not None:
            length = _sf_integer(text)
            if length is None:
                return await request.respond(400, (), "Upload-Length is not an Integer")
        declared = request.expected_length
        if complete and declared is not None:
            if length is not None and length != declared:
                return await self.problem(request, 400, _INCONSISTENT_LENGTH,
                                          "the upload's length does not match the request's")
            length = declared
        limits = self.limits
        if limits.max_size is not None and length is not None and length > limits.max_size:
            return await self.too_large(request)
        if limits.min_size is not None and length is not None and length < limits.min_size:
            return await self.too_small(request, "the upload is shorter than min-size")
        if limits.max_append_size is not None and declared is not None \
                and declared > limits.max_append_size:
            return await self.too_large(request)
        try:
            content_digest = self.digest_of(request)
        except ValueError:
            return await request.respond(400, (),
                                         "Content-Digest names no digest this server checks")
        now = int(time.time())
        if now - self._last_expiry >= 60:
            self._last_expiry = now
            self.store.remove_expired(limits.max_age)
        repr_digest = request.header("repr-digest")
        if repr_digest is not None and _sha256_of_field(repr_digest) is None:
            return await request.respond(400, (),
                                         "Repr-Digest names no digest this server checks")
        # Before anything is on disk: a hook that refuses this request leaves
        # nothing behind to wait out max_age.
        metadata = {}
        if self.on_create is not None:
            try:
                metadata = self.on_create(request.scope)
                if inspect.isawaitable(metadata):
                    metadata = await metadata
            except Refused as refusal:
                return await request.respond(refusal.status, (), refusal.body)
            metadata = {str(k): str(v) for k, v in (metadata or {}).items()}
        info = self.store.create(length=length, content_type=request.header("content-type"),
                                 content_disposition=request.header("content-disposition"),
                                 repr_digest=repr_digest, metadata=metadata)
        handle = self.store.acquire(info.id)
        if handle is None:
            return await request.respond(503)
        # Released however this ends, the 104 included: a send can raise, and
        # a lock left held is an upload nobody can resume.
        try:
            interim = resumable and request.speaks_draft()
            url = "%s/%s" % (location_prefix, info.id)
            if interim:
                await request.interim([
                    ("Upload-Draft-Interop-Version", str(UPLOAD_DRAFT_INTEROP_VERSION)),
                    ("Location", url),
                    ("Upload-Limit", limits.field(limits.max_age)),
                ])
            await self.transfer(request, handle, complete=complete, length=length,
                                resumable=resumable, interim=interim, creating=True, url=url,
                                content_digest=content_digest)
        finally:
            handle.release()

    # --- appending

    async def append(self, request, upload_id):
        media = (request.header("content-type") or "").split(";")[0].strip().lower()
        if media != "application/partial-upload":
            return await request.respond(415, (), "an append is application/partial-upload")
        offset = _sf_integer(request.header("upload-offset") or "")
        if offset is None:
            return await request.respond(400, (), "Upload-Offset is missing or not an Integer")
        complete = _sf_boolean(request.header("upload-complete") or "")
        if complete is None:
            return await request.respond(400, (), "Upload-Complete is missing or not a Boolean")
        info = self.live(upload_id)
        if info is None:
            return await request.respond(404)
        if info.complete:
            return await self.already_complete(request, info.offset, offset)
        length = info.length
        text = request.header("upload-length")
        if text is not None:
            value = _sf_integer(text)
            if value is None:
                return await request.respond(400, (), "Upload-Length is not an Integer")
            if length is not None and length != value:
                return await self.problem(request, 400, _INCONSISTENT_LENGTH,
                                          "the upload's length changed")
            length = value
        declared = request.expected_length
        if complete and declared is not None:
            if length is not None and length != offset + declared:
                return await self.problem(
                    request, 400, _INCONSISTENT_LENGTH,
                    "the upload's length does not match what this request completes it with")
            length = offset + declared
        limits = self.limits
        if limits.max_size is not None and length is not None and length > limits.max_size:
            return await self.too_large(request)
        if limits.min_size is not None and length is not None and length < limits.min_size:
            return await self.too_small(request, "the upload is shorter than min-size")
        if limits.max_append_size is not None and declared is not None \
                and declared > limits.max_append_size:
            return await self.too_large(request)
        # Not the request that completes the upload: that one may be as short
        # as the upload's last bytes are.
        if limits.min_append_size is not None and not complete and declared is not None \
                and declared < limits.min_append_size:
            return await self.too_small(request, "this append is shorter than min-append-size")
        try:
            content_digest = self.digest_of(request)
        except ValueError:
            return await request.respond(400, (),
                                         "Content-Digest names no digest this server checks")
        try:
            handle = await self.acquire(upload_id)
        except FileNotFoundError:
            return await request.respond(404)
        if handle is None:
            return await request.respond(409, [("Retry-After", "1")],
                                         "another request is appending to this upload")
        # Released however this ends. The refusals below let go before they
        # answer, so the next request is not kept waiting on this one's send.
        try:
            await self._append_held(request, handle, upload_id, offset, complete, length,
                                    content_digest)
        finally:
            handle.release()

    async def _append_held(self, request, handle, upload_id, offset, complete, length,
                           content_digest):
        # Taking the lock can wait on a request that was still appending, and
        # what it did is not in the info read before the wait: it may have
        # completed the upload, or declared its length. Reading it again under
        # the lock is what makes the checks before it hold now.
        current = self.store.info(upload_id)
        if current is None:
            handle.release()
            return await request.respond(404)
        if current.complete:
            handle.release()
            return await self.already_complete(request, current.offset, offset)
        if handle.offset != offset:
            held = handle.offset
            handle.release()
            return await self.problem(
                request, 409, _MISMATCHING_OFFSET, "the offset does not match the upload's",
                {"expected-offset": held, "provided-offset": offset},
                [("Upload-Offset", str(held)), ("Upload-Complete", "?0")])
        # A length declared while this request waited is one it never saw, and
        # taking it is what holds these bytes to it.
        if current.length is not None:
            if length is not None and length != current.length:
                handle.release()
                return await self.problem(request, 400, _INCONSISTENT_LENGTH,
                                          "the upload's length changed")
            length = current.length
        elif length is not None:
            self.store.update(upload_id, length=length, complete=False)
        await self.transfer(request, handle, complete=complete, length=length,
                            resumable=True, interim=request.speaks_draft(), creating=False,
                            content_digest=content_digest)

    async def already_complete(self, request, stored, provided):
        await self.problem(request, 409, _MISMATCHING_OFFSET, "the upload is already complete",
                           {"expected-offset": stored, "provided-offset": provided},
                           [("Upload-Complete", "?1"), ("Upload-Offset", str(stored))])

    async def transfer(self, request, handle, complete, length, resumable, interim,
                       creating, content_digest, url=None):
        """Stores the body as it arrives, and answers once it has all come."""
        upload_id = handle.id
        task = self._claim(upload_id)
        start = handle.offset
        # Only when the client said what these bytes should be: hashing what
        # nobody will check is a pass over every byte for nothing.
        running = hashlib.sha256() if content_digest is not None else None
        limits = self.limits
        wants_digest = False
        sha256 = None
        try:
            try:
                since_progress = 0
                more = True
                while more:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        raise _Disconnected()
                    data = message.get("body", b"")
                    more = message.get("more_body", False)
                    if not data:
                        continue
                    if length is not None and handle.offset + len(data) > length:
                        return await self.problem(request, 400, _INCONSISTENT_LENGTH,
                                                  "the upload is longer than its length")
                    if limits.max_size is not None \
                            and handle.offset + len(data) > limits.max_size:
                        return await self.too_large(request)
                    if limits.max_append_size is not None \
                            and handle.offset - start + len(data) > limits.max_append_size:
                        return await self.too_large(request)
                    handle.append(data)
                    if running is not None:
                        running.update(data)
                    since_progress += len(data)
                    if interim and self.progress_interval > 0 \
                            and since_progress >= self.progress_interval:
                        since_progress = 0
                        await request.interim([
                            ("Upload-Draft-Interop-Version", str(UPLOAD_DRAFT_INTEROP_VERSION)),
                            ("Upload-Offset", str(handle.offset)),
                        ])
            except _Disconnected:
                # What arrived is stored, and is where the client resumes
                # from. There is nobody to answer. A request superseded by a
                # newer one for this upload is cancelled instead, and lets go
                # of it on the way out below.
                return

            # Bytes that are not what the client said were corrupted on the
            # way, so they are dropped and the upload stays where this request
            # began it -- the client sends them again from there.
            if running is not None and not secrets.compare_digest(running.digest(),
                                                                   content_digest):
                handle.truncate(start)
                return await self.problem(request, 400, _MISMATCHING_DIGEST,
                                          "the bytes are not what Content-Digest says",
                                          headers=[("Upload-Offset", str(start))])
            offset = handle.offset
            # A body of no declared length is only measured once it has all
            # come, so an append is held to min-append-size here. What came is
            # kept at the offset it reached.
            if not complete and not creating and limits.min_append_size is not None \
                    and offset - start < limits.min_append_size:
                return await self.too_small(request,
                                            "this append is shorter than min-append-size",
                                            [("Upload-Offset", str(offset))])
            if not complete:
                headers = [("Upload-Complete", "?0"), ("Upload-Offset", str(offset))]
                if creating:
                    if url is not None:
                        headers.append(("Location", url))
                    headers.append(("Upload-Limit", limits.field(limits.max_age)))
                    return await request.respond(201, headers)
                return await request.respond(204, headers)
            if length is not None and length != offset:
                return await self.problem(request, 400, _INCONSISTENT_LENGTH,
                                          "the upload ended short of its length")
            # Refused the completion, not deleted: a client that ended early
            # can send the rest or cancel.
            if limits.min_size is not None and offset < limits.min_size:
                return await self.too_small(request, "the upload is shorter than min-size",
                                            [("Upload-Offset", str(offset))])
            wants_digest = _wants_sha256(request.header("want-repr-digest") or "")
            stored = self.store.info(upload_id)
            declared = _sha256_of_field(stored.repr_digest) \
                if stored is not None and stored.repr_digest else None
            if declared is not None:
                actual = await _hash_file(self.store.data_path(upload_id))
                if actual is None or not secrets.compare_digest(actual, declared):
                    # Whole and wrong: appending cannot mend it, so it goes.
                    handle.release()
                    try:
                        self.store.delete(upload_id)
                    except OSError:
                        pass
                    return await self.problem(request, 400, _MISMATCHING_DIGEST,
                                              "the upload is not what Repr-Digest says")
                sha256 = actual
            elif wants_digest:
                sha256 = await _hash_file(self.store.data_path(upload_id))
            self.store.update(upload_id, length=offset, complete=True)
        finally:
            handle.release()
            self._unclaim(upload_id, task)

        info = self.store.info(upload_id)
        if info is None:
            return await request.respond(404)
        extra = []
        if wants_digest and sha256 is not None:
            extra.append(("Repr-Digest", _digest_field(sha256)))
        if resumable:
            extra.append(("Upload-Complete", "?1"))
        answer = await self.on_complete(CompletedUpload(info=info, store=self.store,
                                                        sha256=sha256))
        await self.deliver(request, answer, extra, info)

    async def deliver(self, request, answer, extra, info):
        """Sends what `on_complete` answered, remembering it on its way out,
        so a client whose connection dies before it arrives can ask again."""
        kept = {"status": 200, "type": None, "location": None, "body": [], "size": 0}
        send = request._send

        async def capture(message):
            kind = message.get("type")
            if kind == "http.response.start":
                kept["status"] = message["status"]
                headers = list(message.get("headers") or [])
                for name, value in headers:
                    lowered = name.lower() if isinstance(name, bytes) else name.encode().lower()
                    text = value.decode("latin-1") if isinstance(value, bytes) else value
                    if lowered == b"content-type":
                        kept["type"] = text
                    elif lowered == b"location":
                        kept["location"] = text
                message = dict(message, headers=headers + [
                    (k.encode("latin-1"), v.encode("latin-1")) for k, v in extra])
            elif kind == "http.response.body":
                chunk = message.get("body", b"")
                kept["size"] += len(chunk)
                if kept["size"] <= _REMEMBERED_LIMIT:
                    kept["body"].append(chunk)
            await send(message)

        if callable(answer):
            # An ASGI response object -- Starlette's, for one.
            await answer(request.scope, request.receive, capture)
        else:
            status, headers, body = _answer_parts(answer)
            request._send = capture
            await request.respond(status, headers, body)
        if kept["size"] > _REMEMBERED_LIMIT:
            _LOG.warning("the answer to upload %s is over %d bytes and is not remembered",
                         info.id, _REMEMBERED_LIMIT)
            return
        try:
            self.store.remember(info.id, kept["status"], kept["type"], kept["location"],
                                b"".join(kept["body"]), info.created_at)
        except OSError:
            _LOG.exception("could not remember the answer to upload %s", info.id)

    # --- offset, limits, cancellation

    async def settle(self, upload_id):
        """Waits for nothing to be appending to the upload: one request on
        this event loop is ended, as the draft recommends (section 4.6), and
        one on another worker is given a moment to finish. False if it is
        still going."""
        await self.supersede(upload_id)
        for attempt in range(20):
            if not self.store.busy(upload_id):
                return True
            if attempt < 19:
                await asyncio.sleep(0.025)
        return False

    async def offset(self, request, upload_id, replaying):
        no_store = [("Cache-Control", "no-store")]
        # An offset is only worth giving once nothing is still adding to it:
        # one that is still growing is one the next append would be refused
        # at, and the draft says an offset given must be one it accepts.
        if not await self.settle(upload_id):
            return await request.respond(503, no_store + [("Retry-After", "1")],
                                         "another request is appending to this upload")
        # A GET for an upload that finished is the client asking for the
        # answer it did not get. HEAD is left as the draft describes it.
        if replaying:
            answer = self.store.answer(upload_id)
            if answer is not None and int(time.time()) - answer.created_at <= self.limits.max_age:
                headers = list(no_store)
                if answer.content_type:
                    headers.append(("Content-Type", answer.content_type))
                if answer.location:
                    headers.append(("Location", answer.location))
                headers.append(("Upload-Complete", "?1"))
                return await request.respond(answer.status, headers, answer.body)
        info = self.live(upload_id)
        if info is None:
            return await request.respond(404, no_store)
        headers = no_store + [("Upload-Offset", str(info.offset)),
                              ("Upload-Complete", "?1" if info.complete else "?0")]
        if info.length is not None:
            headers.append(("Upload-Length", str(info.length)))
        remaining = info.created_at + self.limits.max_age - int(time.time())
        headers.append(("Upload-Limit", self.limits.field(remaining)))
        await request.respond(204, headers)

    async def options(self, request):
        headers = []
        if request.header("upload-complete") is None:
            headers.append(("Upload-Limit", self.limits.field(self.limits.max_age)))
        await request.respond(204, headers)

    async def cancel(self, request, upload_id):
        # Not while another worker is still writing into it: its bytes would
        # go on landing in a file nobody can reach, and its completion would
        # find the upload gone.
        if not await self.settle(upload_id):
            return await request.respond(503, [("Retry-After", "1")],
                                         "another request is appending to this upload")
        try:
            # Everything, the answer included: the client asked for this
            # upload to be gone.
            self.store.forget(upload_id)
        except FileNotFoundError:
            return await request.respond(404)
        await request.respond(204)


def _answer_parts(answer):
    """`on_complete`'s answer as (status, headers, body)."""
    if answer is None:
        return 204, [], b""
    if isinstance(answer, int):
        return answer, [], b""
    if isinstance(answer, (bytes, str)):
        return 200, [], answer
    if isinstance(answer, tuple):
        if len(answer) == 3:
            return answer
        if len(answer) == 2:
            return answer[0], [], answer[1]
    raise TypeError("on_complete must return None, a status, a body, (status, body), "
                    "(status, headers, body) or an ASGI response, not %r" % (answer,))


class ResumableUploads:
    """An ASGI application serving resumable uploads at `path`, and passing
    every other request to `app` unchanged.

    `uploads` is the path each upload's own URL is made under. Both it and
    `path` are within the application: under `--root-path /api` they are
    served at `/api/files` and `/api/uploads/<id>`, and the URLs the client
    is given say so.

    `on_complete(upload)` is called once each upload has all its bytes, with a
    `CompletedUpload`; it may be a coroutine function. What it returns is the
    response to the request that finished the upload: None (204), a status, a
    body, `(status, body)`, `(status, headers, body)`, or an ASGI response
    object such as Starlette's. It should move the bytes where they belong and
    call `upload.remove()`.

    `on_create(scope)` is called on the request that creates an upload, before
    anything is written, and returns a dict of strings to keep on it as
    `metadata`; raise `Refused` to turn it away.
    """

    def __init__(self, app, path, *, store, on_complete, limits=None, uploads="/uploads",
                 methods=("POST",), progress_interval=8 << 20, on_create=None):
        if not path.startswith("/") or not uploads.startswith("/"):
            raise ValueError("path and uploads are absolute: %r, %r" % (path, uploads))
        if not uploads.rstrip("/"):
            raise ValueError("uploads cannot be /: every path would be an upload")
        self.app = app
        self.path = path.rstrip("/") or "/"
        self.uploads = uploads.rstrip("/")
        self.methods = frozenset(m.upper() for m in methods)
        self._service = _Service(store, limits or UploadLimits(), progress_interval,
                                 on_create, _as_coroutine(on_complete))

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            handled = await self._dispatch(scope, receive, send)
            if handled:
                return
        if self.app is None:
            if scope["type"] == "lifespan":
                return await _answer_lifespan(receive, send)
            return await _not_found(scope, send)
        await self.app(scope, receive, send)

    async def _dispatch(self, scope, receive, send):
        path = scope["path"]
        method = scope["method"]
        service = self._service
        if (path.rstrip("/") or "/") == self.path:
            if method in self.methods:
                request = _Request(scope, receive, send)
                prefix = scope.get("root_path", "").rstrip("/") + self.uploads
                await service.create(request, prefix)
                return True
            if method == "OPTIONS":
                await service.options(_Request(scope, receive, send))
                return True
            return False
        prefix = self.uploads + "/"
        if not path.startswith(prefix):
            return False
        upload_id = path[len(prefix):]
        if "/" in upload_id or not upload_id:
            return False
        request = _Request(scope, receive, send)
        if method == "HEAD":
            await service.offset(request, upload_id, replaying=False)
        elif method == "GET":
            await service.offset(request, upload_id, replaying=True)
        elif method == "PATCH":
            await service.append(request, upload_id)
        elif method == "DELETE":
            await service.cancel(request, upload_id)
        else:
            await request.respond(405, [("Allow", "HEAD, GET, PATCH, DELETE")])
        return True


def _as_coroutine(function):
    if inspect.iscoroutinefunction(function):
        return function

    async def call(upload):
        result = function(upload)
        if inspect.isawaitable(result):
            result = await result
        return result
    return call


async def _answer_lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _not_found(scope, send):
    await send({"type": "http.response.start", "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
    await send({"type": "http.response.body", "body": b"not found\n"})
