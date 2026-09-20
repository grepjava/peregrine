#!/usr/bin/env bash
# TLS session resumption: a ticket handed out on one connection must shorten
# the next one.
#
#   bash scripts/resumption-test.sh [path-to-peregrine]
#
# Why this file exists. BoringSSL, which is the record layer from 1.1.7 on,
# holds its TLS 1.3 NewSessionTicket until the first application write, so the
# ticket rides out with the response instead of costing a write of its own.
# Resumption works normally, but a client that expects a ticket the moment the
# handshake ends will wait for one that is not coming -- which is how the
# change was found upstream, by a client timing out after 2.006 s.
#
# Peregrine's suites asserted nothing about resumption at all before this, so
# the whole area was untested. These checks close that gap: they confirm a
# ticket resumes a session and that a connection without one does not claim to
# have resumed.
#
# They do NOT reproduce the ticket-timing behaviour itself -- see the 2x2 above
# save_session for why, and for the one flag the checks genuinely depend on.
set -u

BIN=${1:-${PEREGRINE:-$HOME/pgbuild/release/peregrine}}
PORT=${PORT:-8351}
WORK=$(mktemp -d)
PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$3" "$2"; fi; }

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
# shellcheck source=scripts/serverlib.sh
. "$HERE/serverlib.sh"
server_trap_cleanup
trap 'server_stop; rm -rf "$WORK"' EXIT

openssl req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -days 2 -nodes -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost" 2>/dev/null

server_require_port_free "$PORT" || exit 1
server_start "$BIN" --port "$PORT" --workers 1 --log-level error \
    --tls-cert "$WORK/cert.pem" --tls-key "$WORK/key.pem" \
    --python-path "$ROOT/examples" wsgi_app:application \
    > "$WORK/server.log" 2>&1

for _ in $(seq 1 60); do
    curl -sS -k --max-time 1 -o /dev/null "https://127.0.0.1:$PORT/" 2>/dev/null && break
    sleep 0.2
done
if ! curl -sS -k --max-time 2 -o /dev/null "https://127.0.0.1:$PORT/" 2>/dev/null; then
    echo "server failed to start"
    cat "$WORK/server.log"
    exit 1
fi

req() { printf 'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'; }

# `-ign_eof` is what makes these checks work, and the request is not. That is
# the opposite of what three earlier versions of this comment claimed, so here
# is the 2x2 that settles it -- same server, same session, session file size
# in bytes:
#
#                            BoringSSL      OpenSSL
#     request + ign_eof         1628          1669
#     request, no ign_eof          0             0
#     no request + ign_eof      1628          1669
#     no request, no ign_eof       0             0
#
# The request changes nothing. Without `-ign_eof`, s_client tears the
# connection down on stdin EOF before it reads a ticket, under either library,
# and every check here fails. So do not remove `-ign_eof` from save_session.
# The request is kept because it makes the connection a realistic one and
# costs nothing, not because anything depends on it.
#
# Note what this does NOT measure. BoringSSL really does hold its
# NewSessionTicket until the first application write -- that is aviancore's
# behaviour and it was found by a client that timed out waiting 2.006s for a
# ticket. This file cannot see it: s_client's teardown dominates, and all four
# cells above are identical between the two libraries.
#
# The history is worth keeping. This comment first said removing the request
# made the suite pass vacuously, then said it made the suite fail with three
# errors, then said it changed nothing -- each version reasoned from the last
# instead of being run. The version that got it right came from breaking the
# script and watching. Any claim of the form "if X were broken, check Y would
# fail" is two minutes of work to execute; if it is not worth two minutes, it
# is not worth asserting where the next reader will trust it.

# A connection that makes a real request, so the ticket is actually sent, and
# saves whatever session it ends up with.
save_session() {
    local out=$1; shift
    req | timeout 10 openssl s_client -connect "127.0.0.1:$PORT" -servername localhost \
        -sess_out "$out" -ign_eof "$@" > "$WORK/save.log" 2>&1
}

# Reconnect with that session and report what the handshake did.
use_session() {
    local in=$1; shift
    req | timeout 10 openssl s_client -connect "127.0.0.1:$PORT" -servername localhost \
        -sess_in "$in" -ign_eof "$@" 2>&1
}

echo "TLS 1.3"

save_session "$WORK/s13.sess" -tls1_3
if [ -s "$WORK/s13.sess" ]; then
    ok "a request draws a session ticket"
else
    bad "a request draws a session ticket" "a non-empty session file" "empty"
fi

out13=$(use_session "$WORK/s13.sess" -tls1_3)
case "$out13" in
    *Reused*) ok "the ticket resumes the session" ;;
    *)        bad "the ticket resumes the session" "Reused" "$(printf '%s' "$out13" | grep -E '^(New|Reused)' | head -1)" ;;
esac
case "$out13" in
    *"200 OK"*) ok "a resumed connection still serves the request" ;;
    *)          bad "a resumed connection still serves the request" "200 OK" "no 200 in the response" ;;
esac

fresh=$(req | timeout 10 openssl s_client -connect "127.0.0.1:$PORT" -servername localhost \
        -ign_eof -tls1_3 2>&1)
case "$fresh" in
    *Reused*) bad "a connection with no ticket is a full handshake" "New" "Reused" ;;
    *New*)    ok "a connection with no ticket is a full handshake" ;;
    *)        bad "a connection with no ticket is a full handshake" "New" "neither New nor Reused" ;;
esac

echo "TLS 1.2"

save_session "$WORK/s12.sess" -tls1_2
if [ -s "$WORK/s12.sess" ]; then
    ok "a request draws a session ticket"
else
    bad "a request draws a session ticket" "a non-empty session file" "empty"
fi

out12=$(use_session "$WORK/s12.sess" -tls1_2)
case "$out12" in
    *Reused*) ok "the ticket resumes the session" ;;
    *)        bad "the ticket resumes the session" "Reused" "$(printf '%s' "$out12" | grep -E '^(New|Reused)' | head -1)" ;;
esac
case "$out12" in
    *"200 OK"*) ok "a resumed connection still serves the request" ;;
    *)          bad "a resumed connection still serves the request" "200 OK" "no 200 in the response" ;;
esac

printf '\nresumption: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
