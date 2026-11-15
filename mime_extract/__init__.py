"""Streaming MIME attachment extraction using only the standard library.

Public API
----------
``extract(chunks, outdir)`` consumes a single-pass iterable of byte chunks
(each at most 32 KiB) and writes every ``Content-Disposition: attachment``
leaf into an existing directory.  It returns dictionaries in encounter
order::

    [{"path": "attachment-0001.bin", "name": "original.txt", "size": 12}]

``path`` is a generated path relative to ``outdir``; ``name`` is the
original filename metadata only and is never used to name a file; ``size``
is the number of decoded bytes.  Attachments with an empty body are kept.

Attachments are decoded incrementally into a private staging directory and
published only after the whole message has been validated.  Any failure
removes this call's staging and published files while leaving pre-existing
directory contents untouched.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from ._parser import Parser
from ._reader import _Reader

__all__ = ["extract"]

_ATTACHMENT_PREFIX = "attachment-"


def extract(chunks, outdir):
    """Extract MIME attachments from ``chunks`` into ``outdir``.

    Raises ValueError for malformed MIME structure, headers or encodings.
    Nothing created by a failed call remains in ``outdir``.
    """
    outdir = os.fspath(outdir)
    if not os.path.isdir(outdir):
        raise ValueError("outdir must be an existing directory")

    staging = tempfile.mkdtemp(prefix=".mime-extract-", dir=outdir)
    published = []
    try:
        parser = Parser(_Reader(chunks), staging)
        attachments = parser.parse()

        results = []
        reserved = set()
        for attachment in attachments:
            relpath = _free_name(outdir, reserved)
            reserved.add(relpath)
            final_path = os.path.join(outdir, relpath)
            os.replace(attachment.stage_path, final_path)
            published.append(final_path)
            results.append(
                {
                    "path": relpath,
                    "name": attachment.name,
                    "size": attachment.size,
                }
            )
        return results
    except BaseException:
        for path in published:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _free_name(outdir, reserved):
    """Return the first unused ``attachment-NNNN.bin`` relative path."""
    number = 1
    while True:
        relpath = "%s%04d.bin" % (_ATTACHMENT_PREFIX, number)
        if relpath not in reserved and not os.path.lexists(
            os.path.join(outdir, relpath)
        ):
            return relpath
        number += 1
