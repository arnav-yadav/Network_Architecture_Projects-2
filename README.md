# A calculator that stays on the line — and then a protocol of its own

Both halves of the brief, in Python 3 with the standard library only. No
framework, no `http.server`, no library that already knows what a request is.
Every byte on every wire here is produced and consumed by this code.

```
NA_Calculator/     the assignment  — HTTP/1.1 keep-alive calculator
  server.py         the server
  check.py          marks it the way the brief says it will be marked

NA_Binary/     the course project — HTTP, in binary
  SPEC.md           the protocol. read this first; it is the deliverable
  bframe.py         the wire format, shared by both ends
  bserve.py         Track 1 — the server
  bcurl.py          Track 2 — the client
  annotate.py       captures a real exchange and labels every byte
  Hexdump_Annotated.txt   the output of that, for one complete exchange
  www/              files to serve
```

---

## Part 1 — the calculator

```bash
cd NA_Calculator
python3 server.py 8080          # terminal 1
python3 check.py  8080          # terminal 2
```

```
the six graded requests, on one socket
  PASS  GET /add?a=2&b=3         -> 200  5
  PASS  GET /sub?a=10&b=4        -> 200  6
  PASS  GET /mul?a=6&b=7         -> 200  42
  PASS  GET /div?a=1&b=0         -> 400
  PASS  GET /pow?a=2&b=8         -> 404
  PASS  POST /add                -> 405

after all six
  PASS  socket still open: True
  PASS  1 TCP handshake, 6 responses
```

`check.py` also covers the rest of the brief (`/div?a=9&b=3` → `3`,
`a=x` → 400, no `Host` → 400), plus three things beyond it: **pipelining**
(all six written before any is read, answered in order), **`Connection:
close`** actually closing, and a request **split across three TCP segments**
being parsed correctly.

### The part that is actually hard

Keeping the connection open forces the question HTTP/1.0 never asked: *where
does this request end and the next one begin?* While you were hanging up, the
answer was "at EOF", for free.

`take_request()` in `server.py` is that question answered. The head ends at a
**delimiter** (`CRLF CRLF`) because its size is not known in advance. The body
is sized by a **length** (`Content-Length`) because a body may contain any
bytes, delimiter included. It consumes exactly head + body and leaves the rest
in the buffer — byte n+1 belongs to somebody else. Pipelining then falls out
for free: the serve loop simply keeps calling it until the buffer runs dry.

There is no third framing option, here or anywhere.

### Stretch goals, all done

| Asked for | Where |
|---|---|
| honour `Connection: close` | `pump()`, and tested in `check.py` |
| an idle timeout you can defend | `reap()`, 15 s, replies `408` before closing |
| chunked encoding | `take_chunked()` |
| take all six at once and answer in order | the `while` loop in `pump()` |

**Defending the timeout.** A connection held open costs a descriptor and two
buffers. Little's Law says what you hold is throughput × latency, so a client
that goes silent forever is a slow request that never ends — it holds its slot
indefinitely. 15 seconds is longer than any human pause between requests on a
page and far shorter than the time it takes an idle-connection flood to
exhaust the descriptor table. The server answers `408` first rather than
vanishing, so the client learns why.

### One loop, many sockets

`server.py` is single-threaded. It uses `selectors.DefaultSelector`, which is
epoll on Linux: one thread watches every connection and wakes only for the
ready ones. A connection costs a buffer and a dict entry, not an 8 MB thread
stack.

```
200/200 answered by one single-threaded loop
```

Two consequences, both visible in the code. Nothing may block — hence
non-blocking sockets, an outgoing buffer per connection, and `EVENT_WRITE`
registered only while there are bytes left to send. And the parser must be
**resumable**: `take_request` raises `Incomplete` rather than waiting, because
a request can arrive in three segments and the loop cannot stop for it.

The `select` timeout is 1.0, never `None`. `None` means block forever, and one
wedged client would then stall every other connection on the loop.

---

## Part 2 — the binary protocol

```bash
cd NA_Binary
python3 bserve.py ./www 9000            # terminal 1
python3 bcurl.py -v localhost:9000/index.html      # terminal 2
```

```bash
# several paths, and it never opens a second connection
python3 bcurl.py localhost:9000/index.html /style.css

# 404 sets a non-zero exit code
python3 bcurl.py localhost:9000/nope.html ; echo $?     # 1

# prove the forward-compatibility rule: send a frame type that cannot exist
python3 bcurl.py --probe -v localhost:9000/index.html
#   server log:  skipped unknown frame type 0x7F (20 bytes)
#                200 GET /index.html  148 bytes

# regenerate the annotated hexdump from a live exchange
python3 annotate.py localhost 9000 /index.html > Hexdump_Annotated.txt
```

### The frame header, and the defence

```
 0        1        2        3        4        5        6        7
 +--------+--------+--------+--------+--------+--------+--------+--------+
 |         length (24)      |  type  | flags  |      stream id (24)      |
 +--------+--------+--------+--------+--------+--------+--------+--------+
```

24 / 8 / 8 / 24, against HTTP/2's 24 / 8 / 8 / 31.

- **24-bit length** for the same reason HTTP/2 chose it: 16 bits caps a frame
  at 64 KiB and wastes header overhead on large bodies; 32 bits costs a fourth
  byte and lets a stranger announce a 4 GiB allocation. A separate, much lower
  *accepted* ceiling (16 KiB) does the actual defending.
- **8-bit type and 8-bit flags** because flags must be readable before the
  payload is understood, and because a nibble would leave the next field
  unaligned for no saving.
- **24-bit stream id**, not 31. HTTP/2 needs a huge id space because it
  multiplexes heavily through long-lived proxy connections and may never reuse
  an id. v1 here does not multiplex, so 16.7 M ids per connection is generous —
  and the exhaustion rule is written down rather than hidden: close the
  connection and open a new one.
- The real constraint is the **total**: 3 + 1 + 1 + 3 = **8 bytes**, a power of
  two. Fixed offsets, no straddling, no arithmetic.

### Headers

HPACK's first two mechanisms and nothing more: a **static table of the ten
names actually sent**, and **length-prefixed literals** for everything else.
One byte replaces `content-length`. No dynamic table and no Huffman coding —
both add state and a compression-oracle attack surface for a saving this
protocol does not need.

### The line that may not be skipped

> A receiver meeting a frame type it does not know MUST skip it cleanly.

Implemented at **both** ends (`bserve.py` and `bcurl.fetch`), and tested by
`--probe`, which sends type `0x7F` and then expects the request after it to be
answered normally. This is only *possible* because of the length field: a
delimiter-framed protocol cannot skip a message it does not understand,
because finding the end would require understanding it.

### Hand-in checklist

| Asked for | Delivered |
|---|---|
| The spec, two pages, enough for a stranger | `NA_Binary/SPEC.md` |
| Your program | `bserve.py` + `bcurl.py` + `bframe.py` |
| An annotated hexdump of one complete request and response | `Hexdump_Annotated.txt` |

The hexdump is generated from a live capture by `annotate.py`, so the labels
cannot drift from the bytes — the annotations are produced by walking the same
field widths the spec defines.

---

## What each part is really demonstrating

| Idea | Part 1 | Part 2 |
|---|---|---|
| Framing is length or delimiter | `CRLF CRLF` + `Content-Length` | frame length + `content-length` |
| Consume exactly, never one byte more | `take_request` | `take_frame` |
| The parser must be resumable | `Incomplete` | `Incomplete` |
| A loop beats a thread per connection | `selectors` event loop | — |
| Timeouts are not optional | `reap()`, 408 | 30 s idle close |
| Leave room for a version 2 | new headers are ignorable | unknown frame types are skipped |
| Text is debuggable, binary is cheap | telnet works against it | `-v` hexdumps, because it has to |
