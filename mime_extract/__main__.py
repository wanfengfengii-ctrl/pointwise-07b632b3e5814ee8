"""Allow ``python3 -m mime_extract`` from the repository root."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
