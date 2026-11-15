"""Bounded-memory byte reader and tiny output sinks."""

from __future__ import annotations

COMPACT_THRESHOLD = 1 << 16
MAX_HEADER_AREA = 8 * 1024
MAX_DEPTH = 4
# Even if a caller supplies a larger chunk than the promised 32 KiB, never
# copy more than this into the working buffer at once.
PULL_LIMIT = 32 * 1024


class _Reader:
    """Pull chunks at most once and expose a small sliding buffer.

    ``readline`` is only used for header blocks, whose total size is capped
    at 8 KiB.  Multipart bodies are scanned with :meth:`available` /
    :meth:`buf` / :meth:`pos` / :meth:`consume`, never accumulated whole.
    """

    __slots__ = ("_chunks", "_buf", "_pos", "_eof", "_rest", "_rest_at")

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = bytearray()
        self._pos = 0
        self._eof = False
        self._rest = None
        self._rest_at = 0

    # -- chunk supply ---------------------------------------------------

    def pull(self):
        """Pull one chunk.  Return False at end of stream."""
        if self._rest is not None:
            piece = self._rest[self._rest_at : self._rest_at + PULL_LIMIT]
            self._rest_at += PULL_LIMIT
            if self._rest_at >= len(self._rest):
                self._rest = None
                self._rest_at = 0
            self._buf.extend(piece)
            return True
        if self._eof:
            return False
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._eof = True
                return False
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("chunks must yield bytes-like objects")
            if not chunk:
                continue
            if len(chunk) > PULL_LIMIT:
                # Honour the ≤32 KiB contract defensively: keep a reference
                # to the caller's object and release it in slices.
                self._rest = chunk
                self._rest_at = PULL_LIMIT
                self._buf.extend(chunk[:PULL_LIMIT])
                return True
            self._buf.extend(chunk)
            return True

    # -- buffer helpers -------------------------------------------------

    @property
    def buf(self):
        return self._buf

    @property
    def pos(self):
        return self._pos

    @property
    def eof(self):
        return self._eof

    def available(self):
        return len(self._buf) - self._pos

    def consume(self, count):
        self._pos += count
        if self._pos > COMPACT_THRESHOLD:
            del self._buf[: self._pos]
            self._pos = 0

    def readline(self, limit):
        """Read one CRLF-terminated line, excluding the CRLF.

        ``limit`` is the maximum number of bytes this line may occupy,
        including its trailing CRLF.  Exceeding it, a bare CR/LF, or EOF
        before CRLF raises ValueError (a bad header).
        """
        start = self._pos
        while True:
            index = self._buf.find(b"\r\n", self._pos)
            if index != -1:
                if index + 2 - start > limit:
                    raise ValueError("header area exceeds 8 KiB")
                line = bytes(self._buf[self._pos:index])
                self._pos = index + 2
                if b"\r" in line or b"\n" in line:
                    raise ValueError("bad header line ending")
                if self._pos > COMPACT_THRESHOLD:
                    del self._buf[: self._pos]
                    self._pos = 0
                return line
            if len(self._buf) - start > limit:
                raise ValueError("header area exceeds 8 KiB")
            if not self.pull():
                raise ValueError("unterminated header area")


class _NullSink:
    """Discard body bytes while still letting the decoder validate them."""

    __slots__ = ("count",)

    def __init__(self):
        self.count = 0

    def write(self, data):
        self.count += len(data)

    def finish(self):
        return None


class _FileSink:
    """Write decoded bytes straight to a staging file."""

    __slots__ = ("count", "_handle")

    def __init__(self, handle):
        self._handle = handle
        self.count = 0

    def write(self, data):
        self._handle.write(data)
        self.count += len(data)

    def finish(self):
        self._handle.flush()
