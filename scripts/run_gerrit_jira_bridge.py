#!/usr/bin/env python3
"""Thin launcher for the Gerrit/JIRA bridge daemon."""
from __future__ import annotations

import argparse
import sys

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
