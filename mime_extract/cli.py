"""Command line entry points used by the runtime manifest."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

from . import extract
from . import verify as _verify


def _chunks(path, size=32 * 1024):
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(size)
            if not chunk:
                return
            yield chunk


def _demo_message():
    CRLF = b"\r\n"
    inner = (
        b'Content-Type: text/plain' + CRLF
        + b'Content-Transfer-Encoding: binary' + CRLF
        + CRLF + b"portal note, not an attachment" + CRLF
        + b"--root" + CRLF
        + b'Content-Disposition: attachment; filename="report.txt"' + CRLF
        + b"Content-Type: text/plain" + CRLF
        + b"Content-Transfer-Encoding: binary" + CRLF
        + CRLF + b"hello from the portal" + CRLF
        + b"--root" + CRLF
        + b'Content-Disposition: attachment; filename="vector.bin"' + CRLF
        + b"Content-Type: application/octet-stream" + CRLF
        + b"Content-Transfer-Encoding: base64" + CRLF
        + CRLF + b"YWI=" + CRLF
        + b"--root" + CRLF
        + b'Content-Disposition: attachment; filename="empty.dat"' + CRLF
        + b"Content-Type: application/octet-stream" + CRLF
        + b"Content-Transfer-Encoding: binary" + CRLF
        + CRLF
    )
    return (
        b'Content-Type: multipart/mixed; boundary="root"' + CRLF
        + CRLF + b"--root" + CRLF + inner + CRLF + b"--root--" + CRLF
    )


def cmd_setup(argv):
    """Confirm the stdlib-only package imports cleanly."""
    del argv
    print("mime_extract: ready (stdlib only, Python %s)" % sys.version.split()[0])
    return 0


def cmd_verify(argv):
    """Run the acceptance suite (chunking and rejection checks included)."""
    del argv
    return _verify.main()


def cmd_demo(argv):
    """Extract a small demonstration message and print the manifest result.

    Optional argv: ``[message-path] [outdir]``.  With no path a built-in
    message is used; with no outdir a temporary directory is used.
    """
    if len(argv) > 2:
        return _usage("demo accepts at most [message-path] [outdir]")
    outdir = None
    own_outdir = False
    if len(argv) == 2:
        outdir = argv[1]
    if len(argv) >= 1:
        source = argv[0]
    else:
        source = None

    if outdir is None:
        outdir = tempfile.mkdtemp(prefix="mime-extract-demo-")
        own_outdir = True
    try:
        if source is None:
            raw = _demo_message()
            result = extract(
                (raw[i : i + 8] for i in range(0, len(raw), 8)), outdir
            )
        else:
            result = extract(_chunks(source), outdir)
    finally:
        if own_outdir:
            shutil.rmtree(outdir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    for item in result:
        print("  %s <- %r, %d bytes" % (item["path"], item["name"], item["size"]))
    return 0


def cmd_memcheck(argv):
    """Measure extraction of a 64 MiB binary long-line attachment."""
    del argv
    import resource
    import tracemalloc

    size = 64 * 1024 * 1024
    outdir = tempfile.mkdtemp(prefix="mime-extract-mem-")
    try:
        head = (
            b'Content-Type: multipart/mixed; boundary="root"\r\n\r\n'
            b"--root\r\n"
            b'Content-Disposition: attachment; filename="big.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: binary\r\n\r\n"
        )
        tail = b"\r\n--root--\r\n"

        def chunks():
            yield head
            block = b"A" * (32 * 1024)
            left = size
            while left:
                take = min(left, len(block))
                yield block[:take]
                left -= take
            yield tail

        tracemalloc.start()
        base_peak = tracemalloc.get_traced_memory()[1]
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result = extract(chunks(), outdir)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        delta_py = (peak - base_peak) / (1024.0 * 1024.0)
        delta_rss = (after - before) / 1024.0
        print(
            "payload=%d bytes attachment=%r"
            % (size, result[0]["path"])
        )
        print(
            "python tracemalloc peak delta=%.3f MiB; ru_maxrss delta=%.3f MiB"
            % (delta_py, delta_rss)
        )
        if delta_py > 8.0 or delta_rss > 8.0:
            print("FAIL: additional peak exceeds 8 MiB", file=sys.stderr)
            return 1
        print("PASS: additional peak <= 8 MiB")
        return 0
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _usage(message):
    print(message, file=sys.stderr)
    print("usage: runtime.py {setup|verify|demo|memcheck} ...", file=sys.stderr)
    return 2


COMMANDS = {
    "setup": cmd_setup,
    "verify": cmd_verify,
    "demo": cmd_demo,
    "memcheck": cmd_memcheck,
}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        return _usage("unknown or missing subcommand")
    command = COMMANDS[argv[0]]
    try:
        return command(argv[1:])
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
