#!/usr/bin/env python3
"""Thin launcher for the Gerrit/JIRA bridge daemon."""
from __future__ import annotations

import argparse
import sys

# OP-717 deploy fix: explicit basicConfig so logger.info() lines reach
# stdout. Without this, the root logger defaults to WARNING and the
# proactive-merger 'skip_reason=mergeable' decisions get silently
# dropped — making the daemon look broken when it's actually working.
import logging
import os
logging.basicConfig(
    level=os.environ.get('OMNISIGHT_LOG_LEVEL', 'INFO').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)

from backend.agents.gerrit_jira_bridge import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-class",
        default="subscription-claude",
        help="JIRA/Gerrit bot credential class; default uses claude-bot.",
    )
    args = parser.parse_args(argv)
    return run(args.agent_class)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
