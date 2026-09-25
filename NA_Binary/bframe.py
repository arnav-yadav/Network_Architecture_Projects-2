#!/usr/bin/env python3
"""
BHTTP/1 wire format, shared by bserve and bcurl.

SPEC.md is normative; this is the executable copy of it. Nothing here knows
about files, sockets or HTTP semantics: bytes in, frames out.
"""

import struct

#   0        1        2        3        4        5        6        7
#   +--------+--------+--------+--------+--------+--------+--------+--------+
#   |        length (24)       |  type  | flags  |      stream id (24)      |
#   +--------+--------+--------+--------+--------+--------+--------+--------+
#
# 8 bytes so the offsets are fixed. Widths are defended in SPEC.md §2.

HEADER_LEN = 8
MAX_PAYLOAD = (1 << 24) - 1     # what the length field can express
MAX_FRAME = 1 << 14             # what we are willing to accept: 16 KiB

# frame types
T_REQUEST = 0x01
T_RESPONSE = 0x02
T_DATA = 0x03
T_PING = 0x04
T_GOAWAY = 0x05

TYPE_NAME = {
    T_REQUEST: "REQUEST", T_RESPONSE: "RESPONSE", T_DATA: "DATA",
    T_PING: "PING", T_GOAWAY: "GOAWAY",
}

# flags
F_END_STREAM = 0x01
F_ACK = 0x02

# methods, as numbers rather than words
M_GET, M_HEAD = 1, 2
METHOD_NAME = {M_GET: "GET", M_HEAD: "HEAD"}
METHOD_CODE = {"GET": M_GET, "HEAD": M_HEAD}

# The ten header names this protocol actually sends. HPACK's static table,
# small enough to read. Index 0 is reserved for a literal name.
STATIC = [
    None,              # 0 = a literal name follows
    "host",            # 1
    "user-agent",      # 2
    "accept",          # 3
    "content-type",    # 4
    "content-length",  # 5
    "date",            # 6
    "server",          # 7
    "connection",      # 8
    "last-modified",   # 9
    "etag",            # 10
]
STATIC_INDEX = {name: i for i, name in enumerate(STATIC) if name}


class ProtocolError(Exception):
    """The peer sent something we cannot parse. Reply, then hang up."""


class Incomplete(Exception):
    """Not enough bytes for a whole frame yet. Wait for more."""


# --------------------------------------------------------------------------
# frame header
# --------------------------------------------------------------------------

def pack_frame(ftype: int, payload: bytes, stream: int = 0, flags: int = 0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("payload exceeds 24-bit length field")
    return (len(payload).to_bytes(3, "big")
            + bytes([ftype & 0xFF, flags & 0xFF])
            + (stream & 0xFFFFFF).to_bytes(3, "big")
            + payload)


def take_frame(buf: bytearray):
    """Pull one frame off the front of buf. Raises Incomplete if it is not
    all here yet. Returns (type, flags, stream, payload)."""
    if len(buf) < HEADER_LEN:
        raise Incomplete
    length = int.from_bytes(buf[0:3], "big")
    ftype = buf[3]
    flags = buf[4]
    stream = int.from_bytes(buf[5:8], "big")
    if length > MAX_FRAME:
        # Refuse the size rather than allocate on a stranger's say-so.
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_FRAME} limit")
    if len(buf) < HEADER_LEN + length:
        raise Incomplete
    payload = bytes(buf[HEADER_LEN:HEADER_LEN + length])
    del buf[:HEADER_LEN + length]
    return ftype, flags, stream, payload


#   count : u8
#   entry : u8 name_code, then (if 0) u8 name_len + name, then u16 value_len + value

def pack_headers(headers) -> bytes:
    items = list(headers.items()) if isinstance(headers, dict) else list(headers)
    if len(items) > 255:
        raise ProtocolError("more than 255 headers")
    out = bytearray([len(items)])
    for name, value in items:
        name = name.lower()
        value = value.encode() if isinstance(value, str) else value
        idx = STATIC_INDEX.get(name, 0)
        out.append(idx)
        if idx == 0:
            raw = name.encode()
            if len(raw) > 255:
                raise ProtocolError("header name too long")
            out.append(len(raw))
            out += raw
        if len(value) > 0xFFFF:
            raise ProtocolError("header value too long")
        out += len(value).to_bytes(2, "big")
        out += value
    return bytes(out)


def take_headers(payload: bytes, i: int):
    """Returns (headers_dict, new_offset)."""
    if i >= len(payload):
        raise ProtocolError("truncated header block")
    count = payload[i]
    i += 1
    headers = {}
    for _ in range(count):
        if i >= len(payload):
            raise ProtocolError("truncated header entry")
        idx = payload[i]
        i += 1
        if idx == 0:
            if i >= len(payload):
                raise ProtocolError("truncated literal name")
            n = payload[i]
            i += 1
            name = payload[i:i + n].decode("iso-8859-1")
            i += n
        else:
            if idx >= len(STATIC):
                raise ProtocolError(f"static index {idx} is not in the table")
            name = STATIC[idx]
        if i + 2 > len(payload):
            raise ProtocolError("truncated value length")
        vlen = int.from_bytes(payload[i:i + 2], "big")
        i += 2
        if i + vlen > len(payload):
            raise ProtocolError("value runs past the end of the frame")
        headers[name] = payload[i:i + vlen].decode("iso-8859-1")
        i += vlen
    return headers, i


# REQUEST  : u8 method | u16 path_len | path | header list
# RESPONSE : u16 status | header list

def pack_request(method: str, path: str, headers) -> bytes:
    code = METHOD_CODE.get(method.upper())
    if code is None:
        raise ProtocolError(f"unknown method {method}")
    raw = path.encode()
    if len(raw) > 0xFFFF:
        raise ProtocolError("path too long")
    return (bytes([code]) + len(raw).to_bytes(2, "big") + raw
            + pack_headers(headers))


def take_request(payload: bytes):
    if len(payload) < 3:
        raise ProtocolError("request frame too short")
    method = METHOD_NAME.get(payload[0])
    if method is None:
        raise ProtocolError(f"method code {payload[0]} is not defined")
    plen = int.from_bytes(payload[1:3], "big")
    if 3 + plen > len(payload):
        raise ProtocolError("path runs past the end of the frame")
    path = payload[3:3 + plen].decode("iso-8859-1")
    headers, _ = take_headers(payload, 3 + plen)
    return method, path, headers


def pack_response(status: int, headers) -> bytes:
    return status.to_bytes(2, "big") + pack_headers(headers)


def take_response(payload: bytes):
    if len(payload) < 2:
        raise ProtocolError("response frame too short")
    status = int.from_bytes(payload[0:2], "big")
    headers, _ = take_headers(payload, 2)
    return status, headers



def hexdump(data: bytes, prefix: str = "") -> str:
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexpart = " ".join(f"{b:02X}" for b in row)
        asciipart = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{prefix}{off:04X}  {hexpart:<47}  {asciipart}")
    return "\n".join(lines)


def describe(ftype, flags, stream, payload) -> str:
    name = TYPE_NAME.get(ftype, f"UNKNOWN(0x{ftype:02X})")
    bits = []
    if flags & F_END_STREAM:
        bits.append("END_STREAM")
    if flags & F_ACK:
        bits.append("ACK")
    return (f"{name} len={len(payload)} stream={stream} "
            f"flags=0x{flags:02X}{(' ' + '|'.join(bits)) if bits else ''}")
