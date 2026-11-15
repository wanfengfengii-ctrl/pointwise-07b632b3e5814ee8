"""Streaming multipart/mixed parser with strict structural validation."""

from __future__ import annotations

import os

from ._base64 import Base64Sink
from ._headers import parse_boundary, parse_headers, split_params
from ._reader import MAX_DEPTH, _FileSink, _NullSink

_NEED = object()


class Attachment:
    __slots__ = ("name", "stage_path", "size")

    def __init__(self, name, stage_path, size):
        self.name = name
        self.stage_path = stage_path
        self.size = size


def _candidate_at(buf, at, active, max_boundary, eof):
    """Decide whether a delimiter line begins at ``at`` in ``buf``.

    ``active`` is a list of ``(boundary_bytes, is_ancestor)``.  Returns
    ``_NEED`` when more input is required, ``None`` when this position does
    not hold an exact delimiter line, or
    ``(is_ancestor, is_close, end_index)``.
    """
    if len(buf) - at < 2:
        return None if eof else _NEED
    if buf[at : at + 2] != b"--":
        return None
    if len(buf) - at < 2 + max_boundary + 4 and not eof:
        return _NEED
    start = at + 2
    for boundary, is_ancestor in active:
        end = start + len(boundary)
        if end > len(buf):
            continue
        if buf[start:end] != boundary:
            continue
        if buf[end : end + 2] == b"\r\n":
            return (is_ancestor, False, end + 2)
        if buf[end : end + 4] == b"--\r\n":
            return (is_ancestor, True, end + 4)
    return None


class Parser:
    def __init__(self, reader, stage_dir):
        self.reader = reader
        self.stage_dir = stage_dir
        self.attachments = []
        self._stage_index = 0

    # -- entry point -----------------------------------------------------

    def parse(self):
        headers = parse_headers(self.reader)
        content_type = headers.get(b"content-type")
        if content_type is None:
            raise ValueError("root is missing Content-Type")
        boundary = parse_boundary(content_type)
        self._parse_multipart(boundary, (), 1, headers)
        self._drain()
        return self.attachments

    # -- multipart -------------------------------------------------------

    def _parse_multipart(self, boundary, ancestors, depth, container_headers):
        self._check_container_headers(container_headers)
        kind = self._find_first_delimiter(boundary, ancestors)
        if kind == "close":
            return
        while True:
            kind = self._parse_part(boundary, ancestors, depth)
            if kind == "close":
                return

    def _find_first_delimiter(self, boundary, ancestors):
        """Skip a preamble until the first delimiter of this multipart."""
        reader = self.reader
        active = [(boundary, False)]
        active.extend((item, True) for item in ancestors)
        max_boundary = max(len(item) for item, _ in active)
        while True:
            status = _candidate_at(
                reader.buf, reader.pos, active, max_boundary, reader.eof
            )
            if status is _NEED:
                if reader.pull():
                    continue
                status = _candidate_at(
                    reader.buf, reader.pos, active, max_boundary, True
                )
            if status is not None:
                is_ancestor, is_close, end = status
                if is_ancestor:
                    raise ValueError("parent boundary appeared early")
                reader.consume(end - reader.pos)
                return "close" if is_close else "open"

            # Look for CRLF--; the CRLF belongs to the boundary framing.
            index = reader.buf.find(b"\r\n--", reader.pos)
            if index < 0:
                keep = min(3, reader.available())
                if reader.available() > keep:
                    reader.consume(reader.available() - keep)
                if not reader.pull():
                    raise ValueError("multipart has no boundary")
                continue
            # Retain from the CRLF so the candidate test can run at --.
            reader.consume(index - reader.pos)
            status = _candidate_at(
                reader.buf, reader.pos + 2, active, max_boundary, reader.eof
            )
            if status is _NEED:
                if reader.pull():
                    continue
                status = _candidate_at(
                    reader.buf, reader.pos + 2, active, max_boundary, True
                )
            if status is not None:
                is_ancestor, is_close, end = status
                if is_ancestor:
                    raise ValueError("parent boundary appeared early")
                reader.consume(end - reader.pos)
                return "close" if is_close else "open"
            # Not a delimiter: step past this CRLF and keep searching.
            reader.consume(2)
            if not reader.available() and not reader.pull():
                raise ValueError("multipart has no boundary")

    def _parse_part(self, boundary, ancestors, depth):
        headers = parse_headers(self.reader)
        content_type = headers.get(b"content-type")
        if content_type is None:
            raise ValueError("part is missing Content-Type")
        main, _params = split_params(content_type)

        if main == b"multipart/mixed":
            if depth + 1 > MAX_DEPTH:
                raise ValueError("multipart nesting exceeds four levels")
            child = parse_boundary(content_type)
            self._parse_multipart(
                child, (boundary,) + ancestors, depth + 1, headers
            )
            # The child close line is consumed; whatever follows up to the
            # parent delimiter is epilogue and is skipped by _scan.
            return self._scan(boundary, ancestors, None, allow_start=True)

        if main.startswith(b"multipart/"):
            raise ValueError("container is not multipart/mixed")

        _validate_media_type(main)
        encoding = headers.get(b"content-transfer-encoding")
        if encoding is None:
            raise ValueError("leaf is missing Content-Transfer-Encoding")
        encoding = encoding.strip(b" \t").lower()
        if encoding not in (b"binary", b"base64"):
            raise ValueError("unsupported Content-Transfer-Encoding")

        is_attachment, filename = _attachment_info(headers)
        if is_attachment:
            return self._read_attachment(
                boundary, ancestors, encoding, filename
            )

        raw = _NullSink()
        sink = Base64Sink(raw) if encoding == b"base64" else raw
        kind = self._scan(boundary, ancestors, sink, allow_start=True)
        sink.finish()
        return kind

    def _read_attachment(self, boundary, ancestors, encoding, filename):
        stage_path = os.path.join(self.stage_dir, str(self._stage_index))
        self._stage_index += 1
        handle = open(stage_path, "xb")
        try:
            raw = _FileSink(handle)
            sink = Base64Sink(raw) if encoding == b"base64" else raw
            kind = self._scan(boundary, ancestors, sink, allow_start=True)
            sink.finish()
        finally:
            handle.close()
        self.attachments.append(Attachment(filename, stage_path, sink.count))
        return kind

    # -- container checks ------------------------------------------------

    @staticmethod
    def _check_container_headers(headers):
        encoding = headers.get(b"content-transfer-encoding")
        if encoding is not None and encoding.strip(b" \t").lower() != b"binary":
            raise ValueError("multipart body must not use base64")

    # -- body scanning ---------------------------------------------------

    def _scan(self, current, ancestors, sink, allow_start):
        """Consume up to the next delimiter of ``current``.

        Ancestor boundaries are errors before the current close.  Bytes
        before the delimiter's preceding CRLF are written to ``sink``.
        Returns ``"open"`` or ``"close"``.
        """
        reader = self.reader
        active = [(current, False)]
        active.extend((item, True) for item in ancestors)
        max_boundary = max(len(item) for item, _ in active)
        start_mode = allow_start

        while True:
            if start_mode:
                status = _candidate_at(
                    reader.buf, reader.pos, active, max_boundary, reader.eof
                )
                if status is _NEED:
                    if reader.pull():
                        continue
                    status = None
                start_mode = False
                if status is not None:
                    is_ancestor, is_close, end = status
                    if is_ancestor:
                        raise ValueError("parent boundary appeared early")
                    reader.consume(end - reader.pos)
                    return "close" if is_close else "open"

            buf = reader.buf
            pos = reader.pos
            index = buf.find(b"\r\n--", pos)
            if index < 0:
                end = len(buf)
                keep = min(3, end - pos)
                if end - keep > pos:
                    if sink is not None:
                        sink.write(bytes(buf[pos : end - keep]))
                    reader.consume(end - keep - pos)
                if reader.pull():
                    continue
                end = len(reader.buf)
                if end > reader.pos:
                    if sink is not None:
                        sink.write(bytes(reader.buf[reader.pos:end]))
                    reader.consume(end - reader.pos)
                raise ValueError("multipart body has no closing boundary")

            status = _candidate_at(
                buf, index + 2, active, max_boundary, reader.eof
            )
            if status is _NEED:
                if reader.pull():
                    continue
                status = None
            if status is not None:
                is_ancestor, is_close, end = status
                if is_ancestor:
                    raise ValueError("parent boundary appeared early")
                # ``index`` is the excluded preceding CRLF.
                if index > pos and sink is not None:
                    sink.write(bytes(buf[pos:index]))
                reader.consume(end - pos)
                return "close" if is_close else "open"

            # This CRLF-- sequence is ordinary body content.
            if sink is not None:
                sink.write(bytes(buf[pos : index + 3]))
            reader.consume(index + 3 - pos)

    def _drain(self):
        reader = self.reader
        while True:
            if reader.available():
                reader.consume(reader.available())
            if not reader.pull():
                return


def _validate_media_type(main):
    pieces = main.split(b"/")
    if len(pieces) != 2 or not pieces[0] or not pieces[1]:
        raise ValueError("bad Content-Type")
    for piece in pieces:
        for byte in piece:
            if not (
                0x30 <= byte <= 0x39
                or 0x41 <= byte <= 0x5A
                or 0x61 <= byte <= 0x7A
                or byte in b"!#$%&'*+-.^_`|~"
            ):
                raise ValueError("bad Content-Type")


def _attachment_info(headers):
    disposition = headers.get(b"content-disposition")
    if disposition is None:
        return False, None
    main, params = split_params(disposition)
    if main != b"attachment":
        return False, None
    filename = params.get(b"filename")
    if filename is None:
        raise ValueError("attachment is missing a filename")
    value, quoted = filename
    if not quoted:
        raise ValueError("attachment filename must be a quoted string")
    for byte in value:
        if byte < 0x20 or byte > 0x7E or byte == 0x5C:  # backslash
            raise ValueError("attachment filename must be unescaped ASCII")
    return True, value.decode("ascii")
