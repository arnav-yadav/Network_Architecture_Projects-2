#!/usr/bin/env python3
"""
Marks the calculator: one socket, every request.

    $ python3 server.py 8080        # terminal 1
    $ python3 check.py  8080        # terminal 2

Opens one TCP connection, sends the six graded requests down it, checks each
status and body, then proves the socket is still open. Also covers the rest
of the brief, pipelining, and Connection: close.

No http.client anywhere: a library would silently reconnect, and not
reconnecting is the thing being tested.
"""

import socket
import sys
import time

HOST = "localhost"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

GRADED = [
    ("GET /add?a=2&b=3",   200, "5"),
    ("GET /sub?a=10&b=4",  200, "6"),
    ("GET /mul?a=6&b=7",   200, "42"),
    ("GET /div?a=1&b=0",   400, None),
    ("GET /pow?a=2&b=8",   404, None),
    ("POST /add",          405, None),
]

EXTRA = [
    ("GET /div?a=9&b=3",   200, "3"),
    ("GET /add?a=x&b=3",   400, None),
    ("GET /add?a=2",       400, None),
    ("GET /div?a=1&b=3",   200, "0.3333333333333333"),
    ("GET /sub?a=2.5&b=1", 200, "1.5"),
]

ok = True


def say(good, text):
    global ok
    if not good:
        ok = False
    print(f"  {'PASS' if good else 'FAIL'}  {text}")


class Wire:
    """A socket and a buffer. That is the whole client."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def send(self, line, host=True, extra=()):
        method, target = line.split(" ", 1)
        head = [f"{method} {target} HTTP/1.1"]
        if host:
            head.append(f"Host: {HOST}:{PORT}")
        head.extend(extra)
        self.sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode())

    def read_response(self, timeout=3.0):
        """Read one response: headers to the blank line, then exactly
        Content-Length bytes. The rest stays buffered for the next one."""
        self.sock.settimeout(timeout)
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed while reading headers")
            self.buf += chunk
        end = self.buf.find(b"\r\n\r\n")
        head = bytes(self.buf[:end]).decode("iso-8859-1")
        lines = head.split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0))
        while len(self.buf) < end + 4 + n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed mid-body")
            self.buf += chunk
        body = bytes(self.buf[end + 4:end + 4 + n])
        del self.buf[:end + 4 + n]
        return status, headers, body.decode().strip()


def still_open(sock):
    """True if the peer has not closed. A zero-length read means FIN."""
    sock.settimeout(0.3)
    try:
        return sock.recv(1, socket.MSG_PEEK) != b""
    except socket.timeout:
        return True          # nothing waiting, nothing closed
    except OSError:
        return False


def main():
    print(f"\nconnecting once to {HOST}:{PORT}\n")
    t0 = time.time()
    sock = socket.create_connection((HOST, PORT), timeout=3)
    w = Wire(sock)

    print("the six graded requests, on one socket")
    for line, want_status, want_body in GRADED:
        w.send(line)
        status, headers, body = w.read_response()
        good = status == want_status and (want_body is None or body == want_body)
        shown = f"{status}" + (f"  {body}" if body and status == 200 else "")
        say(good, f"{line:<24} -> {shown}"
                  + ("" if good else f"   (wanted {want_status} {want_body or ''})"))

    print("\nafter all six")
    say(still_open(sock), "socket still open: True")
    say(True, f"1 TCP handshake, {len(GRADED)} responses")

    print("\nthe rest of the brief")
    for line, want_status, want_body in EXTRA:
        w.send(line)
        status, _h, body = w.read_response()
        good = status == want_status and (want_body is None or body == want_body)
        shown = f"{status}" + (f"  {body}" if body and status == 200 else "")
        say(good, f"{line:<24} -> {shown}")

    # This one is answered with 400 and a close, so it gets its own socket.
    print("\nno Host header  (own connection: the server is entitled to hang up)")
    s2 = socket.create_connection((HOST, PORT), timeout=3)
    w2 = Wire(s2)
    w2.send("GET /add?a=2&b=3", host=False)
    status, _h, _b = w2.read_response()
    say(status == 400, f"GET /add (no Host)       -> {status}")
    s2.close()

    # Six requests in one segment, six responses in order.
    print("\npipelining: all six written before reading any")
    s3 = socket.create_connection((HOST, PORT), timeout=3)
    w3 = Wire(s3)
    blob = b""
    for line, _s, _b in GRADED:
        method, target = line.split(" ", 1)
        blob += f"{method} {target} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode()
    s3.sendall(blob)
    got = [w3.read_response()[0] for _ in GRADED]
    want = [s for _l, s, _b in GRADED]
    say(got == want, f"statuses in order: {got}")
    say(still_open(s3), "socket still open after pipelining")
    s3.close()

    print("\nConnection: close is honoured")
    s4 = socket.create_connection((HOST, PORT), timeout=3)
    w4 = Wire(s4)
    w4.send("GET /add?a=1&b=1", extra=["Connection: close"])
    status, headers, body = w4.read_response()
    say(status == 200 and headers.get("connection") == "close",
        f"200 {body}, Connection: {headers.get('connection')}")
    time.sleep(0.2)
    say(not still_open(s4), "server hung up afterwards")
    s4.close()

    sock.close()
    print(f"\n{'ALL PASS' if ok else 'SOMETHING FAILED'}   ({time.time() - t0:.2f}s)\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
