#!/usr/bin/env python3
"""
Capture one request and response off the wire and annotate every byte.

    $ python3 bserve.py ./www 9000 &
    $ python3 annotate.py localhost 9000 /index.html > Hexdump_Annotated.txt

The bytes are exactly what crossed the socket and the labels are produced by
walking the field widths in SPEC.md, so the two cannot drift apart.
"""

import socket
import sys

import bframe as B

OUT = []


def w(line=""):
    OUT.append(line)


def row(offset, data, label):
    """One annotated slice: offset, hex, meaning."""
    hx = " ".join(f"{b:02X}" for b in data)
    if len(hx) > 35:
        hx = hx[:32] + "..."
    w(f"  {offset:04X}  {hx:<35}  {label}")


def annotate_frame(raw, base=0):
    length = int.from_bytes(raw[0:3], "big")
    ftype, flags = raw[3], raw[4]
    stream = int.from_bytes(raw[5:8], "big")
    payload = raw[8:8 + length]

    w(f"FRAME  {B.TYPE_NAME.get(ftype, f'UNKNOWN(0x{ftype:02X})')}"
      f"  ({8 + length} bytes on the wire)")
    w("  -- header, 8 bytes, always ------------------------------------")
    row(base + 0, raw[0:3], f"length = {length}  (24 bits, big endian)")
    row(base + 3, raw[3:4], f"type = 0x{ftype:02X} "
                            f"{B.TYPE_NAME.get(ftype, 'unknown')}")
    fl = []
    if flags & B.F_END_STREAM:
        fl.append("END_STREAM")
    if flags & B.F_ACK:
        fl.append("ACK")
    row(base + 4, raw[4:5], f"flags = 0x{flags:02X}"
                            + (f"  {'|'.join(fl)}" if fl else "  (none set)"))
    row(base + 5, raw[5:8], f"stream id = {stream}  (24 bits; client uses odd)")
    w("  -- payload ---------------------------------------------------")

    i = 0
    off = base + 8
    if ftype == B.T_REQUEST:
        m = payload[0]
        row(off, payload[0:1], f"method = {m} ({B.METHOD_NAME.get(m, '?')})"
                               "   one byte, not a word")
        plen = int.from_bytes(payload[1:3], "big")
        row(off + 1, payload[1:3], f"path length = {plen}  (u16)")
        row(off + 3, payload[3:3 + plen],
            f'path = "{payload[3:3 + plen].decode("iso-8859-1")}"')
        i = 3 + plen
    elif ftype == B.T_RESPONSE:
        status = int.from_bytes(payload[0:2], "big")
        row(off, payload[0:2], f"status = {status}  (u16, not three ASCII digits)")
        i = 2
    elif ftype == B.T_DATA:
        preview = payload[:16]
        row(off, preview, f"body bytes ({len(payload)} of them, opaque to the "
                          "framing layer)")
        text = payload.decode("utf-8", "replace")
        first = text.splitlines()[0] if text.splitlines() else ""
        w(f"        {'':<35}  starts: {first[:44]!r}")
        return

    # header list
    count = payload[i]
    row(off + i, payload[i:i + 1], f"header count = {count}")
    i += 1
    for n in range(count):
        idx = payload[i]
        start = i
        if idx == 0:
            nlen = payload[i + 1]
            name = payload[i + 2:i + 2 + nlen].decode("iso-8859-1")
            i += 2 + nlen
            note = f'literal name, {nlen} bytes: "{name}"'
        else:
            name = B.STATIC[idx]
            i += 1
            note = f'static index {idx} = "{name}"'
        vlen = int.from_bytes(payload[i:i + 2], "big")
        value = payload[i + 2:i + 2 + vlen].decode("iso-8859-1")
        i += 2 + vlen
        row(off + start, payload[start:i],
            f'header {n + 1}: {note}, value len {vlen} = "{value}"')
    w()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
    path = sys.argv[3] if len(sys.argv) > 3 else "/index.html"

    req = B.pack_frame(
        B.T_REQUEST,
        B.pack_request("GET", path, {
            "host": f"{host}:{port}",
            "user-agent": "bcurl/1.0",
            "accept": "*/*",
        }),
        stream=1, flags=B.F_END_STREAM)

    s = socket.create_connection((host, port), timeout=5)
    s.sendall(req)

    buf = bytearray()
    frames = []
    while True:
        try:
            ftype, flags, stream, payload = B.take_frame(buf)
        except B.Incomplete:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            continue
        frames.append(B.pack_frame(ftype, payload, stream=stream, flags=flags))
        if flags & B.F_END_STREAM:
            break
    s.close()

    resp = b"".join(frames)

    w("BHTTP/1 — one complete request and response, annotated")
    w("=" * 72)
    w(f"captured from {host}:{port}  ·  GET {path}")
    w(f"request: {len(req)} bytes on the wire · response: {len(resp)} bytes")
    w()
    w("RAW REQUEST BYTES")
    w(B.hexdump(req, prefix="  "))
    w()
    annotate_frame(req)
    w()
    w("RAW RESPONSE BYTES")
    w(B.hexdump(resp, prefix="  "))
    w()
    off = 0
    while off < len(resp):
        length = int.from_bytes(resp[off:off + 3], "big")
        annotate_frame(resp[off:off + 8 + length], base=off)
        off += 8 + length

    w("=" * 72)
    w("WHAT TO NOTICE")
    w()
    w("1. The header is 8 bytes and always 8 bytes. A reader takes 8, learns")
    w("   the length, takes exactly that many more, and is aligned again.")
    w("   No scanning, no delimiter, no escaping — which is why a body may")
    w("   contain any byte at all, CRLF included.")
    w()
    w("2. The status is two bytes, not three ASCII characters, and the method")
    w("   is one byte, not three. That is most of the binary win: no parsing")
    w("   of text into numbers, no case rules, no whitespace ambiguity.")
    w()
    w("3. Header names cost one byte each when they are in the static table.")
    w("   'content-length' is 14 characters in HTTP and one byte here.")
    w("   Values stay literal and length-prefixed, so any bytes are legal.")
    w()
    w("4. Content-Length is still carried, even though every frame already")
    w("   has a length. The frame length frames the FRAME; content-length")
    w("   describes the whole BODY, which may span several DATA frames.")
    w("   Length framing at two levels, exactly as FastCGI does it.")
    w()
    w("5. END_STREAM is a flag, not a sentinel value in the data. There is")
    w("   no byte sequence that could be confused with end-of-message,")
    w("   because the marker does not live in the message at all.")

    print("\n".join(OUT))


if __name__ == "__main__":
    main()
