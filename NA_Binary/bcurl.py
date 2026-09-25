#!/usr/bin/env python3
"""
bcurl — curl, for BHTTP/1.

    $ python3 bcurl.py -v localhost:9000/index.html
    $ python3 bcurl.py localhost:9000/index.html /style.css
    $ python3 bcurl.py --probe -v localhost:9000/index.html

Body to stdout, frames hexdumped to stderr under -v, non-zero exit on
4xx/5xx. Several paths are fetched down one socket: it never opens a
second connection.
"""

import socket
import sys

import bframe as B

UA = "bcurl/1.0"


def parse_target(arg: str):
    """localhost:9000/index.html -> ("localhost", 9000, "/index.html")"""
    rest = arg.split("://", 1)[1] if "://" in arg else arg
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    return host or "localhost", int(port or 9000), "/" + path


class Client:
    def __init__(self, host, port, verbose=False):
        self.verbose = verbose
        self.buf = bytearray()
        self.stream = 1                 # client streams are odd (SPEC §2)
        self.sock = socket.create_connection((host, port), timeout=10)
        self.host = f"{host}:{port}"

    def send(self, ftype, payload, flags=0, stream=None):
        sid = self.stream if stream is None else stream
        frame = B.pack_frame(ftype, payload, stream=sid, flags=flags)
        if self.verbose:
            self.dump("send", frame, ftype, flags, sid, payload)
        self.sock.sendall(frame)

    def recv_frame(self):
        while True:
            try:
                ftype, flags, stream, payload = B.take_frame(self.buf)
            except B.Incomplete:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("server closed the connection")
                self.buf += chunk
                continue
            if self.verbose:
                frame = B.pack_frame(ftype, payload, stream=stream, flags=flags)
                self.dump("recv", frame, ftype, flags, stream, payload)
            return ftype, flags, stream, payload

    def dump(self, direction, frame, ftype, flags, stream, payload):
        arrow = "->" if direction == "send" else "<-"
        print(f"{arrow} {B.describe(ftype, flags, stream, payload)}",
              file=sys.stderr)
        print(B.hexdump(frame, prefix="   "), file=sys.stderr)
        print(file=sys.stderr)

    def fetch(self, path, method="GET"):
        self.send(B.T_REQUEST,
                  B.pack_request(method, path, {
                      "host": self.host,
                      "user-agent": UA,
                      "accept": "*/*",
                  }),
                  flags=B.F_END_STREAM)

        status, headers, body = None, {}, bytearray()
        while True:
            ftype, flags, stream, payload = self.recv_frame()

            # §3 binds both ends: unknown types are skipped here too.
            if ftype == B.T_RESPONSE:
                status, headers = B.take_response(payload)
                if flags & B.F_END_STREAM:
                    break
            elif ftype == B.T_DATA:
                body += payload
                if flags & B.F_END_STREAM:
                    break
            elif ftype == B.T_GOAWAY:
                raise ConnectionError("server sent GOAWAY")
            elif ftype == B.T_PING:
                continue
            else:
                print(f"   (ignored unknown frame type 0x{ftype:02X})",
                      file=sys.stderr)
                continue

        self.stream += 2
        return status, headers, bytes(body)

    def probe(self):
        """Send a frame type that cannot exist yet. Stream 0, because it is
        a connection-level frame, not a request. Tests SPEC.md §3."""
        self.send(0x7F, b"hello from version 2", flags=0, stream=0)
        print("   (sent unknown frame type 0x7F — the server must skip it)",
              file=sys.stderr)

    def close(self):
        try:
            self.send(B.T_GOAWAY, b"", stream=0)
        except OSError:
            pass
        self.sock.close()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    head = "-I" in sys.argv
    probing = "--probe" in sys.argv
    if not args:
        print("usage: bcurl.py [-v] [-I] [--probe] host:port/path [path2 ...]",
              file=sys.stderr)
        return 2

    host, port, first = parse_target(args[0])
    paths = [first] + [parse_target(a)[2] if ("/" in a and ":" in a) else
                       (a if a.startswith("/") else "/" + a) for a in args[1:]]

    c = Client(host, port, verbose=verbose)
    worst = 0
    try:
        if probing:
            c.probe()
        for p in paths:
            status, headers, body = c.fetch(p, "HEAD" if head else "GET")
            if verbose or head:
                print(f"status {status}", file=sys.stderr)
                for k, v in headers.items():
                    print(f"  {k}: {v}", file=sys.stderr)
            if not head:
                sys.stdout.buffer.write(body)
                sys.stdout.buffer.flush()
            if status >= 400:
                worst = max(worst, 1)
        if verbose:
            print(f"\n{len(paths)} request(s) on 1 connection", file=sys.stderr)
    finally:
        c.close()
    return worst


if __name__ == "__main__":
    sys.exit(main())
