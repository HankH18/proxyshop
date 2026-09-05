"""``python -m proxyshop_demo`` — run the S1 starting-slice demo and narrate it."""

from __future__ import annotations

import sys

from .s1 import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
