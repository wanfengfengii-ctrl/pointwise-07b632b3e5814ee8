# mime_extract

Standard-library-only, single-pass, bounded-memory extraction of MIME
attachments from a chunked email stream.

```python
from mime_extract import extract

result = extract(chunks, outdir)
# [{"path": "attachment-0001.bin", "name": "report.txt", "size": 1234}, ...]
```

`chunks` is any iterable of `bytes`-like objects (contract: at most 32 KiB
per chunk); it is consumed exactly once and never rewound. `outdir` must
already exist. The return value lists attachments in encounter order. Each
dict has:

* `path` — generated path relative to `outdir`;
* `name` — the original `filename` metadata (never used to name a file);
* `size` — number of decoded bytes.

Empty attachments are kept as zero-byte files. Paths are generated as
`attachment-0001.bin`, `attachment-0002.bin`, …; any name already present
in `outdir` is skipped so pre-existing content is never overwritten.

## Accepted input

* CRLF structural lines; root and every container are `multipart/mixed`
  with a quoted `boundary` parameter of 1–70 `[A-Za-z0-9_]` characters.
  Nesting, including the root, is limited to four levels.
* Per-part header area at most 8 KiB, no folded (continuation) lines, no
  duplicate field names (compared case-insensitively).
* Leaf parts must carry `Content-Type` and `Content-Transfer-Encoding`.
  The only encodings accepted are `binary` and `base64`.
* `Content-Disposition: attachment; filename="..."` marks an attachment;
  the filename must be an unescaped ASCII quoted string. Everything else
  (text parts, `inline` parts, preambles, epilogues) is skipped, but its
  structure and encoding are still validated.
* Delimiter lines are exactly `--boundary` or `--boundary--` followed by
  CRLF. The CRLF immediately before a delimiter is framing, not body
  content, so an empty attachment body is represented by two consecutive
  CRLFs after the last header line.
* Base64 bodies ignore CRLF and nothing else. The alphabet is checked, the
  data must form complete four-character groups, `=` may only pad the final
  group, and no byte may follow padding. `YWI=` decodes to `ab`.

`ValueError` is raised for a bad header, an unsupported or malformed
encoding, a missing closing boundary, or an ancestor boundary that appears
before the current container's close.

## Failure semantics

Attachment bodies are decoded incrementally into a private staging
directory inside `outdir`. A call publishes (`os.replace`) its files only
after the entire message has been parsed and validated. If anything raises,
the staging directory and any files already published by that call are
removed, leaving pre-existing directory entries untouched.

## Memory

Bodies are never accumulated in memory. A sliding window of a few dozen
bytes finds delimiters; binary data is copied straight to the staging file
in bounded pieces, and base64 is validated/decoded block by block. The
`memcheck` command measures a 64 MiB single-line binary attachment:

```
payload=67108864 bytes attachment='attachment-0001.bin'
python tracemalloc peak delta=0.194 MiB; ru_maxrss delta=0.000 MiB
PASS: additional peak <= 8 MiB
```

## Commands

`.pointwise-runtime.json` declares the runtime manifest:

| field | command |
| --- | --- |
| `setup` | `python3 -I runtime.py setup` |
| `verify` | `python3 -I runtime.py verify` |
| `demo` | `python3 -I runtime.py demo` |

`verify` runs the chunking and rejection suite (see `mime_extract/verify.py`)
and exits non-zero on any failure. `demo` extracts a built-in message, or
`python3 -I runtime.py demo MESSAGE [OUTDIR]` for a supplied file.
`python3 -I runtime.py memcheck` reproduces the memory measurement above.

## Layout

* `mime_extract/__init__.py` — public `extract` and atomic publication;
* `mime_extract/_parser.py` — recursive multipart parser;
* `mime_extract/_reader.py` — bounded reader and output sinks;
* `mime_extract/_headers.py` — strict CRLF header/parameter parsing;
* `mime_extract/_base64.py` — incremental strict base64 decoder;
* `mime_extract/verify.py` — acceptance suite;
* `mime_extract/cli.py`, `runtime.py` — CLI used by the manifest.

## Status

Implemented and verified. `python3 -I runtime.py verify` reports
`16 test groups, 0 failures`; the suite exercises chunk sizes down to one
byte, randomized nested streams, all rejection paths, cleanup preservation,
and that a failed call publishes nothing. `memcheck` passes the ≤8 MiB
additional-Python-peak requirement for a 64 MiB binary long-line
attachment.
