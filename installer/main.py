"""BS.4.1 placeholder entrypoint for the omnisight-installer sidecar.

BS.4.2 fully replaces this file with the long-poll loop against the
backend ``/installer/jobs/poll`` endpoint and protocol-version
handshake. This stub exists so ``Dockerfile.installer`` (BS.4.1) ships
an image that is buildable + runnable today: ``docker run --rm
omnisight-installer`` exits 0 with a clear marker line in stderr
instead of ``ModuleNotFoundError: No module named 'installer'``.
"""

from __future__ import annotations

import sys


def main() -> int:
    sys.stderr.write(
        "omnisight-installer: BS.4.1 stub entrypoint "
        "(Dockerfile.installer shipped; long-poll loop pending BS.4.2)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
