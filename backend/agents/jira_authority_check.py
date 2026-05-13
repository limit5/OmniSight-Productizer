"""JIRA changelog authority checks for runner TOCTOU boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

BOT_IDENTIFIERS = frozenset({"claude-bot", "codex-bot", "merger-bot", "sora-bot"})
DEFAULT_ROSTER_PATH = Path("~/.config/omnisight/operator-deputy-roster.yaml").expanduser()


@dataclass(frozen=True)
class AuthorityAuthor:
    account_id: str
    display_name: str
    kind: str
    level: str


@dataclass(frozen=True)
class AuthorityChange:
    field: str
    old: str
    new: str
    author: AuthorityAuthor
    created: str


def _author_tokens(author: dict[str, Any]) -> set[str]:
    return {
        value.strip().lower()
        for key in ("accountId", "displayName", "name", "emailAddress")
        if isinstance((value := author.get(key)), str) and value.strip()
    }


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _iter_strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _iter_strings(inner)


def _load_roster(path: Path = DEFAULT_ROSTER_PATH) -> dict[str, set[str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {"L1": set(), "L2": set()}
    if not isinstance(raw, dict):
        return {"L1": set(), "L2": set()}

    out = {"L1": set(), "L2": set()}

    def visit(key_path: str, value: Any) -> None:
        key_l = key_path.lower()
        level = (
            "L1"
            if ("l1" in key_l or "top" in key_l or "owner" in key_l)
            else "L2"
            if ("l2" in key_l or "deput" in key_l)
            else None
        )
        if level:
            out[level].update(s.strip().lower() for s in _iter_strings(value) if s.strip())
        if isinstance(value, dict):
            for key, inner in value.items():
                visit(f"{key_path}.{key}", inner)
        elif isinstance(value, list):
            for idx, inner in enumerate(value):
                visit(f"{key_path}.{idx}", inner)

    for key, value in raw.items():
        visit(str(key), value)
    return out


def classify_author(
    author: dict[str, Any],
    *,
    roster_path: Path = DEFAULT_ROSTER_PATH,
) -> AuthorityAuthor:
    tokens = _author_tokens(author)
    account_id = str(author.get("accountId") or "")
    display_name = str(author.get("displayName") or account_id)
    if any(bot in token for token in tokens for bot in BOT_IDENTIFIERS):
        return AuthorityAuthor(account_id, display_name, "automation", "L3")

    roster = _load_roster(roster_path)
    level = "L1" if tokens & roster["L1"] else "L2" if tokens & roster["L2"] else "unknown"
    return AuthorityAuthor(account_id, display_name, "human", level)


def _normalise_field(item: dict[str, Any]) -> str:
    return str(item.get("fieldId") or item.get("field") or "").strip().lower()


def latest_authority_change(
    client: Any,
    key: str,
    *,
    roster_path: Path = DEFAULT_ROSTER_PATH,
) -> AuthorityChange | None:
    from backend.agents import jira_dispatch

    response = jira_dispatch._request(client, "GET", f"/issue/{key}/changelog")
    histories = response.get("values") if isinstance(response, dict) else None
    if not isinstance(histories, list):
        return None

    latest: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    for history in histories:
        if not isinstance(history, dict):
            continue
        created = history.get("created") if isinstance(history.get("created"), str) else ""
        for item in history.get("items") or ():
            if isinstance(item, dict) and _normalise_field(item) in {"assignee", "status"}:
                if latest is None or created >= latest[0]:
                    latest = (created, history, item)
    if latest is None:
        return None

    created, history, item = latest
    return AuthorityChange(
        field=_normalise_field(item),
        old=str(item.get("fromString") or item.get("from") or ""),
        new=str(item.get("toString") or item.get("to") or ""),
        author=classify_author(history.get("author") or {}, roster_path=roster_path),
        created=created,
    )
