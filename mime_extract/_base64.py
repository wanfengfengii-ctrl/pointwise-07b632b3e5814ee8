"""Strict, incremental base64 validator/decoder.

The MIME rule implemented here is intentionally narrower than RFC 2045:
only CRLF line breaks are ignored.  Every other byte must be a standard
base64 alphabet character, padding may only occupy the final group, and
nothing may follow padding.  Validated symbols are buffered into large
blocks before decoding so the cost stays linear even for long messages.
"""

from __future__ import annotations

import base64

_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

# ``translate`` maps alphabet symbols to 0 and every other byte to 1, so a
# single C-level ``in`` test validates a whole block.
_BAD = bytearray(b"\x01" * 256)
for _c in _ALPHABET:
    _BAD[_c] = 0
_BAD = bytes(_BAD)

_EQ = 0x3D
_LF = 0x0A
# Decode in multiples of four once this many symbols are buffered.  The
# working set stays bounded regardless of body length.
_BLOCK = 64 * 1024

_DATA, _PADDING, _DONE = 0, 1, 2


class Base64Sink:
    """Decode base64 into ``out`` while validating the stream as it arrives.

    ``out`` implements ``write(bytes)`` and ``finish()`` (the sink protocol
    shared with :mod:`mime_extract._reader`).  Input is coalesced into
    blocks so validation and decoding run at C speed rather than once per
    MIME line.
    """

    __slots__ = (
        "_out",
        "_in",
        "_tail",
        "_pending_cr",
        "_state",
        "_pad",
        "count",
    )

    def __init__(self, out):
        self._out = out
        self._in = bytearray()
        self._tail = bytearray()
        self._pending_cr = False
        self._state = _DATA
        self._pad = 0
        self.count = 0

    # -- sink protocol --------------------------------------------------

    def write(self, data):
        if not data:
            return
        self._in.extend(data)
        if len(self._in) >= _BLOCK:
            block = bytes(self._in)
            del self._in[:]
            self._process(block)

    def finish(self):
        if self._in:
            block = bytes(self._in)
            del self._in[:]
            self._process(block)
        if self._pending_cr:
            raise ValueError("bare CR in base64 body")
        if self._state == _DONE:
            self._out.finish()
            return
        if self._state == _PADDING:
            raise ValueError("incomplete base64 padding")
        if len(self._tail) % 4:
            raise ValueError("base64 length is not a multiple of four")
        self._flush(force=True)
        self._out.finish()

    # -- internals ------------------------------------------------------

    def _process(self, data):
        # Resolve a CR / LF pair that was split across two writes.
        if self._pending_cr:
            if data[0] != _LF:
                raise ValueError("bare CR in base64 body")
            data = data[1:]
            self._pending_cr = False
            if not data:
                return

        if data.endswith(b"\r"):
            data = data[:-1]
            self._pending_cr = True

        if b"\r\n" in data:
            data = data.replace(b"\r\n", b"")
        if b"\r" in data or b"\n" in data:
            raise ValueError("base64 body may only ignore CRLF")
        if not data:
            return
        if self._state == _DONE:
            raise ValueError("data after base64 padding")

        eq = data.find(b"=")
        if eq == -1:
            self._consume_data(data)
        else:
            self._consume_data(data[:eq])
            self._consume_padding(data[eq:])

    # -- internals ------------------------------------------------------

    def _consume_data(self, data):
        if not data:
            return
        if self._state == _PADDING:
            raise ValueError("data after base64 padding")
        if 1 in data.translate(_BAD):
            raise ValueError("invalid base64 alphabet character")
        self._tail.extend(data)
        if len(self._tail) >= _BLOCK:
            self._flush(force=False)

    def _flush(self, force):
        total = len(self._tail)
        complete = total - (total % 4)
        if force:
            complete = total
        elif total >= _BLOCK:
            # Leave a partial group for the next write.
            complete = total - (total % 4)
        else:
            return
        if not complete:
            return
        block = bytes(self._tail[:complete])
        del self._tail[:complete]
        self._emit(block, 0)

    def _consume_padding(self, data):
        if data.count(b"=") != len(data):
            raise ValueError("data after base64 padding")
        if self._state == _DATA:
            # Emit all complete groups, keeping only the group that the
            # padding is about to finish.
            total = len(self._tail)
            complete = total - (total % 4)
            if complete:
                block = bytes(self._tail[:complete])
                del self._tail[:complete]
                self._emit(block, 0)
            if len(self._tail) not in (2, 3):
                raise ValueError("base64 padding is not in the final group")
            self._state = _PADDING
        self._pad += len(data)
        if len(self._tail) + self._pad > 4:
            raise ValueError("too much base64 padding")
        if len(self._tail) + self._pad == 4:
            self._flush_final()

    def _flush_final(self):
        tail = bytes(self._tail)
        pad = self._pad
        if len(tail) + pad != 4 or pad not in (1, 2):
            raise ValueError("invalid base64 padding")
        self._emit(tail + b"=" * pad, pad)
        self._tail.clear()
        self._pad = 0
        self._state = _DONE

    def _emit(self, group, pad):
        decoded = base64.b64decode(group)
        self._out.write(decoded)
        self.count += len(decoded)
