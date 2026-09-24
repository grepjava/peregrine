#!/usr/bin/env python3
"""Python-only checks for peregrine.contrib and peregrine.webtransport.

    python3 scripts/contrib_test.py

Needs nothing but the standard library: no server binary, no aioquic, no
frameworks. The live FastAPI and Django suites in webtransport-test.py and
framework-test.sh still exist; this is the routing, converters, and session
plumbing those cannot reach without a build.
"""

import asyncio
import os
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

from peregrine.contrib.asgi import (  # noqa: E402
    AltSvcMiddleware, WebTransportRouter, _Route, http_version, is_http3,
    session_from, supports_webtransport,
)
from peregrine.contrib.django import WebTransportRouter as DjangoRouter  # noqa: E402
from peregrine.contrib.fastapi import WebTransportEndpoint  # noqa: E402
from peregrine.webtransport import WebTransportSession  # noqa: E402

PASS = 0
FAIL = 0


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


class Channel:
    def __init__(self, scripted=()):
        self.inbox = asyncio.Queue()
        self.sent = []
        for message in scripted:
            self.inbox.put_nowait(message)

    async def receive(self):
        return await self.inbox.get()

    async def send(self, message):
        self.sent.append(message)


SCOPE = {"type": "webtransport", "path": "/probe", "headers": []}


def test_starlette_routes():
    print("\nStarlette-style routes")
    is_("{name} matches a segment",
        _Route("/room/{name}", None).match("/room/lobby"),
        {"name": "lobby"})
    is_("{name} does not cross a slash",
        _Route("/room/{name}", None).match("/room/a/b"),
        None)
    is_("{count:int} converts",
        _Route("/n/{count:int}", None).match("/n/42"),
        {"count": 42})
    is_("{count:int} rejects a non-integer",
        _Route("/n/{count:int}", None).match("/n/abc"),
        None)
    is_("{count:int} rejects a signed value the way Starlette does",
        _Route("/n/{count:int}", None).match("/n/-1"),
        None)
    ident = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    matched = _Route("/u/{id:uuid}", None).match("/u/" + ident)
    check("{id:uuid} converts",
          isinstance(matched.get("id") if matched else None, uuid.UUID),
          matched)
    is_("{rest:path} keeps slashes",
        _Route("/files/{rest:path}", None).match("/files/a/b/c"),
        {"rest": "a/b/c"})
    is_("{name:slug} accepts a slug",
        _Route("/s/{name:slug}", None).match("/s/hello-world"),
        {"name": "hello-world"})
    is_("{name:slug} rejects a space",
        _Route("/s/{name:slug}", None).match("/s/hello world"),
        None)
    try:
        _Route("/x/{n:nope}", None)
        bad("unknown converter is refused", "ValueError", "no error")
    except ValueError:
        ok("unknown converter is refused")
    try:
        _Route("/a/{n}/b/{n}", None)
        bad("duplicate path parameter is refused", "ValueError", "no error")
    except ValueError:
        ok("duplicate path parameter is refused")

    # A more specific int route must not steal a later str route.
    router = WebTransportRouter()
    router.add_route("/n/{count:int}", lambda s: "int")
    router.add_route("/n/{name}", lambda s: "str")
    int_route, int_params = router._lookup("/n/7")
    str_route, str_params = router._lookup("/n/seven")
    is_("int wins on digits", int_params, {"count": 7})
    is_("str wins on the rest", str_params, {"name": "seven"})
    check("they are different handlers",
          int_route is not None and str_route is not None
          and int_route.handler is not str_route.handler)

    from peregrine.contrib.fastapi import WebTransportRouter as FastAPIRouter

    class Endpoint(WebTransportEndpoint):
        pass

    fastapi = FastAPIRouter()
    fastapi.add_route("/chat", Endpoint)
    check("a WebTransportEndpoint subclass is wrapped, not stored as the handler",
          fastapi.routes and not isinstance(fastapi.routes[0].handler, type),
          type(fastapi.routes[0].handler) if fastapi.routes else None)


def test_django_routes():
    print("\nDjango-style routes")
    router = DjangoRouter()
    router.add_route("chat/<str:room>/", lambda s: "room")
    router.add_route("n/<int:count>/", lambda s: "count")
    router.add_route("u/<uuid:ident>/", lambda s: "uuid")
    router.add_route("files/<path:rest>", lambda s: "path")

    _, params = router._lookup("/chat/lobby/")
    is_("a leading slash is optional at registration",
        params, {"room": "lobby"})
    _, params = router._lookup("/n/42/")
    is_("<int:> converts rather than leaving a string", params, {"count": 42})
    is_("<int:> does not match a non-integer (later routes can)",
        router._lookup("/n/abc/")[1], None)
    ident = "12345678-1234-1234-1234-123456789abc"
    _, params = router._lookup("/u/" + ident + "/")
    check("<uuid:> converts",
          isinstance((params or {}).get("ident"), uuid.UUID), params)
    _, params = router._lookup("/files/a/b/c")
    is_("<path:> keeps slashes", params, {"rest": "a/b/c"})
    is_("Django <path:> does not match an empty remainder",
        router._lookup("/files/")[1], None)
    is_("without a trailing slash, the exact pattern misses",
        router._lookup("/chat/lobby")[1], None)


async def test_slash_fallback():
    print("\nTrailing slashes")
    seen = []

    async def echo(session):
        seen.append(session.path)
        await session.close(code=0)

    router = WebTransportRouter()
    router.add_route("/wt/echo", echo)
    channel = Channel([{"type": "webtransport.connect"}])
    await router({"type": "webtransport", "path": "/wt/echo/", "headers": []},
                 channel.receive, channel.send)
    is_("a trailing slash still reaches the handler", seen[:1], ["/wt/echo/"])
    check("and the CONNECT was refused-or-closed rather than left hanging",
          any(m.get("type") == "webtransport.close" for m in channel.sent),
          channel.sent)

    django = DjangoRouter()
    seen.clear()
    django.add_route("wt/room/<str:name>/", echo)
    channel = Channel([{"type": "webtransport.connect"}])
    await django({"type": "webtransport", "path": "/wt/room/lobby",
                  "headers": []},
                 channel.receive, channel.send)
    is_("Django without the trailing slash still matches",
        seen[:1], ["/wt/room/lobby"])


def test_helpers():
    print("\nhttp_version / supports_webtransport")
    is_("a raw HTTP/3 scope reports 3",
        http_version({"http_version": "3"}), "3")
    is_("is_http3 agrees", is_http3({"http_version": "3"}), True)
    check("HTTP/3 scopes with the extension advertise WebTransport",
          supports_webtransport({"extensions": {"webtransport": {}}}),
          "missing")
    check("HTTP/1.1 does not",
          not supports_webtransport({"http_version": "1.1"}),
          "advertised")

    class Request:
        def __init__(self, scope):
            self.scope = scope

    is_("a Starlette-style request is unwrapped",
        http_version(Request({"http_version": "2"})), "2")
    check("and so is supports_webtransport",
          supports_webtransport(Request({"extensions": {"webtransport": {}}})),
          "missing")
    check("a WSGI request with no scope does not claim WebTransport",
          not supports_webtransport(object()),
          "claimed")

    from peregrine.contrib.django import http_version as django_http_version

    class WSGIRequest:
        META = {"SERVER_PROTOCOL": "HTTP/3"}

    is_("Django WSGI reads SERVER_PROTOCOL",
        django_http_version(WSGIRequest()), "3")

    class ASGIRequest:
        scope = {"http_version": "3"}
        META = {"SERVER_PROTOCOL": "HTTP/1.1"}

    is_("Django ASGI prefers the scope over a stale META",
        django_http_version(ASGIRequest()), "3")


async def test_altsvc():
    print("\nAltSvcMiddleware")
    captured = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    async def capture(message):
        captured.append(message)

    wrapped = AltSvcMiddleware(app, port=443)
    await wrapped({"type": "http", "http_version": "1.1"}, None, capture)
    headers = dict(captured[0]["headers"])
    is_("HTTP/1.1 responses gain alt-svc",
        headers.get(b"alt-svc"), b'h3=":443"; ma=86400')

    captured.clear()
    await wrapped({"type": "http", "http_version": "3"}, None, capture)
    headers = dict(captured[0]["headers"])
    check("HTTP/3 responses are left alone",
          b"alt-svc" not in headers, headers)

    captured.clear()

    async def already(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"alt-svc", b'h3=":9999"')]})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = AltSvcMiddleware(already, port=443)
    await wrapped({"type": "http", "http_version": "1.1"}, None, capture)
    values = [v for k, v in captured[0]["headers"] if k.lower() == b"alt-svc"]
    is_("an existing alt-svc is not doubled", values, [b'h3=":9999"'])


async def test_endpoint_disconnect():
    print("\nWebTransportEndpoint lifecycle")
    channel = Channel([{"type": "webtransport.connect"}])
    session = session_from(SCOPE, channel.receive, channel.send)
    disconnected = []

    class BoomConnect(WebTransportEndpoint):
        async def on_connect(self):
            raise RuntimeError("refused to start")

        async def on_disconnect(self, code, reason):
            disconnected.append("no-accept")

    try:
        await BoomConnect(session).dispatch()
        bad("on_connect raising ends the endpoint", "RuntimeError", "no error")
    except RuntimeError as error:
        is_("on_connect raising ends the endpoint", str(error), "refused to start")
    is_("on_disconnect is not called when the session was never accepted",
        disconnected, [])

    channel = Channel([{"type": "webtransport.connect"}])
    session = WebTransportSession(SCOPE, channel.receive, channel.send)
    disconnected = []

    class AcceptThenBoom(WebTransportEndpoint):
        async def on_connect(self):
            await self.session.accept()
            raise RuntimeError("after accept")

        async def on_disconnect(self, code, reason):
            disconnected.append("after-accept")

    try:
        await AcceptThenBoom(session).dispatch()
        bad("on_connect raising after accept still ends the endpoint",
            "RuntimeError", "no error")
    except RuntimeError as error:
        is_("on_connect raising after accept still ends the endpoint",
            str(error), "after accept")
    is_("on_disconnect runs after a session that was accepted",
        disconnected, ["after-accept"])
    await session._stop()

    channel = Channel([{"type": "webtransport.connect"}])
    session = WebTransportSession(SCOPE, channel.receive, channel.send)

    class BothBoom(WebTransportEndpoint):
        async def on_connect(self):
            await self.session.accept()
            raise RuntimeError("after accept")

        async def on_disconnect(self, code, reason):
            raise RuntimeError("disconnect failed")

    try:
        await BothBoom(session).dispatch()
        bad("on_disconnect does not hide the original error",
            "RuntimeError", "no error")
    except RuntimeError as error:
        is_("on_disconnect does not hide the original error",
            str(error), "after accept")
    await session._stop()


async def test_close_on_handler_error():
    print("\nRouter closes a session the handler abandoned")
    seen = []

    async def boom(session):
        await session.accept()
        seen.append("accepted")
        raise RuntimeError("handler failed")

    router = WebTransportRouter()
    router.add_route("/boom", boom)
    channel = Channel([{"type": "webtransport.connect"}])
    try:
        await router({"type": "webtransport", "path": "/boom", "headers": []},
                     channel.receive, channel.send)
        bad("handler error propagates", "RuntimeError", "no error")
    except RuntimeError as error:
        is_("handler error propagates", str(error), "handler failed")
    is_("the handler did run", seen, ["accepted"])
    check("the session was still closed",
          any(m.get("type") == "webtransport.close" for m in channel.sent),
          channel.sent)

    class CloseFails(Channel):
        async def send(self, message):
            self.sent.append(message)
            if message.get("type") == "webtransport.close":
                raise RuntimeError("close failed")

    channel = CloseFails([{"type": "webtransport.connect"}])

    async def boom_then_close_fails(session):
        await session.accept()
        raise RuntimeError("handler failed")

    router = WebTransportRouter()
    router.add_route("/boom", boom_then_close_fails)
    try:
        await router({"type": "webtransport", "path": "/boom", "headers": []},
                     channel.receive, channel.send)
        bad("a failing close does not hide the handler error",
            "RuntimeError", "no error")
    except RuntimeError as error:
        is_("a failing close does not hide the handler error",
            str(error), "handler failed")


async def test_per_stream_backpressure():
    print("\nPer-stream backpressure")
    from peregrine.webtransport import DATAGRAM_QUEUE, INCOMING_QUEUE, STREAM_QUEUE

    channel = Channel([{"type": "webtransport.connect"}])
    session = WebTransportSession(SCOPE, channel.receive, channel.send)
    await session.accept()
    for _ in range(STREAM_QUEUE):
        channel.inbox.put_nowait({
            "type": "webtransport.stream.receive",
            "stream": 0, "data": b"x", "more_data": True,
        })
    channel.inbox.put_nowait({
        "type": "webtransport.stream.receive",
        "stream": 4, "data": b"fast", "more_data": False,
    })
    slow = await asyncio.wait_for(session.accept_stream(), 5)
    fast = await asyncio.wait_for(session.accept_stream(), 5)
    is_("the slow stream is announced first", slow.id, 0)
    is_("an unread stream does not hide the next one", fast.id, 4)
    is_("the next stream is readable while the first is unread",
        await asyncio.wait_for(fast.read(), 5), b"fast")
    check("the slow stream was paused",
          any(m.get("type") == "webtransport.stream.pause"
              and m.get("stream") == 0 for m in channel.sent),
          channel.sent)
    n = 0
    while n < STREAM_QUEUE:
        chunk = await asyncio.wait_for(slow.receive(), 5)
        if chunk is None:
            break
        n += 1
    check("the slow stream was resumed after a drain",
          any(m.get("type") == "webtransport.stream.resume"
              and m.get("stream") == 0 for m in channel.sent),
          channel.sent)
    await session._stop()

    channel = Channel([{"type": "webtransport.connect"}])
    session = WebTransportSession(SCOPE, channel.receive, channel.send)
    await session.accept()
    for i in range(DATAGRAM_QUEUE + 8):
        channel.inbox.put_nowait({
            "type": "webtransport.datagram.receive",
            "data": b"%d" % i,
        })
    channel.inbox.put_nowait({
        "type": "webtransport.stream.receive",
        "stream": 0, "data": b"ok", "more_data": False,
    })
    stream = await asyncio.wait_for(session.accept_stream(), 5)
    is_("a full datagram queue does not stall streams",
        await asyncio.wait_for(stream.read(), 5), b"ok")
    await session._stop()

    channel = Channel([{"type": "webtransport.connect"}])
    session = WebTransportSession(SCOPE, channel.receive, channel.send)
    await session.accept()
    for i in range(INCOMING_QUEUE):
        channel.inbox.put_nowait({
            "type": "webtransport.stream.receive",
            "stream": i * 4, "data": b"a", "more_data": True,
        })
    overflow = INCOMING_QUEUE * 4
    channel.inbox.put_nowait({
        "type": "webtransport.stream.receive",
        "stream": overflow, "data": b"held", "more_data": False,
    })
    channel.inbox.put_nowait({
        "type": "webtransport.stream.receive",
        "stream": 0, "data": b"b", "more_data": False,
    })
    first = await asyncio.wait_for(session.accept_stream(), 5)
    is_("a full incoming queue does not stall a stream already accepted",
        await asyncio.wait_for(first.read(), 5), b"ab")
    check("the overflow stream was paused rather than stalling the pump",
          any(m.get("type") == "webtransport.stream.pause"
              and m.get("stream") == overflow for m in channel.sent),
          channel.sent)
    await session._stop()


def test_as_bytes():
    print("\nBytes coercion")
    from peregrine.webtransport import _as_bytes
    is_("bytes pass through", _as_bytes(b"hi"), b"hi")
    is_("bytearray is copied", _as_bytes(bytearray(b"hi")), b"hi")
    is_("str is encoded", _as_bytes("hi"), b"hi")
    try:
        _as_bytes(3)
        bad("an int is refused rather than becoming NULs", "TypeError", "no error")
    except TypeError:
        ok("an int is refused rather than becoming NULs")


def test_uploads():
    import shutil
    import tempfile
    from peregrine.contrib import uploads as u

    print("\nresumable uploads: fields and store")
    is_("?1 and ?0 are Booleans", (u._sf_boolean("?1"), u._sf_boolean(" ?0 ")), (True, False))
    is_("nothing else is", [u._sf_boolean(t) for t in ("1", "?2", "true", "")], [None] * 4)
    is_("an Integer is digits", u._sf_integer(" 42 "), 42)
    is_("and not a sign, a fraction or 16 digits",
        [u._sf_integer(t) for t in ("-1", "1.0", "1" * 16, "", "0x1")], [None] * 5)
    digest = __import__("hashlib").sha256(b"x").digest()
    field = u._digest_field(digest)
    is_("a digest field reads back", u._sha256_of_field("md5=:AA==:, " + field), digest)
    is_("one that is not a byte sequence is not guessed at",
        u._sha256_of_field("sha-256=abc"), None)
    check("Want-Repr-Digest is read, and 0 means no",
          u._wants_sha256("sha-256=5") and not u._wants_sha256("sha-256=0")
          and not u._wants_sha256("sha-512=3"))
    is_("Upload-Limit lists what is set, in the draft's order",
        u.UploadLimits(max_size=10, min_append_size=2, max_age=60).field(30),
        "max-size=10, min-append-size=2, max-age=30")
    try:
        u.UploadLimits(max_size=1, min_size=2)
        bad("limits that contradict each other are refused", "ValueError", "accepted")
    except ValueError:
        ok("limits that contradict each other are refused")
    is_("on_complete's answers take every documented shape",
        [u._answer_parts(a) for a in (None, 202, b"x", (201, b"y"), (200, [], b"z"))],
        [(204, [], b""), (202, [], b""), (200, [], b"x"), (201, [], b"y"), (200, [], b"z")])

    directory = tempfile.mkdtemp(prefix="peregrine-uploads-unit-")
    try:
        store = u.FileUploadStore(directory)
        info = store.create(length=5, content_type="text/plain", metadata={"user": "ada"})
        handle = store.acquire(info.id)
        is_("a second request cannot take an upload being appended to",
            store.acquire(info.id), None)
        handle.append(b"abc")
        handle.release()
        again = store.info(info.id)
        is_("the offset is what reached the disk, and the record reads back",
            (again.offset, again.length, again.metadata), (3, 5, {"user": "ada"}))
        store.remember(info.id, 201, "text/plain", "/elsewhere", b"body\nwith newline", 7)
        answer = store.answer(info.id)
        is_("a remembered answer keeps its body whole",
            (answer.status, answer.content_type, answer.location, answer.body,
             answer.created_at), (201, "text/plain", "/elsewhere", b"body\nwith newline", 7))
        store.delete(info.id)
        check("deleting keeps the answer", store.info(info.id) is None
              and store.answer(info.id) is not None)
        is_("and expiry takes it once it is old", store.remove_expired(0), 0)
        is_("leaving nothing", os.listdir(directory), [])
        check("an id that is not one of the store's is never a path",
              store.info("../../etc/passwd") is None)

        # What a process that died part-way leaves: bytes with no record, and
        # a record never renamed into place. Old ones go; a new one may be an
        # upload being created right now, and stays.
        for name in ("a" * 32 + ".data", "b" * 32 + ".info.1.2.tmp", "c" * 32 + ".data"):
            open(os.path.join(directory, name), "wb").close()
        old = time.time() - 3600
        os.utime(os.path.join(directory, "a" * 32 + ".data"), (old, old))
        os.utime(os.path.join(directory, "b" * 32 + ".info.1.2.tmp"), (old, old))
        store.remove_expired(60)
        is_("expiry removes what a dead process left, once it is old",
            os.listdir(directory), ["c" * 32 + ".data"])
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    asyncio.run(_uploads_asgi(u))


async def _uploads_asgi(u):
    import shutil
    import tempfile
    import threading

    def scope(method, path, headers=(), root_path=""):
        return {"type": "http", "method": method, "path": path, "root_path": root_path,
                "headers": [(k.encode(), v.encode()) for k, v in headers],
                "extensions": {"http.response.informational": {}}}

    async def call(app, s, body=b"", fail_on=None):
        sent = []
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == fail_on:
                raise RuntimeError("the send failed")
            sent.append(message)
        await app(s, receive, send)
        starts = [m for m in sent if m["type"] == "http.response.start"]
        return (starts[-1]["status"] if starts else None,
                {k.lower(): v for k, v in starts[-1]["headers"]} if starts else {}, sent)

    print("\nresumable uploads: the ASGI side")
    directory = tempfile.mkdtemp(prefix="peregrine-uploads-asgi-")
    try:
        store = u.FileUploadStore(directory)
        hashed_on = []
        real = u._sha256_of_file

        def recording(path):
            hashed_on.append(threading.get_ident())
            return real(path)
        u._sha256_of_file = recording

        async def finished(upload):
            return 201, [], b"done"
        app = u.ResumableUploads(None, "/files", store=store, on_complete=finished)

        status, headers, _ = await call(app, scope(
            "POST", "/files", [("upload-complete", "?1"), ("want-repr-digest", "sha-256=1")]),
            b"abc")
        check("a whole-upload digest is computed off the event loop's thread",
              status == 201 and hashed_on and hashed_on[0] != threading.get_ident(),
              (status, hashed_on))
        u._sha256_of_file = real

        # A 104 whose send fails must not leave the upload locked.
        try:
            await call(app, scope("POST", "/files", [("upload-complete", "?0"),
                                                      ("upload-draft-interop-version", "9")]),
                       b"abc", fail_on="http.response.informational")
            bad("a failing 104 send reaches the caller", "RuntimeError", "nothing")
        except RuntimeError:
            ok("a failing 104 send reaches the caller")
        ids = [n[:-5] for n in os.listdir(directory)
               if n.endswith(".info") and not store.info(n[:-5]).complete]
        handle = store.acquire(ids[0]) if len(ids) == 1 else None
        check("and the upload it was creating can still be taken", handle is not None, ids)

        # Held elsewhere -- another worker, as far as this loop can tell.
        status, headers, _ = await call(app, scope("HEAD", "/uploads/" + ids[0]))
        is_("a HEAD while another worker appends waits, then says to retry",
            (status, headers.get(b"retry-after")), (503, b"1"))
        status, _, _ = await call(app, scope("DELETE", "/uploads/" + ids[0]))
        is_("and so does a DELETE, rather than pulling the file from under it", status, 503)
        # The other worker's append lands, and then it lets go.
        handle.append(b"abc")
        asyncio.get_running_loop().call_later(0.1, handle.release)
        status, headers, _ = await call(app, scope("HEAD", "/uploads/" + ids[0]))
        is_("one that finishes while it waits gets its final offset",
            (status, headers.get(b"upload-offset")), (204, b"3"))

        status, headers, _ = await call(app, scope("POST", "/files", [("upload-complete", "?0")],
                                                   root_path="/api/"), b"x")
        check("a root_path ending in a slash does not double it",
              headers.get(b"location", b"").startswith(b"/api/uploads/"), headers)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def run_all(ok=ok, is_=is_, check=check, bad=bad):
    """Entry used by webtransport-test.py, sharing that file's reporters."""
    globals()["ok"] = ok
    globals()["is_"] = is_
    globals()["check"] = check
    globals()["bad"] = bad
    test_starlette_routes()
    test_django_routes()
    test_helpers()
    test_as_bytes()
    test_uploads()
    asyncio.run(test_slash_fallback())
    asyncio.run(test_altsvc())
    asyncio.run(test_endpoint_disconnect())
    asyncio.run(test_close_on_handler_error())
    asyncio.run(test_per_stream_backpressure())


def main():
    run_all()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
