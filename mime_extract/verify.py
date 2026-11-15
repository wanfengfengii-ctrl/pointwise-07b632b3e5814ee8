"""Self-verification for :mod:`mime_extract`.

Running ``python3 -I runtime.py verify`` executes the checks below and exits
non-zero on the first failure.  The suite covers round-trip extraction under
many chunk sizes, every specified rejection path, cleanup semantics, and the
``YWI=`` base64 vector.
"""

from __future__ import annotations

import os
import sys
import tempfile

from . import extract

CRLF = b"\r\n"


# ---------------------------------------------------------------------------
# Message construction helpers
# ---------------------------------------------------------------------------


def build_part(headers, body=b""):
    return headers + CRLF + body


def build_multipart(parts, boundary, preamble=b"", epilogue=b""):
    out = bytearray()
    out += preamble
    for part in parts:
        out += b"--" + boundary + CRLF
        out += part
        out += CRLF
    out += b"--" + boundary + b"--" + CRLF
    out += epilogue
    return bytes(out)


def root_message(body, boundary=b"root", headers=None):
    head = headers or (
        b'Content-Type: multipart/mixed; boundary="' + boundary + b'"' + CRLF
    )
    return head + CRLF + body


def leaf(headers, body=b""):
    return build_part(headers, body)


def attachment(name, body=b"", encoding=b"binary", extra=b""):
    head = (
        b'Content-Disposition: attachment; filename="' + name + b'"' + CRLF
        + b"Content-Type: application/octet-stream" + CRLF
        + b"Content-Transfer-Encoding: " + encoding + CRLF
    )
    return build_part(head + extra, body)


def simple_message(parts, boundary=b"root"):
    return root_message(build_multipart(parts, boundary))


def chunked(data, size):
    for start in range(0, len(data), size):
        yield data[start : start + size]


# ---------------------------------------------------------------------------
# Assertions and test helpers
# ---------------------------------------------------------------------------


def run_extract(raw, chunk_size=7, outdir=None):
    own = outdir is None
    if own:
        outdir = tempfile.mkdtemp(prefix="mime-extract-verify-")
    try:
        result = extract(chunked(raw, chunk_size), outdir)
        contents = []
        for item in result:
            assert set(item) == {"path", "name", "size"}, item
            path = item["path"]
            assert not os.path.isabs(path), path
            assert os.path.isfile(os.path.join(outdir, path)), path
            with open(os.path.join(outdir, path), "rb") as handle:
                data = handle.read()
            assert len(data) == item["size"], (item, len(data))
            contents.append(data)
        return result, contents, outdir
    finally:
        if own:
            import shutil

            shutil.rmtree(outdir, ignore_errors=True)


def expect_value_error(raw, chunk_size=11, note=""):
    outdir = tempfile.mkdtemp(prefix="mime-extract-reject-")
    sentinel = os.path.join(outdir, "keep.txt")
    with open(sentinel, "wb") as handle:
        handle.write(b"original")
    try:
        try:
            extract(chunked(raw, chunk_size), outdir)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError: " + note)
        assert open(sentinel, "rb").read() == b"original", note
        leftovers = sorted(os.listdir(outdir))
        assert leftovers == ["keep.txt"], (note, leftovers)
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------


def test_simple_and_order():
    parts = [
        leaf(b"Content-Type: text/plain" + CRLF
             + b"Content-Transfer-Encoding: binary" + CRLF,
             b"not an attachment"),
        attachment(b"first.bin", b"abc"),
        attachment(b"second.txt", b"ZGVmZw==", b"base64", b"X-Note: ok" + CRLF),
    ]
    raw = simple_message(parts)
    for size in (1, 2, 3, 7, 64, 4096, 32768):
        result, contents, _ = run_extract(raw, size)
        assert [item["name"] for item in result] == ["first.bin", "second.txt"]
        assert contents == [b"abc", b"defg"]
        assert [item["size"] for item in result] == [3, 4]


def test_empty_attachment_kept():
    raw = simple_message([attachment(b"empty.dat", b"")])
    result, contents, _ = run_extract(raw, 5)
    assert len(result) == 1
    assert result[0]["size"] == 0
    assert contents == [b""]


def test_base64_crlf_and_vector():
    body = b"YWJjZGVmZ2hpamtsbW5vcA==\r\n"
    raw = simple_message([attachment(b"b64.dat", body, b"base64")])
    result, contents, _ = run_extract(raw, 3)
    assert contents == [b"abcdefghijklmnop"]
    assert result[0]["size"] == len(contents[0])

    vector = simple_message([attachment(b"vector.bin", b"YWI=", b"base64")])
    _, contents, _ = run_extract(vector, 1)
    assert contents == [b"ab"]

    long_line = b"YWJj" * 250 + b"ZA=="
    raw = simple_message([attachment(b"long.bin", long_line, b"base64")])
    _, contents, _ = run_extract(raw, 32)
    assert contents[0] == b"abc" * 250 + b"d"


def test_nesting_and_preambles():
    inner = build_multipart(
        [
            leaf(b"Content-Type: text/plain" + CRLF
                 + b"Content-Transfer-Encoding: binary" + CRLF,
                 b"child text"),
            attachment(b"nested.bin", b"nested data"),
        ],
        b"inner",
        preamble=b"inner preamble" + CRLF,
        epilogue=b"inner epilogue" + CRLF,
    )
    mid = build_multipart(
        [
            leaf(b'Content-Type: multipart/mixed; boundary="inner"' + CRLF,
                 inner),
        ],
        b"mid",
    )
    outer = build_multipart(
        [
            leaf(b'Content-Type: multipart/mixed; boundary="mid"' + CRLF, mid),
            attachment(b"top.bin", b"top data"),
        ],
        b"outer",
        preamble=b"outer preamble" + CRLF,
        epilogue=b"outer epilogue",
    )
    raw = root_message(outer, b"outer")
    result, contents, _ = run_extract(raw, 4)
    assert [item["name"] for item in result] == ["nested.bin", "top.bin"]
    assert contents == [b"nested data", b"top data"]


def test_deepest_allowed_nesting():
    leaf_part = attachment(b"deep.bin", b"deep")
    level4 = build_multipart([leaf_part], b"b4")
    level3 = build_multipart(
        [leaf(b'Content-Type: multipart/mixed; boundary="b4"' + CRLF, level4)],
        b"b3",
    )
    level2 = build_multipart(
        [leaf(b'Content-Type: multipart/mixed; boundary="b3"' + CRLF, level3)],
        b"b2",
    )
    level1 = build_multipart(
        [leaf(b'Content-Type: multipart/mixed; boundary="b2"' + CRLF, level2)],
        b"b1",
    )
    result, contents, _ = run_extract(root_message(level1, b"b1"), 9)
    assert [item["name"] for item in result] == ["deep.bin"]
    assert contents == [b"deep"]


def test_non_attachment_base64_is_validated():
    good = simple_message(
        [
            leaf(b"Content-Type: text/plain" + CRLF
                 + b"Content-Transfer-Encoding: base64" + CRLF,
                 b"aGVsbG8="),
        ]
    )
    run_extract(good, 2)
    expect_value_error(
        simple_message(
            [
                leaf(b"Content-Type: text/plain" + CRLF
                     + b"Content-Transfer-Encoding: base64" + CRLF,
                     b"aGVsbG8"),
            ]
        ),
        note="unvalidated non-attachment",
    )


def test_binary_long_line_content():
    payload = bytes(range(256)) * 97
    raw = simple_message([attachment(b"binary.bin", payload)])
    _, contents, _ = run_extract(raw, 32768)
    assert contents == [payload]


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


def test_rejections():
    closing = b"--root--" + CRLF
    bad = []

    # Bad header (missing colon).
    bad.append(("no colon", simple_message([b"Broken\r\n\r\n"])))

    # Folded header.
    bad.append(
        (
            "folded",
            simple_message(
                [
                    b"Content-Type: text/plain\r\n Content-Transfer-Encoding: binary\r\n\r\nx"
                ]
            ),
        )
    )

    # Duplicate field.
    bad.append(
        (
            "duplicate",
            simple_message(
                [
                    b"Content-Type: text/plain\r\nContent-Type: text/plain\r\n"
                    b"Content-Transfer-Encoding: binary\r\n\r\nx"
                ]
            ),
        )
    )

    # Missing leaf Content-Type.
    bad.append(
        (
            "missing type",
            simple_message(
                [
                    b"Content-Transfer-Encoding: binary\r\n\r\nx",
                ]
            ),
        )
    )

    # Missing leaf encoding.
    bad.append(
        (
            "missing encoding",
            simple_message([b"Content-Type: text/plain\r\n\r\nx"]),
        )
    )

    # Unsupported encoding.
    bad.append(
        (
            "bad encoding",
            simple_message(
                [
                    b"Content-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n\r\nx"
                ]
            ),
        )
    )

    # Invalid base64 alphabet.
    bad.append(
        (
            "base64 alphabet",
            simple_message([attachment(b"a", b"ab*c", b"base64")]),
        )
    )

    # Base64 length not a multiple of four.
    bad.append(
        (
            "base64 group",
            simple_message([attachment(b"a", b"abc", b"base64")]),
        )
    )

    # Data after padding.
    bad.append(
        (
            "after padding",
            simple_message([attachment(b"a", b"YQ==YQ==", b"base64")]),
        )
    )

    # Padding in the middle.
    bad.append(
        (
            "mid padding",
            simple_message([attachment(b"a", b"YQ==AAAA", b"base64")]),
        )
    )

    # Bare LF inside base64.
    bad.append(
        (
            "bare LF",
            simple_message([attachment(b"a", b"YQ==\n", b"base64")]),
        )
    )

    # Missing closing boundary.
    bad.append(
        (
            "no close",
            root_message(b"--root\r\n" + attachment(b"a", b"x")),
        )
    )

    # Early parent boundary in a nested container.
    early = (
        b"--root\r\n"
        + leaf(
            b'Content-Type: multipart/mixed; boundary="inner"' + CRLF,
            b"--inner\r\n" + attachment(b"a", b"x"),
        )
        + CRLF
        + closing
    )
    bad.append(("early parent", root_message(early)))

    # Malformed quoted filename.
    bad.append(
        (
            "filename unquoted",
            simple_message(
                [
                    b'Content-Disposition: attachment; filename=x.bin\r\n'
                    b"Content-Type: application/octet-stream\r\n"
                    b"Content-Transfer-Encoding: binary\r\n\r\nx"
                ]
            ),
        )
    )

    # Escaped filename.
    bad.append(
        (
            "filename escaped",
            simple_message(
                [
                    b'Content-Disposition: attachment; filename="a\\"b"\r\n'
                    b"Content-Type: application/octet-stream\r\n"
                    b"Content-Transfer-Encoding: binary\r\n\r\nx"
                ]
            ),
        )
    )

    # Header area over 8 KiB.
    huge = b"X-Pad: " + b"a" * 8200 + CRLF
    bad.append(
        (
            "huge header",
            simple_message(
                [
                    huge
                    + b"Content-Type: text/plain\r\n"
                    + b"Content-Transfer-Encoding: binary\r\n\r\nx"
                ]
            ),
        )
    )

    # Container that is not multipart/mixed.
    bad.append(
        (
            "wrong container type",
            root_message(
                b"--root\r\n"
                + leaf(
                    b"Content-Type: multipart/alternative; boundary=\"x\"\r\n",
                    b"--x--\r\n",
                )
                + CRLF
                + closing
            ),
        )
    )

    # Nesting deeper than four levels.
    l4 = build_multipart([attachment(b"a", b"x")], b"b4")
    l3 = build_multipart(
        [leaf(b'Content-Type: multipart/mixed; boundary="b4"' + CRLF, l4)], b"b3"
    )
    l2 = build_multipart(
        [leaf(b'Content-Type: multipart/mixed; boundary="b3"' + CRLF, l3)], b"b2"
    )
    l1 = build_multipart(
        [leaf('Content-Type: multipart/mixed; boundary="b2"\r\n'.encode(), l2)],
        b"b1",
    )
    l0 = build_multipart(
        [leaf('Content-Type: multipart/mixed; boundary="b1"\r\n'.encode(), l1)],
        b"b0",
    )
    bad.append(("too deep", root_message(l0, b"b0")))

    # Boundary longer than 70 characters.
    boundary = b"b" * 71
    bad.append(
        (
            "long boundary",
            b'Content-Type: multipart/mixed; boundary="' + boundary + b'"'
            + CRLF + CRLF
            + b"--" + boundary + b"--" + CRLF,
        )
    )

    for note, raw in bad:
        for size in (1, 5, 17, 32768):
            expect_value_error(raw, size, note)


def test_failure_preserves_directory():
    outdir = tempfile.mkdtemp(prefix="mime-extract-preserve-")
    try:
        with open(os.path.join(outdir, "existing.txt"), "wb") as handle:
            handle.write(b"pre-existing")
        bad = simple_message([attachment(b"a", b"YQ==YQ==", b"base64")])
        try:
            extract(chunked(bad, 3), outdir)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        assert sorted(os.listdir(outdir)) == ["existing.txt"]
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_publish_only_after_validation():
    # The first attachment is valid, the second has a bad encoding; nothing
    # from the failed call may be published.
    raw = simple_message(
        [
            attachment(b"good.bin", b"good"),
            attachment(b"bad.bin", b"YQ==YQ==", b"base64"),
        ]
    )
    expect_value_error(raw, 13, "publish after validation")


def test_existing_names_not_overwritten():
    outdir = tempfile.mkdtemp(prefix="mime-extract-existing-")
    try:
        with open(os.path.join(outdir, "attachment-0001.bin"), "wb") as handle:
            handle.write(b"do not touch")
        raw = simple_message([attachment(b"a.bin", b"new")])
        result = extract(chunked(raw, 4), outdir)
        assert result[0]["path"] == "attachment-0002.bin"
        assert open(
            os.path.join(outdir, "attachment-0001.bin"), "rb"
        ).read() == b"do not touch"
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_large_attachment_streams():
    payload = b"A" * (1 << 20) + b"\r\n" + b"B" * 1024
    raw = simple_message([attachment(b"large.bin", payload)])
    result, contents, _ = run_extract(raw, 32768)
    assert contents == [payload]
    assert result[0]["size"] == len(payload)


def test_single_pass_source():
    """The extractor must consume the chunk iterator exactly once."""
    data = simple_message([attachment(b"b", b"x" * 4096)])
    pieces = 0

    def counted():
        nonlocal pieces
        for chunk in chunked(data, 17):
            pieces += 1
            yield chunk

    outdir = tempfile.mkdtemp(prefix="mime-extract-single-")
    try:
        extract(counted(), outdir)
        assert pieces == (len(data) + 16) // 17
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_base64_direct():
    from ._base64 import Base64Sink

    class Collector:
        def __init__(self):
            self.parts = []

        def write(self, data):
            self.parts.append(data)

        def finish(self):
            pass

    cases = {
        b"": b"",
        b"YWI=": b"ab",
        b"YQ==": b"a",
        b"YWJj": b"abc",
        b"YWJj\r\nZA==": b"abcd",
        b"YWJj\r\nZA": None,
        b"YWJjZA=": None,
        b"YWJjZA===": None,
        b"YWJj=A==": None,
        b"=YWJj": None,
        b"YW Jj": None,
        b"YWJj\nZA==": None,
    }
    for text, expected in cases.items():
        collector = Collector()
        sink = Base64Sink(collector)
        ok = True
        try:
            for start in range(0, len(text), 2):
                sink.write(text[start : start + 2])
            sink.finish()
        except ValueError:
            ok = False
        if expected is None:
            assert not ok, text
        else:
            assert ok, text
            assert b"".join(collector.parts) == expected, text


def test_chunk_split_noise():
    raw = simple_message(
        [
            attachment(b"a.bin", b"YWJj\r\nZA==", b"base64"),
            attachment(b"b.bin", b"\x00\xff\r\n--notroot\r\n" * 50),
        ]
    )
    for size in range(1, 40):
        result, contents, _ = run_extract(raw, size)
        assert contents[0] == b"abcd"
        assert contents[1] == b"\x00\xff\r\n--notroot\r\n" * 50


def test_bad_boundary_parameter():
    bads = [
        b"multipart/mixed; boundary=root",
        b'multipart/mixed; boundary=""',
        b'multipart/mixed; boundary="a b"',
        b"multipart/mixed",
        b'multipart/mixed; boundary="bad-dash"',
    ]
    for value in bads:
        raw = (
            b"Content-Type: " + value + CRLF + CRLF
            + b"--root" + CRLF + attachment(b"a", b"x") + CRLF
            + b"--root--" + CRLF
        )
        expect_value_error(raw, 7, value.decode())


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


TESTS = [
    ("simple and order", test_simple_and_order),
    ("empty attachment", test_empty_attachment_kept),
    ("base64 CRLF and YWI=", test_base64_crlf_and_vector),
    ("nesting and preambles", test_nesting_and_preambles),
    ("deepest nesting", test_deepest_allowed_nesting),
    ("non-attachment validation", test_non_attachment_base64_is_validated),
    ("binary long line", test_binary_long_line_content),
    ("rejections", test_rejections),
    ("failure preserves directory", test_failure_preserves_directory),
    ("publish after validation", test_publish_only_after_validation),
    ("existing names preserved", test_existing_names_not_overwritten),
    ("large attachment", test_large_attachment_streams),
    ("single pass", test_single_pass_source),
    ("base64 direct", test_base64_direct),
    ("chunk split noise", test_chunk_split_noise),
    ("bad boundary parameter", test_bad_boundary_parameter),
]


def main():
    failures = 0
    for name, test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            sys.stderr.write("FAIL %s: %s: %s\n" % (name, type(exc).__name__, exc))
            import traceback

            traceback.print_exc()
        else:
            sys.stdout.write("ok %s\n" % name)
    sys.stdout.write("%d test groups, %d failures\n" % (len(TESTS), failures))
    return 1 if failures else 0
