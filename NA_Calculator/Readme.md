Calculator

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
