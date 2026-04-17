#!/usr/bin/env python3
"""X4 #300 — Software compliance CLI driver.

Thin shell over ``backend.software_compliance.run_all``. Produces a
single JSON summary on stdout and an exit status reflecting the bundle
verdict, matching the X1/X3 contract:

    0  bundle passed (all gates pass or skipped)
    1  at least one gate failed
    2  caller-side error (bad args, missing path, invalid ecosystem)

Usage
-----
    scripts/software_compliance.py --app-path=./service
    scripts/software_compliance.py --app-path=. --ecosystem=pip \\
        --sbom-format=spdx --sbom-out=./sbom.spdx
    scripts/software_compliance.py --app-path=./api \\
        --cve-fail-on=CRITICAL --allowlist=readline
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.software_compliance.__main__ import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
