#!/usr/bin/env python3
"""
bserve — a static file server speaking BHTTP/1 instead of HTTP.

    $ python3 bserve.py ./www 9000 [-v]

Accepts a connection, reads request frames, maps each path to a file under
the root, replies with status + headers + bytes, and keeps the connection
open. 404 if the file is missing, 400 if the frame is malformed.
"""

import mimetypes
import os
import socket
import sys
import time
from email.utils import formatdate

import bframe as B

IDLE_TIMEOUT = 30.0


def safe_path(root: str, urlpath: str):
    """Map a request path to a file under root, or None.

    Checked on the resolved path, not the raw string: %2e%2e, backslashes
    and symlinks all defeat string matching.
    """
    clean = urlpath.split("?", 1)[0].split("#", 1)[0]
    if not clean.startswith("/"):
        return None
    if clean.endswith("/"):
        clean += "index.html"
    root_abs = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_abs, clean.lstrip("/")))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        return None                      # escaped the root
    return target


def base_headers(extra=None):
    h = {
        "server": "bserve/1.0",
        "date": formatdate(usegmt=True),
        "connection": "keep-alive",
    }
    if extra:
        h.update(extra)
    return h


def serve_one(conn, addr, root, verbose):
    buf = bytearray()
    conn.settimeout(IDLE_TIMEOUT)
    served = 0
    while True:
        try:
            frames = []
            while True:
                try:
                    frames.append(B.take_frame(buf))
                except B.Incomplete:
                    break
            if not frames:
                chunk = conn.recv(65536)
                if not chunk:
                    break                # peer closed
                buf += chunk
                continue
        except B.ProtocolError as e:
            send(conn, B.T_RESPONSE,
                 B.pack_response(400, base_headers({"connection": "close"})),
                 stream=0, flags=B.F_END_STREAM, verbose=verbose)
            print(f"  400 {e}", flush=True)
            return served

        for ftype, flags, stream, payload in frames:
            if verbose:
                print(f"  <- {B.describe(ftype, flags, stream, payload)}", flush=True)

            # SPEC.md §3: an unknown frame type MUST be skipped cleanly.
            # take_frame already consumed exactly `length` bytes, so simply
            # ignoring it leaves the stream aligned for the next frame.
            if ftype not in (B.T_REQUEST, B.T_PING, B.T_GOAWAY):
                print(f"  skipped unknown frame type 0x{ftype:02X} "
                      f"({len(payload)} bytes)", flush=True)
                continue

            if ftype == B.T_GOAWAY:
                return served

            if ftype == B.T_PING:
                send(conn, B.T_PING, payload, stream=0, flags=B.F_ACK,
                     verbose=verbose)
                continue

            try:
                method, path, headers = B.take_request(payload)
            except B.ProtocolError as e:
                send(conn, B.T_RESPONSE,
                     B.pack_response(400, base_headers({"connection": "close"})),
                     stream=stream, flags=B.F_END_STREAM, verbose=verbose)
                print(f"  400 {e}", flush=True)
                return served

            if "host" not in headers:
                respond(conn, stream, 400, b"", "text/plain", verbose)
                served += 1
                continue

            target = safe_path(root, path)
            if target is None or not os.path.isfile(target):
                body = b"not found\n"
                respond(conn, stream, 404, body, "text/plain", verbose,
                        head_only=(method == "HEAD"))
                served += 1
                print(f"  404 {method} {path}", flush=True)
                continue

            with open(target, "rb") as fh:
                body = fh.read()
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            respond(conn, stream, 200, body, ctype, verbose,
                    head_only=(method == "HEAD"),
                    extra={"last-modified":
                           formatdate(os.path.getmtime(target), usegmt=True)})
            served += 1
            print(f"  200 {method} {path}  {len(body)} bytes", flush=True)
    return served


def respond(conn, stream, status, body, ctype, verbose, head_only=False, extra=None):
    h = base_headers(extra)
    h["content-type"] = ctype
    h["content-length"] = str(len(body))       # sizes the body, not the frame
    send(conn, B.T_RESPONSE, B.pack_response(status, h), stream=stream,
         flags=(B.F_END_STREAM if head_only or not body else 0), verbose=verbose)
    if body and not head_only:
        for i in range(0, len(body), B.MAX_FRAME):
            piece = body[i:i + B.MAX_FRAME]
            last = i + len(piece) >= len(body)
            send(conn, B.T_DATA, piece, stream=stream,
                 flags=(B.F_END_STREAM if last else 0), verbose=verbose)


def send(conn, ftype, payload, stream, flags, verbose):
    frame = B.pack_frame(ftype, payload, stream=stream, flags=flags)
    if verbose:
        print(f"  -> {B.describe(ftype, flags, stream, payload)}", flush=True)
    conn.sendall(frame)


def main():
    if len(sys.argv) < 3:
        print("usage: bserve.py <root> <port> [-v]", file=sys.stderr)
        return 2
    root, port = sys.argv[1], int(sys.argv[2])
    verbose = "-v" in sys.argv
    if not os.path.isdir(root):
        print(f"no such directory: {root}", file=sys.stderr)
        return 2

    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("", port))
    ls.listen(16)
    print(f"bserve: {os.path.realpath(root)} on :{port}", flush=True)

    try:
        while True:
            conn, addr = ls.accept()
            print(f"connection from {addr[0]}:{addr[1]}", flush=True)
            t0 = time.time()
            try:
                n = serve_one(conn, addr, root, verbose)
            except socket.timeout:
                print("  idle too long, closing", flush=True)
                n = 0
            except (ConnectionResetError, BrokenPipeError):
                n = 0
            finally:
                conn.close()
            print(f"connection closed after {n} request(s), "
                  f"{time.time() - t0:.2f}s", flush=True)
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    finally:
        ls.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
