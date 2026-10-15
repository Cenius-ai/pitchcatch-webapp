#!/usr/bin/env python3
"""Shim so `python3 main.py` works from the project root.

Pitchcatch's whole implementation lives in ``pitchcatch.py``; this module only
forwards to it.  The packaged console script (``pitchcatch``) points straight at
``pitchcatch:main`` and does not use this shim.
"""

import sys

from pitchcatch import main

if __name__ == "__main__":
    sys.exit(main())
