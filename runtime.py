#!/usr/bin/env python3
"""Repository-root launcher for the runtime manifest.

Inserting this directory explicitly makes ``python3 -I runtime.py ...`` work
from the repository root without installing the package.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mime_extract.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
