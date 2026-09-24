#!/usr/bin/env python3
"""Resumable uploads (peregrine.contrib.uploads), end to end.

    ~/pgvenv/bin/python scripts/upload-test.py [path-to-peregrine]

Serves examples/uploads_app.py with two workers, so an upload resumed on a new
connection may well be resumed by another process than the one that started
it, and runs the protocol over HTTP/1.1, HTTP/2 and HTTP/3. Needs openssl, h2
and aioquic.
"""

import asyncio
import base64
import hashlib
import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

import h2.config
import h2.connection
import h2.events
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3Connection, HeadersState
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/pgbuild/release/peregrine")
EXTRA = shlex.split(os.environ.get("PEREGRINE_EXTRA_ARGS", ""))
UPLOADS = tempfile.mkdtemp(prefix="peregrine-upload-test-")

PASS = 0
FAIL = 0
CERTS = None


def ok(name):
    global PASS
    PASS += 1
    print("  ok   %s" % name)
    sys.stdout.flush()


def bad(name, expected, actual):
    global FAIL
    FAIL += 1
    print("  FAIL %s\n       expected: %r\n       actual:   %r" % (name, expected, actual))
    sys.stdout.flush()


def check(name, condition, detail=""):
    if condition:
        ok(name)
    else:
        bad(name, "true", detail or "false")


def is_(name, actual, expected):
    if actual == expected:
        ok(name)
    else:
        bad(name, expected, actual)


def payload(n, seed=0):
    return bytes((i * 131 + seed) & 0xFF for i in range(n))


def fingerprint(data):
    return "%d %s" % (len(data), hashlib.sha256(data).hexdigest())


def digest_field(data):
    return "sha-256=:%s:" % base64.b64encode(hashlib.sha256(data).digest()).decode()


def stored():
    return sorted(f for f in os.listdir(UPLOADS) if f.endswith((".data", ".info")))


def make_certs():
    global CERTS
    if CERTS is None:
        directory = tempfile.mkdtemp(prefix="peregrine-upload-certs-")
        cert, key = os.path.join(directory, "cert.pem"), os.path.join(directory, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
                        "-out", cert, "-days", "2", "-nodes", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        CERTS = (cert, key)
    return CERTS


def free_port():
    while True:
        udp, tcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket()
        try:
            udp.bind(("127.0.0.1", 0))
            port = udp.getsockname()[1]
            tcp.bind(("127.0.0.1", port))
            return port
        except OSError:
            pass
        finally:
            tcp.close()
            udp.close()


class Server:
    def __init__(self, *args, workers=2, http3=False):
        self.port = free_port()
        self.tls = http3
        cmd = [BIN, "--port", str(self.port), "--log-level", "error",
               "--workers", str(workers),
               "--python-path", os.path.join(ROOT, "examples"),
               "--python-path", os.path.join(ROOT, "python")]
        if http3:
            cert, key = make_certs()
            cmd += ["--http3", "--tls-cert", cert, "--tls-key", key]
        cmd += EXTRA + list(args) + ["uploads_app:app"]
        env = dict(os.environ, PEREGRINE_UPLOAD_DIR=UPLOADS)
        self.process = subprocess.Popen(cmd, env=env)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.25).close()
                return
            except OSError:
                if self.process.poll() is not None:
                    raise SystemExit("server exited during start-up")
                time.sleep(0.05)
        raise SystemExit("server did not start")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


# ------------------------------------------------------------------ HTTP/1.1


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    def header(self, name):
        values = [v for k, v in self.headers if k == name]
        return values[-1] if values else None

    def json(self):
        return json.loads(self.body)


class H1:
    """One connection, reading every response on it, interim ones included."""

    def __init__(self, server, timeout=15):
        self.sock = socket.create_connection(("127.0.0.1", server.port), timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    @staticmethod
    def head(method, path, headers=(), length=None):
        lines = ["%s %s HTTP/1.1" % (method, path), "Host: localhost"]
        lines += ["%s: %s" % (k, v) for k, v in headers]
        if length is not None:
            lines.append("Content-Length: %d" % length)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def send(self, data):
        self.sock.sendall(data)

    def _fill(self):
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise ConnectionError("the server closed the connection")
        self.buf += chunk

    def response(self, method="POST"):
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = []
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers.append((name.strip().lower(), value.strip()))
        r = Response(status, headers, b"")
        if status < 200 or status in (204, 304) or method == "HEAD":
            return r
        if r.header("transfer-encoding") == "chunked":
            body = b""
            while True:
                while b"\r\n" not in self.buf:
                    self._fill()
                size_line, _, self.buf = self.buf.partition(b"\r\n")
                size = int(size_line.split(b";")[0], 16)
                while len(self.buf) < size + 2:
                    self._fill()
                body += self.buf[:size]
                self.buf = self.buf[size + 2:]
                if size == 0:
                    break
            r.body = body
            return r
        length = int(r.header("content-length") or 0)
        while len(self.buf) < length:
            self._fill()
        r.body, self.buf = self.buf[:length], self.buf[length:]
        return r

    def responses(self, method="POST"):
        """Every response up to and including the final one."""
        out = []
        while True:
            r = self.response(method)
            out.append(r)
            if r.status >= 200:
                return out

    def request(self, method, path, headers=(), body=None):
        self.send(self.head(method, path, headers,
                            None if body is None and method in ("GET", "HEAD", "DELETE",
                                                                "OPTIONS")
                            else len(body or b"")) + (body or b""))
        return self.responses(method)[-1]

    def close(self):
        self.sock.close()


def request(server, method, path, headers=(), body=None):
    conn = H1(server)
    try:
        return conn.request(method, path, headers, body)
    finally:
        conn.close()


DRAFT = ("Upload-Draft-Interop-Version", "9")


def partial(offset, complete, extra=()):
    return [("Content-Type", "application/partial-upload"), ("Upload-Offset", str(offset)),
            ("Upload-Complete", "?1" if complete else "?0")] + list(extra)


def settled_offset(server, location):
    """The offset once it has stopped moving: what a client resumes from."""
    offset, previous, r = None, None, None
    for _ in range(50):
        r = request(server, "HEAD", location)
        offset = r.header("upload-offset")
        if offset is not None and offset == previous:
            break
        previous = offset
        time.sleep(0.1)
    return (int(offset) if offset and offset.isdigit() else -1), r


def uploads_h1():
    print("\nResumable uploads, HTTP/1.1")
    with Server() as s:
        whole = payload(3 << 20, 7)

        conn = H1(s)
        conn.send(conn.head("POST", "/files", [("Upload-Complete", "?1"), DRAFT],
                            len(whole)) + whole)
        responses = conn.responses()
        conn.close()
        first, final = responses[0], responses[-1]
        check("an upload in one request is told where it lives first",
              first.status == 104 and (first.header("location") or "").startswith("/uploads/"),
              [(r.status, r.headers) for r in responses[:2]])
        is_("with the limits", first.header("upload-limit"), "max-size=67108864, max-age=86400")
        progress = [r.header("upload-offset") for r in responses[1:-1] if r.status == 104]
        # Every MiB or so: an offset is reported once a MiB has arrived since
        # the last, wherever the body's pieces happen to end.
        offsets = [int(p) for p in progress]
        check("progress arrives as 104s about every MiB",
              len(offsets) >= 2 and offsets == sorted(offsets)
              and all(b - a >= 1 << 20 for a, b in zip([0] + offsets, offsets))
              and offsets[-1] <= len(whole), progress)
        is_("and it completes with on_complete's answer", (final.status, final.body.decode()),
            (201, fingerprint(whole)))
        is_("saying so", final.header("upload-complete"), "?1")
        is_("the answer is given again to a GET of its URL",
            (lambda r: (r.status, r.body.decode(), r.header("upload-complete")))(
                request(s, "GET", first.header("location"))),
            (201, fingerprint(whole), "?1"))
        is_("but the upload itself is gone once the handler removed it",
            request(s, "HEAD", first.header("location")).status, 404)

        r = request(s, "POST", "/files", body=b"plain")
        is_("a client that does not speak the protocol gets an ordinary upload",
            (r.status, r.body.decode(), r.header("upload-complete")),
            (201, fingerprint(b"plain"), None))

        # Interrupted: half the body, then the connection is gone.
        half = len(whole) // 2
        conn = H1(s)
        conn.send(conn.head("POST", "/files", [("Upload-Complete", "?1"), DRAFT],
                            len(whole)) + whole[:half])
        first = conn.response()
        location = first.header("location")
        is_("an interrupted upload had its 104", first.status, 104)
        conn.close()

        kept, r = settled_offset(s, location)
        check("what arrived before the drop is kept, and no more than was sent",
              0 < kept <= half, (kept, half))
        is_("and it is not complete", r.header("upload-complete"), "?0")
        is_("and its length is remembered", r.header("upload-length"), str(len(whole)))
        is_("and HEAD is never cached", r.header("cache-control"), "no-store")

        r = request(s, "PATCH", location, partial(0, False), whole[:10])
        is_("resuming from the wrong offset is a conflict", r.status, 409)
        is_("which names the right one", r.header("upload-offset"), str(kept))
        is_("as a problem document", r.json().get("type"),
            "https://iana.org/assignments/http-problem-types#mismatching-upload-offset")

        rest = whole[kept:]
        mid = len(rest) // 2
        r = request(s, "PATCH", location, partial(kept, False), rest[:mid])
        is_("an append that is not the last is 204",
            (r.status, r.header("upload-offset")), (204, str(kept + mid)))
        r = request(s, "PATCH", location, partial(kept + mid, True, [DRAFT]), rest[mid:])
        is_("the last append completes the upload with the handler's answer",
            (r.status, r.body.decode(), r.header("upload-complete")),
            (201, fingerprint(whole), "?1"))
        is_("the store is empty afterwards", stored(), [])

        # Created empty, then filled by one append; what on_create kept about
        # the creating request reaches on_complete.
        r = request(s, "POST", "/files", [("Upload-Complete", "?0"), ("X-User", "ada")], b"")
        location = r.header("location")
        is_("an upload created empty is 201 with its URL",
            (r.status, r.header("upload-offset"), r.header("upload-complete"),
             bool(location)), (201, "0", "?0", True))
        r = request(s, "PATCH", location, partial(0, True), b"filled")
        is_("on_complete sees what on_create recorded",
            (r.status, r.body.decode()), (201, fingerprint(b"filled") + " ada"))

        before = stored()
        r = request(s, "POST", "/files", [("Upload-Complete", "?1"), ("X-Refuse", "1")], b"x")
        is_("on_create can refuse an upload", (r.status, r.body), (403, b"not this one"))
        is_("and nothing is written for it", stored(), before)

        r = request(s, "POST", "/files", [("Upload-Complete", "?0")], b"abc")
        location = r.header("location")
        is_("DELETE cancels an upload", request(s, "DELETE", location).status, 204)
        is_("which is then gone", request(s, "HEAD", location).status, 404)
        is_("and a second DELETE finds nothing", request(s, "DELETE", location).status, 404)
        is_("an id that was never made is 404",
            request(s, "HEAD", "/uploads/" + "0" * 32).status, 404)
        is_("as is one that is not an id at all",
            request(s, "HEAD", "/uploads/%2e%2e").status, 404)
        is_("and a path below an id is not an upload's",
            request(s, "GET", "/uploads/%2e%2e/%2e%2e/etc").body, b"the application\n")

        r = request(s, "OPTIONS", "/files")
        is_("OPTIONS says what the limits are",
            (r.status, r.header("upload-limit")), (204, "max-size=67108864, max-age=86400"))

        # Malformed appends are refused before anything is written.
        r = request(s, "POST", "/files", [("Upload-Complete", "?0")], b"abc")
        location = r.header("location")
        is_("an append that is not application/partial-upload is 415",
            request(s, "PATCH", location, [("Content-Type", "text/plain"),
                                           ("Upload-Offset", "3"), ("Upload-Complete", "?1")],
                    b"d").status, 415)
        is_("one without Upload-Offset is 400",
            request(s, "PATCH", location, [("Content-Type", "application/partial-upload"),
                                           ("Upload-Complete", "?1")], b"d").status, 400)
        is_("one whose Upload-Complete is not a Boolean is 400",
            request(s, "PATCH", location, partial(3, True)[:2] + [("Upload-Complete", "yes")],
                    b"d").status, 400)
        is_("one whose offset is signed is 400",
            request(s, "PATCH", location, partial(3, True)[:1] + [("Upload-Offset", "-3"),
                                                                   ("Upload-Complete", "?1")],
                    b"d").status, 400)
        is_("and none of them moved the offset", settled_offset(s, location)[0], 3)

        # Digests: a request's own bytes (Content-Digest), and the whole
        # upload's (Repr-Digest).
        r = request(s, "PATCH", location, partial(3, False, [("Content-Digest",
                                                              digest_field(b"not this"))]),
                    b"defgh")
        is_("bytes that are not what Content-Digest says are refused",
            (r.status, r.json().get("type"), r.header("upload-offset")),
            (400, "https://garuda.dev/problems/mismatching-digest", "3"))
        is_("and dropped, so the upload is where the request began it",
            settled_offset(s, location)[0], 3)
        r = request(s, "PATCH", location, partial(3, True, [("Content-Digest",
                                                             digest_field(b"defgh"))]),
                    b"defgh")
        # A request's digest checks that request's bytes, not the upload's,
        # so it gives on_complete no SHA-256 of the whole.
        is_("bytes that are what it says are kept",
            (r.status, r.body.decode()), (201, fingerprint(b"abcdefgh")))
        r = request(s, "POST", "/files", [("Upload-Complete", "?1"),
                                          ("Content-Digest", "md5=:AAAA:")], b"x")
        is_("a digest this server cannot check is refused, not ignored", r.status, 400)

        r = request(s, "POST", "/files", [("Upload-Complete", "?1"),
                                          ("Repr-Digest", digest_field(b"whole")),
                                          ("Want-Repr-Digest", "sha-256=5")], b"whole")
        is_("a Repr-Digest that matches is accepted, and told back when asked",
            (r.status, r.header("repr-digest")), (201, digest_field(b"whole")))
        r = request(s, "POST", "/files", [("Upload-Complete", "?0"),
                                          ("Repr-Digest", digest_field(b"other"))], b"wh")
        location = r.header("location")
        r = request(s, "PATCH", location, partial(2, True), b"ole")
        is_("an upload that is not what Repr-Digest says is refused whole",
            (r.status, r.json().get("type")), (400, "https://garuda.dev/problems/mismatching-digest"))
        is_("and removed, since appending cannot mend it",
            request(s, "HEAD", location).status, 404)

        # Lengths.
        r = request(s, "POST", "/files", [("Upload-Complete", "?1"), ("Upload-Length", "9")],
                    b"abc")
        is_("an Upload-Length that disagrees with the body is a problem",
            (r.status, r.json().get("type")),
            (400, "https://iana.org/assignments/http-problem-types#inconsistent-upload-length"))
        r = request(s, "POST", "/files", [("Upload-Complete", "?0"), ("Upload-Length", "5")],
                    b"abc")
        location = r.header("location")
        r = request(s, "PATCH", location, partial(3, False), b"defg")
        is_("an append past the declared length is refused", r.status, 400)
        r = request(s, "PATCH", location, partial(3, True), b"de")
        is_("and one that ends on it completes", (r.status, r.body.decode()),
            (201, fingerprint(b"abcde")))

        is_("every other request reaches the application",
            request(s, "GET", "/somewhere").body, b"the application\n")
        is_("a GET of the upload path too", request(s, "GET", "/files").body,
            b"the application\n")


def limits_h1():
    print("\nUpload limits, HTTP/1.1")
    with Server() as s:
        r = request(s, "OPTIONS", "/small")
        is_("a second mount has limits of its own", r.header("upload-limit"),
            "max-size=1000, min-size=10, max-append-size=600, min-append-size=100, "
            "max-age=86400")
        r = request(s, "POST", "/small", [("Upload-Complete", "?0"), ("Upload-Length", "2000")],
                    b"")
        is_("a declared length over max-size is 413 with the limits",
            (r.status, bool(r.header("upload-limit"))), (413, True))
        r = request(s, "POST", "/small", [("Upload-Complete", "?0"), ("Upload-Length", "5")],
                    b"")
        is_("and one under min-size is 400", r.status, 400)
        r = request(s, "POST", "/small", [("Upload-Complete", "?1")], b"x" * 700)
        is_("a request carrying more than max-append-size is 413", r.status, 413)

        r = request(s, "POST", "/small", [("Upload-Complete", "?0")], b"x" * 50)
        location = r.header("location")
        check("its uploads live under their own prefix",
              r.status == 201 and location.startswith("/small-uploads/"), location)
        r = request(s, "PATCH", location, partial(50, False), b"y" * 20)
        is_("an append under min-append-size is 400", r.status, 400)
        r = request(s, "PATCH", location, partial(50, True), b"y" * 20)
        is_("but the one that completes may be short",
            (r.status, r.body.decode()), (201, fingerprint(b"x" * 50 + b"y" * 20)))
        r = request(s, "POST", "/small", [("Upload-Complete", "?1")], b"tiny")
        is_("an upload that ends under min-size is refused its completion", r.status, 400)


def superseding():
    print("\nA newer request for an upload ends the older one")
    with Server(workers=1) as s:
        whole = payload(1 << 20, 5)
        half = len(whole) // 2
        older = H1(s)
        older.send(older.head("POST", "/files", [("Upload-Complete", "?1"), DRAFT],
                              len(whole)) + whole[:half])
        location = older.response().header("location")
        time.sleep(0.3)
        # The older connection is still open: the client came back on a new
        # one without the first having noticed it was gone.
        kept, r = settled_offset(s, location)
        is_("a HEAD waits for the request still appending, and sees all it stored",
            kept, half)
        r = request(s, "PATCH", location, partial(half, True), whole[half:])
        is_("and the upload finishes on the new connection",
            (r.status, r.body.decode()), (201, fingerprint(whole)))
        older.close()

    with Server("--root-path", "/api") as s:
        conn = H1(s)
        conn.send(conn.head("POST", "/api/files", [("Upload-Complete", "?0"), DRAFT], 3)
                  + b"abc")
        responses = conn.responses()
        conn.close()
        location = responses[0].header("location")
        check("under --root-path an upload's URL is the one the client sees",
              location.startswith("/api/uploads/"), location)
        r = request(s, "PATCH", location, partial(3, True), b"def")
        is_("and resuming at it works", (r.status, r.body.decode()),
            (201, fingerprint(b"abcdef")))


# ------------------------------------------------------------------- HTTP/2


class H2:
    def __init__(self, server):
        self.sock = socket.create_connection(("127.0.0.1", server.port), 15)
        self.conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True))
        self.conn.initiate_connection()
        self.port = server.port
        self.flush()
        self.status, self.headers, self.body, self.interim = {}, {}, {}, {}
        self.ended = set()

    def flush(self):
        data = self.conn.data_to_send()
        if data:
            self.sock.sendall(data)

    def open(self, method, path, headers=(), length=None):
        block = [(":method", method), (":scheme", "http"),
                 (":authority", "127.0.0.1:%d" % self.port), (":path", path)]
        if length is not None:
            block.append(("content-length", str(length)))
        block += list(headers)
        stream = self.conn.get_next_available_stream_id()
        self.conn.send_headers(stream, block)
        self.flush()
        return stream

    def send(self, stream, data, end=False):
        sent = 0
        while sent < len(data):
            window = min(self.conn.local_flow_control_window(stream),
                         self.conn.max_outbound_frame_size)
            if window <= 0:
                self.step()
                continue
            self.conn.send_data(stream, data[sent:sent + window])
            sent += window
            self.flush()
        if end:
            self.conn.end_stream(stream)
            self.flush()

    def step(self, timeout=0.2):
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(1 << 20)
        except socket.timeout:
            return True
        if not data:
            return False
        for event in self.conn.receive_data(data):
            if isinstance(event, h2.events.InformationalResponseReceived):
                self.interim.setdefault(event.stream_id, []).append(dict(event.headers))
            elif isinstance(event, h2.events.ResponseReceived):
                fields = dict(event.headers)
                self.headers[event.stream_id] = fields
                self.status[event.stream_id] = int(fields[b":status"])
            elif isinstance(event, h2.events.DataReceived):
                self.body[event.stream_id] = self.body.get(event.stream_id, b"") + event.data
                self.conn.acknowledge_received_data(event.flow_controlled_length,
                                                    event.stream_id)
            elif isinstance(event, (h2.events.StreamEnded, h2.events.StreamReset)):
                self.ended.add(event.stream_id)
        self.flush()
        return True

    def wait(self, condition, deadline=20):
        limit = time.monotonic() + deadline
        while not condition() and time.monotonic() < limit:
            if not self.step():
                break


def uploads_h2():
    print("\nResumable uploads, HTTP/2")
    with Server() as s:
        whole = payload(1 << 20, 9)
        c = H2(s)
        stream = c.open("POST", "/files", [("upload-complete", "?1"),
                                           ("upload-draft-interop-version", "9")], len(whole))
        c.send(stream, whole, end=True)
        c.wait(lambda: stream in c.ended)
        is_("an upload is told where it lives",
            [f.get(b":status") for f in c.interim.get(stream, [])][:1], [b"104"])
        is_("and completes", (c.status.get(stream), c.body.get(stream, b"").decode()),
            (201, fingerprint(whole)))

        # Interrupted by a reset, then finished with PATCH.
        half = len(whole) // 2
        stream = c.open("POST", "/files", [("upload-complete", "?1"),
                                           ("upload-draft-interop-version", "9")], len(whole))
        c.send(stream, whole[:half])
        c.wait(lambda: c.interim.get(stream))
        location = c.interim.get(stream, [{}])[0].get(b"location", b"").decode()
        time.sleep(0.3)
        c.conn.reset_stream(stream, 0x8)
        c.flush()
        kept, _ = settled_offset(s, location)
        is_("a reset stream keeps what arrived", kept, half)
        rest = c.open("PATCH", location, [("content-type", "application/partial-upload"),
                                          ("upload-offset", str(half)),
                                          ("upload-complete", "?1")], len(whole) - half)
        c.send(rest, whole[half:], end=True)
        c.wait(lambda: rest in c.ended)
        is_("and the rest completes it", (c.status.get(rest), c.body.get(rest, b"").decode()),
            (201, fingerprint(whole)))
        c.sock.close()


# ------------------------------------------------------------------- HTTP/3


class InterimH3Connection(H3Connection):
    """aioquic 1.3 reads a second HEADERS frame as trailers, so after a 1xx
    the stream is put back to waiting for its response (RFC 9114 4.1)."""

    def _handle_request_or_push_frame(self, frame_type, frame_data, stream, stream_ended):
        events = super()._handle_request_or_push_frame(frame_type, frame_data, stream,
                                                       stream_ended)
        for event in events:
            if isinstance(event, HeadersReceived) and dict(event.headers).get(
                    b":status", b"").startswith(b"1"):
                stream.headers_recv_state = HeadersState.INITIAL
        return events


class H3(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = InterimH3Connection(self._quic)
        self.status, self.headers, self.body, self.interim = {}, {}, {}, {}
        self._done = {}

    def open(self, method, path, headers=(), length=None, body=None, end=True):
        stream = self._quic.get_next_available_stream_id()
        block = [(b":method", method.encode()), (b":scheme", b"https"),
                 (b":authority", b"localhost"), (b":path", path.encode())]
        if length is not None:
            block.append((b"content-length", str(length).encode()))
        block += [(k.encode(), v.encode()) for k, v in headers]
        self._http.send_headers(stream, block, end_stream=body is None and end)
        if body is not None:
            self._http.send_data(stream, body, end_stream=end)
        self._done[stream] = asyncio.get_event_loop().create_future()
        self.transmit()
        return stream

    def send(self, stream, data, end=False):
        self._http.send_data(stream, data, end_stream=end)
        self.transmit()

    async def finished(self, stream, timeout=30):
        await asyncio.wait_for(asyncio.shield(self._done[stream]), timeout)

    def quic_event_received(self, event):
        for e in self._http.handle_event(event):
            if isinstance(e, HeadersReceived):
                fields = dict(e.headers)
                if fields.get(b":status", b"").startswith(b"1"):
                    self.interim.setdefault(e.stream_id, []).append(fields)
                else:
                    self.status[e.stream_id] = int(fields[b":status"])
                    self.headers[e.stream_id] = fields
            elif isinstance(e, DataReceived):
                self.body[e.stream_id] = self.body.get(e.stream_id, b"") + e.data
            if getattr(e, "stream_ended", False):
                done = self._done.get(e.stream_id)
                if done is not None and not done.done():
                    done.set_result(None)


def uploads_h3():
    print("\nResumable uploads, HTTP/3")
    with Server(http3=True) as s:
        whole = payload(1 << 20, 11)

        async def scenario():
            config = QuicConfiguration(is_client=True, alpn_protocols=["h3"])
            config.verify_mode = ssl.CERT_NONE
            async with connect("127.0.0.1", s.port, configuration=config,
                               create_protocol=H3) as h3:
                stream = h3.open("POST", "/files", [("upload-complete", "?1"),
                                                    ("upload-draft-interop-version", "9")],
                                 body=whole)
                await h3.finished(stream)
                is_("an upload is told where it lives",
                    [f.get(b":status") for f in h3.interim.get(stream, [])][:1], [b"104"])
                is_("and completes", (h3.status.get(stream), h3.body.get(stream, b"").decode()),
                    (201, fingerprint(whole)))

                half = len(whole) // 2
                stream = h3.open("POST", "/files", [("upload-complete", "?1"),
                                                    ("upload-draft-interop-version", "9")],
                                 length=len(whole), end=False)
                h3.send(stream, whole[:half])
                for _ in range(100):
                    if h3.interim.get(stream):
                        break
                    await asyncio.sleep(0.05)
                location = h3.interim.get(stream, [{}])[0].get(b"location", b"").decode()
                is_("an interrupted upload had its 104", bool(location), True)
                await asyncio.sleep(0.3)
                h3._quic.reset_stream(stream, 0x10c)
                h3.transmit()
                offset = None
                for _ in range(50):
                    probe = h3.open("HEAD", location)
                    await h3.finished(probe)
                    offset = h3.headers.get(probe, {}).get(b"upload-offset")
                    if offset == str(half).encode():
                        break
                    await asyncio.sleep(0.1)
                is_("a reset stream keeps what arrived", offset, str(half).encode())
                rest = h3.open("PATCH", location, [("content-type", "application/partial-upload"),
                                                   ("upload-offset", str(half)),
                                                   ("upload-complete", "?1")],
                               body=whole[half:])
                await h3.finished(rest)
                is_("and the rest completes it",
                    (h3.status.get(rest), h3.body.get(rest, b"").decode()),
                    (201, fingerprint(whole)))

        asyncio.run(asyncio.wait_for(scenario(), 120))


def main():
    if not os.path.exists(BIN):
        print("no such binary: %s" % BIN)
        return 2
    print("peregrine resumable upload tests (%s)" % BIN)
    try:
        for section in (uploads_h1, limits_h1, superseding, uploads_h2, uploads_h3):
            try:
                section()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                bad("%s ran to the end" % section.__name__, "no exception", repr(exc))
    finally:
        shutil.rmtree(UPLOADS, ignore_errors=True)
    print("\npassed: %d   failed: %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
