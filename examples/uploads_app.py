"""Resumable uploads, for scripts/upload-test.py.

    peregrine --python-path examples --python-path python uploads_app:app

Uploads land in $PEREGRINE_UPLOAD_DIR. `/files` takes anything up to 64 MiB;
`/small` has every limit set, so the suite can reach each of them; anything
else is answered by an application that knows nothing about uploads.
"""

import hashlib
import os

from peregrine.contrib.uploads import (
    FileUploadStore, Refused, ResumableUploads, UploadLimits,
)

STORE = FileUploadStore(os.environ.get("PEREGRINE_UPLOAD_DIR", "/tmp/peregrine-uploads"))


def fingerprint(path):
    """The length and SHA-256 of a file, which is what the suite checks
    against what it sent."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            size += len(block)
            h.update(block)
    return "%d %s" % (size, h.hexdigest())


def created(scope):
    headers = dict(scope["headers"])
    if b"x-refuse" in headers:
        raise Refused(403, "not this one")
    return {"user": headers.get(b"x-user", b"anonymous").decode()}


async def finished(upload):
    body = fingerprint(upload.path)
    if upload.metadata.get("user", "anonymous") != "anonymous":
        body += " " + upload.metadata["user"]
    if upload.sha256 is not None:
        body += " digested"
    upload.remove()
    return 201, [(b"content-type", b"text/plain")], body


async def site(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"the application\n"})


small = ResumableUploads(site, "/small", uploads="/small-uploads", store=STORE,
                         limits=UploadLimits(max_size=1000, min_size=10,
                                             max_append_size=600, min_append_size=100),
                         on_complete=finished)

app = ResumableUploads(small, "/files", store=STORE,
                       limits=UploadLimits(max_size=64 << 20),
                       progress_interval=1 << 20,
                       on_create=created, on_complete=finished)
