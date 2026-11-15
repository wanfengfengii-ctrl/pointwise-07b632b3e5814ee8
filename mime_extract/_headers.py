"""Strict CRLF header parsing for one MIME part."""

from __future__ import annotations

from ._reader import MAX_HEADER_AREA

_TOKEN = frozenset(
    b"!#$%&'*+-.^_`|~0123456789"
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)
_BOUNDARY = frozenset(
    b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
)


def parse_headers(reader):
    """Read headers up to and including the blank CRLF line.

    Returns a ``dict`` keyed by lowercase field name whose values are the
    raw value bytes.  Raises ValueError for folding, duplicates, malformed
    lines, missing terminators, or a header area larger than 8 KiB.
    """
    headers = {}
    used = 0
    while True:
        line = reader.readline(MAX_HEADER_AREA - used)
        used += len(line) + 2
        if used > MAX_HEADER_AREA:
            raise ValueError("header area exceeds 8 KiB")
        if not line:
            return headers
        if line[:1] in (b" ", b"\t"):
            raise ValueError("folded header line")
        colon = line.find(b":")
        if colon <= 0:
            raise ValueError("bad header line")
        name = line[:colon]
        for byte in name:
            if byte not in _TOKEN:
                raise ValueError("bad header field name")
        value = line[colon + 1 :].strip(b" \t")
        for byte in value:
            if byte == 0x7F or (byte < 0x20 and byte != 0x09):
                raise ValueError("bad control character in header value")
        key = bytes(name).lower()
        if key in headers:
            raise ValueError("duplicate header field")
        headers[key] = value


def split_params(value):
    """Parse ``type; name=value; name="quoted"``.

    Returns ``(main_type, params)`` where ``params`` maps lowercased names
    to ``(bytes, was_quoted)``.  This is deliberately strict: tokens are
    unquoted ASCII and quoted strings contain no backslash escapes.
    """
    pieces = _split_semicolons(value)
    main = pieces[0].strip(b" \t").lower()
    if not main:
        raise ValueError("empty header value")
    params = {}
    for piece in pieces[1:]:
        piece = piece.strip(b" \t")
        if not piece:
            continue
        eq = piece.find(b"=")
        if eq <= 0:
            raise ValueError("bad header parameter")
        name = piece[:eq].strip(b" \t")
        if not name or any(byte not in _TOKEN for byte in name):
            raise ValueError("bad header parameter name")
        raw = piece[eq + 1 :].strip(b" \t")
        quoted = raw.startswith(b'"')
        if quoted:
            if len(raw) < 2 or not raw.endswith(b'"'):
                raise ValueError("bad quoted header parameter")
            inner = raw[1:-1]
            if b'"' in inner:
                raise ValueError("bad quoted header parameter")
        else:
            if not raw or any(byte not in _TOKEN for byte in raw):
                raise ValueError("bad header parameter value")
            inner = raw
        key = bytes(name).lower()
        if key in params:
            raise ValueError("duplicate header parameter")
        params[key] = (bytes(inner), quoted)
    return main, params


def _split_semicolons(value):
    """Split on semicolons outside of double quotes."""
    pieces = []
    start = 0
    quoted = False
    for index, byte in enumerate(value):
        if byte == 0x22:  # '"'
            quoted = not quoted
        elif byte == 0x3B and not quoted:  # ';'
            pieces.append(value[start:index])
            start = index + 1
    if quoted:
        raise ValueError("unterminated quoted header parameter")
    pieces.append(value[start:])
    return pieces


def parse_boundary(content_type):
    """Extract and validate the ``boundary`` parameter of multipart/mixed."""
    main, params = split_params(content_type)
    if main != b"multipart/mixed":
        raise ValueError("container is not multipart/mixed")
    boundary = params.get(b"boundary")
    if boundary is None:
        raise ValueError("missing boundary parameter")
    boundary, quoted = boundary
    if not quoted:
        raise ValueError("boundary must be quoted")
    if not 1 <= len(boundary) <= 70:
        raise ValueError("boundary length out of range")
    if any(byte not in _BOUNDARY for byte in boundary):
        raise ValueError("invalid boundary character")
    return boundary
