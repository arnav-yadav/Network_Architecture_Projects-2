# BHTTP/1 — a binary request/response protocol

Version 1.0 · request/response over one TCP connection · written to be
implemented by someone who has never spoken to me.

---

## 1. Connection

A client opens one TCP connection and keeps it. All frames for all requests
travel on it, in both directions, until either side sends `GOAWAY` or closes.

There is no handshake and no version negotiation in v1. The first frame a
client sends is a `REQUEST`. A server that receives anything else first MAY
close the connection.

Byte order is big-endian throughout, matching every other protocol on the
wire.

---

## 2. The frame header — 8 bytes, always

```
 0        1        2        3        4        5        6        7
 +--------+--------+--------+--------+--------+--------+--------+--------+
 |         length (24)      |  type  | flags  |      stream id (24)      |
 +--------+--------+--------+--------+--------+--------+--------+--------+
                            |<- payload: exactly `length` bytes ->|
```

| Field | Width | Meaning |
|---|---|---|
| length | 24 bits | Payload bytes that follow the header. Does **not** include the header. |
| type | 8 bits | What the payload is. See §3. |
| flags | 8 bits | Eight booleans, defined per type. See §4. |
| stream id | 24 bits | Which request/response pair this frame belongs to. |

A receiver reads 8 bytes, learns `length`, reads exactly that many more, and
is aligned for the next header. There is no delimiter anywhere in this
protocol and therefore nothing to escape.

### Why these widths

**length = 24 bits.** 16 bits caps a frame at 64 KiB, which forces a file
body into many frames and wastes header overhead. 32 bits costs a fourth
byte and lets a peer announce a 4 GiB payload, which is an invitation to
allocate memory on a stranger's say-so. 24 bits expresses 16 MiB, which is
more than any single frame should ever be. HTTP/2 chose 24 for the same
reason and then went further: it also imposes a *negotiated* ceiling well
below the field's maximum. This protocol does the same — see §7.

**type = 8 bits.** 256 frame types is far more than this protocol will ever
define, and one byte keeps the header aligned. Four bits would save nothing,
since the next field would then be unaligned.

**flags = 8 bits.** Flags are booleans that apply to the frame itself, not to
the message inside it, so they must be in the header where a receiver can
act on them before parsing a payload it may not understand.

**stream id = 24 bits.** Two choices are visible here. HTTP/2 uses 31 bits
with one reserved, because a long-lived multiplexed connection through a
proxy can burn ids quickly and must never reuse one. This protocol does not
multiplex in v1 — a client sends one request at a time — so 16.7 million ids
on a single connection is generous. The consequence is stated plainly: **when
the id space is exhausted the connection MUST be closed and a new one
opened.** HTTP/2 has exactly the same rule at a larger number.

The real reason for 24 rather than 16 or 32 is the total: 3 + 1 + 1 + 3 = 8
bytes. A power-of-two header means a reader can slice it with fixed offsets,
and a header never straddles the boundary of a small read buffer.

### Stream ids

- Client-initiated streams are **odd**, starting at 1 and increasing by 2.
- Stream id `0` is reserved for connection-level frames (`PING`, `GOAWAY`).
- A server MUST use the id from the request on every frame of its response.

---

## 3. Frame types

| Type | Name | Payload |
|---|---|---|
| `0x01` | REQUEST | §5 |
| `0x02` | RESPONSE | §6 |
| `0x03` | DATA | Opaque body bytes |
| `0x04` | PING | Up to 64 bytes, echoed back with `ACK` |
| `0x05` | GOAWAY | Empty, or a UTF-8 reason |
| `0x06`–`0xFF` | *unassigned* | |

### The rule that may not be skipped

**A receiver that meets a frame type it does not recognise MUST skip it
cleanly and carry on.** It reads `length` bytes, discards them, and processes
the next frame normally. It MUST NOT close the connection, MUST NOT reply
with an error, and MUST NOT attempt to interpret the payload.

This single rule is what leaves room for a version 2. A v2 client can send a
`SETTINGS` frame, or a `PUSH`, to a v1 server, and the v1 server will ignore
it and answer the request that follows. Without the rule, every future
extension requires every deployed implementation to be upgraded first, which
in practice means no extension is ever shipped.

The reason this rule is *possible* is the length field. A protocol that
framed with delimiters could not skip an unknown message, because finding
its end would require understanding it.

---

## 4. Flags

| Bit | Name | Applies to | Meaning |
|---|---|---|---|
| `0x01` | END_STREAM | REQUEST, RESPONSE, DATA | The last frame of this direction of this stream |
| `0x02` | ACK | PING | This is a reply, not a request |
| `0x04`–`0x80` | *unassigned* | | MUST be ignored on receipt, MUST be sent as 0 |

END_STREAM is a flag rather than a marker in the data. Nothing in a body can
be mistaken for end-of-message, because the marker is not in the body.
Contrast SMTP, which ends a message with a lone `.` and must therefore
escape a legitimate leading dot forever.

---

## 5. REQUEST payload

```
  +--------+--------+--------+=========================+--------------------+
  | method |   path length   |       path bytes        |   header list §7   |
  |  u8    |       u16       |     (length bytes)      |                    |
  +--------+--------+--------+=========================+--------------------+
```

| Method | Code |
|---|---|
| GET | `1` |
| HEAD | `2` |

An unknown method code is a protocol error: the server replies `400` and
closes. Methods are numbers because a number needs no parsing, no
case-folding and no whitespace rules.

A REQUEST MUST carry the `host` header. A request without one gets `400`,
for the same reason HTTP/1.1 requires it: without it a server cannot know
which site was asked for.

---

## 6. RESPONSE payload

```
  +--------+--------+--------------------+
  |      status     |   header list §7   |
  |       u16       |                    |
  +--------+--------+--------------------+
```

The status is a 16-bit integer with the same meanings as HTTP: `200`, `400`,
`404`, `405`, `500`. Two bytes, not three ASCII digits that have to be parsed
back into a number.

The body, if any, follows in one or more `DATA` frames on the same stream.
The last frame of the response — the RESPONSE frame itself if there is no
body, otherwise the final DATA frame — MUST set `END_STREAM`.

---

## 7. Header list

```
  +-------+
  | count |   u8 — at most 255 headers
  +-------+
  then `count` entries, each:

    +------------+                                +-------------+==========+
    | name code  |  if 0: u8 len + name bytes     | value length |  value  |
    |     u8     |                                |     u16      |         |
    +------------+                                +-------------+==========+
```

Name code `1`–`10` indexes the **static table**; `0` means a literal name
follows, length-prefixed. This is HPACK's first two mechanisms and nothing
else: a fixed table for the names that actually occur, and a length-prefixed
literal for everything else. There is no dynamic table and no Huffman coding
in v1 — both add state and a compression-oracle attack surface for a saving
this protocol does not need.

| Index | Name | Index | Name |
|---|---|---|---|
| 1 | `host` | 6 | `date` |
| 2 | `user-agent` | 7 | `server` |
| 3 | `accept` | 8 | `connection` |
| 4 | `content-type` | 9 | `last-modified` |
| 5 | `content-length` | 10 | `etag` |

A name code above 10 is a protocol error. Header names are lowercase; there
is no case-insensitive comparison anywhere, because there is no case.

`content-length` is still sent, even though every frame carries a length.
They frame different things: the frame length sizes **one frame**, while
`content-length` describes the **whole body**, which may span several DATA
frames. Length framing at two levels — the same arrangement FastCGI uses.

---

## 8. Limits and errors

| Limit | Value | On violation |
|---|---|---|
| Max frame payload accepted | 16 KiB (`16384`) | Close the connection |
| Max header name | 255 bytes | `400` |
| Max header value | 65535 bytes | `400` |
| Max headers per list | 255 | `400` |
| Idle connection | 30 s | Close |

A malformed frame — a truncated header list, a value length running past the
end of the payload, an undefined method code — gets a `400` response with
`connection: close`, after which the connection is closed. Once framing is in
doubt the connection cannot be reused, because the receiver no longer knows
where the next frame begins. Guessing at that boundary is precisely where
request smuggling lives.

An unknown *frame type* is not an error. See §3.

---

## 9. A complete exchange

```
client                                                      server
  |                                                            |
  |-- REQUEST  stream=1  flags=END_STREAM  GET /index.html --->|
  |                                                            |
  |<-- RESPONSE stream=1  flags=0          200, 6 headers ------|
  |<-- DATA     stream=1  flags=END_STREAM 148 bytes -----------|
  |                                                            |
  |-- REQUEST  stream=3  flags=END_STREAM  GET /style.css ---->|
  |<-- RESPONSE stream=3 ... -----------------------------------|
  |<-- DATA     stream=3  flags=END_STREAM ---------------------|
  |                                                            |
  |-- GOAWAY   stream=0 --------------------------------------->|
```

Byte-level annotation of a real capture: `hexdump-annotated.txt`.

---

## 10. What v1 deliberately does not do

- **No multiplexing.** One request in flight per connection. Streams are
  numbered anyway, so v2 can multiplex without a header change.
- **No compression.** No `gzip`, no HPACK dynamic table.
- **No trailers, no push, no priority.**
- **No TLS.** It is a transport concern and layers underneath, unchanged.

Each of these can arrive as a new frame type or a new flag, and §3's skip
rule means a v1 implementation will ignore them rather than break.

---

## 11. Conformance checklist

An implementation conforms if it:

1. writes and reads the 8-byte header with the exact field widths in §2;
2. consumes exactly `length` payload bytes and no more;
3. skips unknown frame types cleanly, per §3;
4. sends `END_STREAM` on the last frame of every message;
5. uses odd client stream ids starting at 1, and echoes the id in responses;
6. resolves static header indices 1–10 to the names in §7;
7. rejects a frame larger than 16 KiB and a REQUEST without `host`;
8. treats a malformed frame as `400` and closes afterwards.
