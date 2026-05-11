#!/usr/bin/env python3
"""OP-869 prod env config drift detector."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.env_config import ENV_ROOT, EnvConfigDrift, load_config_file


DEFAULT_RUNNING_CONFIG = Path("/run/omnisight/prod/config.yaml")
DEFAULT_GIT_CONFIG = ENV_ROOT / "prod" / "config.yaml"


def load_runtime_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        raw = json.loads(text)
    else:
        raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping")
    return raw


def normalize_config(path: Path) -> dict[str, Any]:
    config = load_config_file(path, expected_env="prod")
    return config.model_dump(mode="json")


def detect_drift(
    *,
    running_config: Path = DEFAULT_RUNNING_CONFIG,
    git_config: Path = DEFAULT_GIT_CONFIG,
) -> list[str]:
    running = load_runtime_mapping(running_config)
    expected = normalize_config(git_config)
    diffs: list[str] = []
    for key in sorted(set(running) | set(expected)):
        if running.get(key) != expected.get(key):
            diffs.append(f"{key}: running={running.get(key)!r} git={expected.get(key)!r}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--running-config", type=Path, default=DEFAULT_RUNNING_CONFIG)
    parser.add_argument("--git-config", type=Path, default=DEFAULT_GIT_CONFIG)
    args = parser.parse_args(argv)

    try:
        diffs = detect_drift(
            running_config=args.running_config,
            git_config=args.git_config,
        )
    except Exception as exc:
        print(f"EnvConfigDrift: unable to compare config: {exc}", file=sys.stderr)
        return 2

    if not diffs:
        print("OK: prod running config matches deploy/env/prod/config.yaml")
        return 0

    err = EnvConfigDrift("EnvConfigDrift: prod running config differs from git")
    print(str(err), file=sys.stderr)
    for diff in diffs:
        print(f"  {diff}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
