"""Entry point of the frozen ``stadiacast`` executable: the receiver CLI, like ``python -m stadiacast``."""

import sys

from stadiacast.cli import main

if __name__ == "__main__":
    sys.exit(main())
