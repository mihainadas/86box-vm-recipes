#!/usr/bin/env python3
"""Record a synthetic 86Box invocation for the macOS launcher contract test."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


capture_path = Path(os.environ["MOCK_86BOX_CAPTURE"])
with capture_path.open("a", encoding="utf-8") as capture_file:
    json.dump({"pid": os.getpid(), "argv": sys.argv[1:]}, capture_file)
    capture_file.write("\n")

raise SystemExit(int(os.environ.get("MOCK_86BOX_EXIT", "0")))
