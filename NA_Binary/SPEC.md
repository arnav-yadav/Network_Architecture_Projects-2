# BHTTP/1: binary HTTP over one TCP connection

Version 1. All multi-byte integers are big-endian (network byte order).

## 1. Connection

The client opens one TCP connection and sends all of its requests on it. There is
no handshake in v1; the first frame a client sends is normally a REQUEST. A server
MAY close a connection whose first frame is neither REQUEST nor PING, and MAY close
a connection that has been idle for 30 seconds. A client SHOULD send GOAWAY before
closing.

## 2. Frame header

Every frame starts with the same 8-byte header, followed by exactly `length` bytes
of payload.

```
 0        1        2        3        4        5        6        7
 +--------+--------+--------+--------+--------+--------+--------+--------+
 |        length (24)       |  type  | flags  |      stream id (24)      |
 +--------+--------+--------+--------+--------+--------+--------+--------+
```

| Field | Bits | Meaning |
|---|---|---|
| length | 24 | Payload size in bytes, not counting the header |
| type | 8 | Frame type (§3) |
| flags | 8 | Flags for that type (§4) |
| stream id | 24 | The request/response pair this frame belongs to |

A receiver reads 8 bytes, then exactly `length` more, and is then at the start of
the next frame.

**Why these widths.** Eight bytes in total, so every field is at a fixed offset.
`length` is 24 bits, as in HTTP/2: 16 bits (64 KiB) would split ordinary files into
many frames, and 32 bits would let a peer announce a 4 GiB frame. The limit that
actually protects a receiver is the 16 KiB it agrees to accept (§8). `type` and
`flags` get one byte each so they can be read before the payload is understood.
`stream id` is 24 bits rather than HTTP/2's 31 because v1 sends one request at a
time, so 16.7 million ids per connection is plenty. A client that runs out MUST
open a new connection.

Stream ids: requests use odd ids, starting at 1 and increasing. Id 0 is for frames
about the whole connection (PING, GOAWAY). A server MUST put the request's stream id
on every frame of its response.

## 3. Frame types

| Type | Name | Payload |
|---|---|---|
| 0x01 | REQUEST | §5 |
| 0x02 | RESPONSE | §6 |
| 0x03 | DATA | Body bytes |
| 0x04 | PING | Up to 64 bytes, sent back in a PING with the ACK flag |
| 0x05 | GOAWAY | Empty, or a UTF-8 reason; the sender then closes |
| 0x06–0xFF | unassigned | |

**Unknown frame types.** A receiver that meets a frame type it does not know MUST
skip it: read `length` bytes, discard them, and carry on with the next frame. It
MUST NOT close the connection or send an error. This applies to clients and servers
alike. It lets a version 2 add frame types without breaking version 1, and it works
because every frame carries its length in the same place.

## 4. Flags

| Bit | Name | Used on | Meaning |
|---|---|---|---|
| 0x01 | END_STREAM | REQUEST, RESPONSE, DATA | Last frame in this direction for this stream |
| 0x02 | ACK | PING | This PING is a reply |
| 0x04–0x80 | unassigned | | Sent as 0, ignored on receipt |

## 5. REQUEST payload

```
| method (u8) | path length (u16) | path bytes | header list (§7) |
```

Methods: GET = 1, HEAD = 2. Any other method code is a protocol error (§8). The
path starts with `/`. Requests carry no body in v1, so a REQUEST SHOULD set
END_STREAM. A REQUEST MUST include the `host` header; a server answers 400 to a
request without one.

## 6. RESPONSE payload

```
| status (u16) | header list (§7) |
```

Status codes have their HTTP meanings. A v1 server sends 200, 400 and 404. A path
that does not name a file under the server's root, including one that tries to
leave it, gets 404.

The body follows in zero or more DATA frames on the same stream; a body larger than
16 KiB is split across several. The last frame of the response (the RESPONSE
itself if there is no body) MUST set END_STREAM.

A response SHOULD carry `content-length`. For GET it MUST equal the total payload of
the response's DATA frames. A HEAD response carries the same `content-length` that
GET would, and no DATA frames.

## 7. Header list

```
count (u8), then `count` entries, each:
  name code (u8) | [if code is 0: name length (u8) | name] | value length (u16) | value
```

Codes 1–10 name a header from the table below; code 0 means the name follows as a
length-prefixed literal. A code above 10 is a protocol error. Header names are
lowercase ASCII.

| Code | Name | Code | Name |
|---|---|---|---|
| 1 | host | 6 | date |
| 2 | user-agent | 7 | server |
| 3 | accept | 8 | connection |
| 4 | content-type | 9 | last-modified |
| 5 | content-length | 10 | etag |

This is HPACK's static table and literal strings, without its dynamic table or
Huffman coding.

## 8. Limits and errors

| Limit | Value |
|---|---|
| Frame payload accepted | 16,384 bytes |
| Header name | 255 bytes |
| Header value | 65,535 bytes |
| Headers per list | 255 |
| Idle connection | 30 s |

A protocol error is a frame the receiver cannot safely parse: a payload over the
limit, a header list that runs past the end of its frame, or an unknown method or
header code. The server answers 400 with `connection: close` (on the request's
stream, or stream 0 if the frame header itself was bad) and closes the connection,
because it can no longer be sure where the next frame starts. An unknown frame type
is not a protocol error (§3).

## 9. Example

REQUEST (stream 1, END_STREAM) → RESPONSE (stream 1) → DATA (stream 1, last one
with END_STREAM); the next request uses stream 3 on the same connection. Every byte
of one real exchange is annotated in `Hexdump_Annotated.txt`. Later versions can add
multiplexing, compression or push as new frame types, which v1 peers skip (§3).
