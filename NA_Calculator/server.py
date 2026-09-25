#!/usr/bin/env python3
"""
A calculator that stays on the line.

HTTP/1.1 over a raw socket. No framework, no http.server, no library that
knows what a request is. One event loop, many connections, and every
response framed by Content-Length so the connection can survive it.

    $ python3 server.py 8080

    GET /add?a=2&b=3     -> 200  5
    GET /sub?a=10&b=4    -> 200  6
    GET /mul?a=6&b=7     -> 200  42
    GET /div?a=9&b=3     -> 200  3
    GET /div?a=1&b=0     -> 400
    GET /add?a=x&b=3     -> 400
    GET /pow?a=2&b=8     -> 404
    POST /add            -> 405
    GET /add  (no Host)  -> 400
"""

import errno
import selectors
import socket
import sys
import time
from email.utils import formatdate
from urllib.parse import urlsplit, parse_qs

IDLE_TIMEOUT = 15.0        # seconds a connection may sit silent before we close it
MAX_HEADER_BYTES = 8192    # a request head larger than this is hostile, not real
MAX_BODY_BYTES = 1 << 20   # 1 MiB
OPS = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a / b,
}

REASON = {
    200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
    408: "Request Timeout", 411: "Length Required", 413: "Payload Too Large",
    431: "Request Header Fields Too Large", 505: "HTTP Version Not Supported",
}


# TCP is a byte stream, so the end of a request is ours to find: delimiter
# for the head, Content-Length for the body.

class Incomplete(Exception):
    """Not enough bytes yet. Keep the buffer and wait for more."""


class BadRequest(Exception):
    def __init__(self, status=400, detail="malformed request"):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def take_request(buf: bytearray):
    """Pull exactly one request off the front of buf, or raise Incomplete.

    Returns (method, target, version, headers, body). Mutates buf.
    """
    # head: read to the blank line
    end = buf.find(b"\r\n\r\n")
    if end == -1:
        if len(buf) > MAX_HEADER_BYTES:
            raise BadRequest(431, "header block too large")
        raise Incomplete
    head = bytes(buf[:end])

    try:
        lines = head.decode("iso-8859-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        raise BadRequest(400, "bad request line")

    if not version.startswith("HTTP/"):
        # "NOT A REQUEST" parses into three words but is not a request line.
        # That is malformed input, not a version we happen to lack.
        raise BadRequest(400, "bad request line")
    if not version.startswith("HTTP/1."):
        raise BadRequest(505, "unsupported version")

    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        if not _:
            raise BadRequest(400, "header without a colon")
        headers[name.strip().lower()] = value.strip()

    # body: sized by Content-Length, or chunked
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body, consumed = take_chunked(buf, end + 4)
    else:
        raw = headers.get("content-length", "0")
        if not raw.isdigit():
            raise BadRequest(400, "bad Content-Length")
        n = int(raw)
        if n > MAX_BODY_BYTES:
            raise BadRequest(413, "body too large")
        if len(buf) < end + 4 + n:
            raise Incomplete
        body = bytes(buf[end + 4:end + 4 + n])
        consumed = end + 4 + n

    # Consume exactly this request. Anything after it is the next one.
    del buf[:consumed]
    return method, target, version, headers, body


def take_chunked(buf: bytearray, start: int):
    """Decode a chunked body starting at `start`. Returns (body, total_consumed)."""
    out = bytearray()
    i = start
    while True:
        nl = buf.find(b"\r\n", i)
        if nl == -1:
            raise Incomplete
        try:
            size = int(bytes(buf[i:nl]).split(b";")[0], 16)
        except ValueError:
            raise BadRequest(400, "bad chunk size")
        i = nl + 2
        if size == 0:
            # trailer section, then the final CRLF
            endt = buf.find(b"\r\n", i)
            if endt == -1:
                raise Incomplete
            return bytes(out), endt + 2
        if len(buf) < i + size + 2:
            raise Incomplete
        out += buf[i:i + size]
        i += size + 2
        if len(out) > MAX_BODY_BYTES:
            raise BadRequest(413, "body too large")


# --------------------------------------------------------------------------
# the calculator
# --------------------------------------------------------------------------

def number(text: str) -> float:
    """Accept 2, -3, 4.5. Reject 'x', '', 'inf', 'nan'."""
    try:
        v = float(text)
    except (TypeError, ValueError):
        raise BadRequest(400, "operand is not a number")
    if v != v or v in (float("inf"), float("-inf")):
        raise BadRequest(400, "operand is not finite")
    return v


def tidy(v: float) -> str:
    """9/3 should print 3, not 3.0. 1/3 should still print as a float."""
    return str(int(v)) if v == int(v) else repr(v)


def handle(method, target, version, headers):
    """Return (status, body_text). Raises BadRequest for the 4xx paths."""
    # HTTP/1.1 without Host is malformed: name-based hosting depends on it.
    if version == "HTTP/1.1" and "host" not in headers:
        raise BadRequest(400, "HTTP/1.1 request without a Host header")

    parts = urlsplit(target)
    path = parts.path

    if path not in OPS:
        # 404 before 405: /pow does not exist, so there is no honest
        # Allow: header we could send for it.
        raise BadRequest(404, "no such operation")

    if method != "GET":
        raise BadRequest(405, "only GET is allowed here")

    q = parse_qs(parts.query, keep_blank_values=True)
    if "a" not in q or "b" not in q:
        raise BadRequest(400, "need both a and b")

    a, b = number(q["a"][0]), number(q["b"][0])
    if path == "/div" and b == 0:
        raise BadRequest(400, "division by zero")

    return 200, tidy(OPS[path](a, b))


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------

def build(status, body_text, close, allow=None):
    body = body_text.encode()
    head = [
        f"HTTP/1.1 {status} {REASON.get(status, 'Error')}",
        "Server: staysontheline/1.0",
        f"Date: {formatdate(usegmt=True)}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        f"Connection: {'close' if close else 'keep-alive'}",
    ]
    if not close:
        head.append(f"Keep-Alive: timeout={int(IDLE_TIMEOUT)}")
    if allow:
        head.append(f"Allow: {allow}")
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


# One thread, many sockets. selectors is epoll on Linux, so nothing here
# may block: non-blocking sockets, an out-buffer per connection, and
# EVENT_WRITE registered only while there is something left to send.

class Conn:
    __slots__ = ("sock", "inbuf", "outbuf", "closing", "last", "served")

    def __init__(self, sock):
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.closing = False      # flush what we have, then hang up
        self.last = time.monotonic()
        self.served = 0


class Server:
    def __init__(self, port, host=""):
        self.sel = selectors.DefaultSelector()
        self.conns = {}
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind((host, port))
        self.lsock.listen(128)
        self.lsock.setblocking(False)
        self.sel.register(self.lsock, selectors.EVENT_READ, self.accept)
        self.port = self.lsock.getsockname()[1]

    def accept(self, _key, _mask):
        while True:
            try:
                sock, _addr = self.lsock.accept()
            except BlockingIOError:
                return
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c = Conn(sock)
            self.conns[sock] = c
            self.sel.register(sock, selectors.EVENT_READ, self.ready)

    def ready(self, key, mask):
        c = self.conns.get(key.fileobj)
        if c is None:
            return
        if mask & selectors.EVENT_READ:
            try:
                data = c.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return self.drop(c)
            if not data:                       # peer closed
                return self.drop(c)
            c.last = time.monotonic()
            c.inbuf += data
            self.pump(c)
        if mask & selectors.EVENT_WRITE:
            self.flush(c)

    def pump(self, c):
        """Serve every complete request in the buffer, which is what makes
        pipelining work: six requests in one segment, six responses in order."""
        while not c.closing:
            try:
                method, target, version, headers, _body = take_request(c.inbuf)
            except Incomplete:
                break
            except BadRequest as e:
                # Framing is now in doubt: we cannot locate the next
                # request, so the connection cannot be reused.
                c.outbuf += build(e.status, e.detail + "\n", close=True)
                c.closing = True
                break

            try:
                status, text = handle(method, target, version, headers)
                allow = None
            except BadRequest as e:
                status, text = e.status, e.detail + "\n"
                allow = "GET" if e.status == 405 else None

            wants_close = (
                headers.get("connection", "").lower() == "close"
                or version == "HTTP/1.0"
                and headers.get("connection", "").lower() != "keep-alive"
            )
            c.outbuf += build(status, text + "\n" if status == 200 else text,
                              close=wants_close, allow=allow)
            c.served += 1
            if wants_close:
                c.closing = True

        self.flush(c)

    def flush(self, c):
        while c.outbuf:
            try:
                n = c.sock.send(c.outbuf)
            except (BlockingIOError, InterruptedError):
                break
            except OSError as e:
                if e.errno in (errno.EPIPE, errno.ECONNRESET):
                    return self.drop(c)
                return self.drop(c)
            del c.outbuf[:n]
        want = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.outbuf else 0)
        try:
            self.sel.modify(c.sock, want, self.ready)
        except (KeyError, ValueError, OSError):
            return self.drop(c)
        if c.closing and not c.outbuf:
            self.drop(c)

    def drop(self, c):
        try:
            self.sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        self.conns.pop(c.sock, None)
        try:
            c.sock.close()
        except OSError:
            pass

    def serve_forever(self):
        print(f"listening on :{self.port}  (idle timeout {IDLE_TIMEOUT:.0f}s)",
              flush=True)
        while True:
            # Never None: one wedged client would otherwise stall the
            # whole loop, and with it every other connection on it.
            for key, mask in self.sel.select(timeout=1.0):
                key.data(key, mask)
            self.reap()

    def reap(self):
        now = time.monotonic()
        for c in list(self.conns.values()):
            if now - c.last > IDLE_TIMEOUT:
                # Say why, then hang up.
                try:
                    c.sock.sendall(build(408, "idle too long\n", close=True))
                except OSError:
                    pass
                self.drop(c)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    try:
        Server(port).serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)


if __name__ == "__main__":
    main()
