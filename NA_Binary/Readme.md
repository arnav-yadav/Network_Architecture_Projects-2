Binary protocol

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
