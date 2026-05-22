#!/usr/bin/env python3
"""OP-1480 image retention policy enforcer for container package versions."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen


RELEASE_RE = re.compile(r"^v\d+\.\d+\.\d+$")
RC_RE = re.compile(r"^(v\d+\.\d+\.\d+)-rc\d+$")
HOTFIX_RE = re.compile(r"^v\d+\.\d+\.\d+-hotfix-\d+$")
DEVELOP_RE = re.compile(r"^develop-[0-9a-f]{12}$")
FEATURE_RE = re.compile(r"^feature-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^sha-[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
MUTABLE_ALIASES = {"canary", "staging", "prod", "latest", "develop-latest"}


@dataclass(frozen=True)
class PackageVersion:
    version_id: int | str
    tags: tuple[str, ...]
    updated_at: datetime
    digest: str | None = None


@dataclass(frozen=True)
class RetentionDecision:
    version_id: int | str
    tags: tuple[str, ...]
    updated_at: datetime
    digest: str | None
    tag_class: str
    action: str
    reason: str

    @property
    def delete(self) -> bool:
        return self.action == "delete"


def parse_timestamp(value: str) -> datetime:
    """Parse the GitHub REST API's ISO-8601 timestamps as UTC datetimes."""
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_versions(payload: Iterable[dict[str, Any]]) -> list[PackageVersion]:
    versions: list[PackageVersion] = []
    for row in payload:
        metadata = row.get("metadata") or {}
        container = metadata.get("container") or {}
        tags = row.get("tags", container.get("tags", [])) or []
        versions.append(
            PackageVersion(
                version_id=int(row["id"]),
                tags=tuple(str(tag) for tag in tags),
                updated_at=parse_timestamp(str(row["updated_at"])),
                digest=_normalize_digest(row.get("name") or row.get("digest")),
            )
        )
    return versions


def normalize_gitlab_tags(payload: Iterable[dict[str, Any]]) -> list[PackageVersion]:
    versions: list[PackageVersion] = []
    for row in payload:
        tag_name = str(row["name"])
        updated_at = row.get("updated_at") or row.get("created_at")
        if updated_at is None:
            raise ValueError(
                f"GitLab registry tag {tag_name!r} has no updated_at/created_at"
            )
        versions.append(
            PackageVersion(
                version_id=tag_name,
                tags=(tag_name,),
                updated_at=parse_timestamp(str(updated_at)),
                digest=_normalize_digest(row.get("digest") or row.get("revision")),
            )
        )
    return versions


def _normalize_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = DIGEST_RE.fullmatch(value.strip().lower())
    return match.group(0) if match else None


def _json_documents(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    docs: list[Any] = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        doc, idx = decoder.raw_decode(text, idx)
        docs.append(doc)
    return docs


def parse_gh_api_output(text: str) -> list[dict[str, Any]]:
    """Parse ``gh api --paginate`` output, flattening one or more pages."""
    rows: list[dict[str, Any]] = []
    for doc in _json_documents(text):
        if isinstance(doc, list):
            rows.extend(row for row in doc if isinstance(row, dict))
        elif isinstance(doc, dict):
            rows.append(doc)
    return rows


def _version_tag_class(version: PackageVersion) -> str:
    tags = set(version.tags)
    if not tags:
        return "untagged"
    if any(RELEASE_RE.fullmatch(tag) for tag in tags):
        return "release"
    if any(HOTFIX_RE.fullmatch(tag) for tag in tags):
        return "hotfix"
    if tags & MUTABLE_ALIASES:
        return "mutable-alias"
    if any(RC_RE.fullmatch(tag) for tag in tags):
        return "release-candidate"
    if any(SHA_RE.fullmatch(tag) for tag in tags):
        return "sha"
    if any(DEVELOP_RE.fullmatch(tag) for tag in tags):
        return "develop"
    if any(FEATURE_RE.fullmatch(tag) for tag in tags):
        return "feature"
    return "unknown-tagged"


def build_release_ship_times(versions: Iterable[PackageVersion]) -> dict[str, datetime]:
    shipped: dict[str, datetime] = {}
    for version in versions:
        for tag in version.tags:
            if RELEASE_RE.fullmatch(tag):
                current = shipped.get(tag)
                if current is None or version.updated_at > current:
                    shipped[tag] = version.updated_at
    return shipped


def retention_decisions(
    versions: Iterable[PackageVersion],
    *,
    now: datetime | None = None,
    protected_digests: Iterable[str] = (),
    sha_keep_days: int = 14,
    develop_keep_days: int = 14,
    develop_min_keep: int = 30,
    feature_keep_days: int = 7,
    untagged_keep_days: int = 7,
    rc_without_parent_days: int = 30,
    rc_after_parent_days: int = 90,
) -> list[RetentionDecision]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    versions_list = list(versions)
    protected_digest_set = {_normalize_digest(digest) for digest in protected_digests}
    protected_digest_set.discard(None)
    shipped = build_release_ship_times(versions_list)
    develop_floor = {
        version.version_id
        for version in sorted(
            (v for v in versions_list if _version_tag_class(v) == "develop"),
            key=lambda v: v.updated_at,
            reverse=True,
        )[:develop_min_keep]
    }

    decisions: list[RetentionDecision] = []
    for version in sorted(versions_list, key=lambda v: v.updated_at, reverse=True):
        tag_class = _version_tag_class(version)
        age = now - version.updated_at
        action = "keep"
        reason = "conservative keep"

        if version.digest is not None and version.digest in protected_digest_set:
            reason = "digest referenced by release_train/release_audit protection input"
        elif tag_class in {"release", "hotfix"}:
            reason = "immutable release-line tag"
        elif tag_class == "mutable-alias":
            reason = "mutable alias points at live storage"
        elif tag_class == "unknown-tagged":
            reason = "unknown tag class; manual review before deletion"
        elif tag_class == "release-candidate":
            parent_tags = {
                match.group(1)
                for tag in version.tags
                for match in [RC_RE.fullmatch(tag)]
                if match is not None
            }
            parent_ship_times = [shipped[tag] for tag in parent_tags if tag in shipped]
            if parent_ship_times:
                newest_parent = max(parent_ship_times)
                if now - newest_parent > timedelta(days=rc_after_parent_days):
                    action = "delete"
                    reason = f"parent release shipped >{rc_after_parent_days}d ago"
                else:
                    reason = f"parent release shipped within {rc_after_parent_days}d"
            elif age > timedelta(days=rc_without_parent_days):
                action = "delete"
                reason = f"release candidate has no parent after {rc_without_parent_days}d"
            else:
                reason = f"release candidate has no parent yet and is <={rc_without_parent_days}d old"
        elif tag_class == "develop":
            if version.version_id in develop_floor:
                reason = f"within newest {develop_min_keep} develop tags"
            elif age > timedelta(days=develop_keep_days):
                action = "delete"
                reason = f"develop tag older than {develop_keep_days}d and outside newest {develop_min_keep}"
            else:
                reason = f"develop tag <={develop_keep_days}d old"
        elif tag_class == "feature":
            if age > timedelta(days=feature_keep_days):
                action = "delete"
                reason = f"feature tag older than {feature_keep_days}d"
            else:
                reason = f"feature tag <={feature_keep_days}d old"
        elif tag_class == "sha":
            if age > timedelta(days=sha_keep_days):
                action = "delete"
                reason = f"unpromoted sha tag older than {sha_keep_days}d"
            else:
                reason = f"unpromoted sha tag <={sha_keep_days}d old"
        elif tag_class == "untagged":
            if age > timedelta(days=untagged_keep_days):
                action = "delete"
                reason = f"orphaned untagged version older than {untagged_keep_days}d"
            else:
                reason = f"orphaned untagged version <={untagged_keep_days}d old"

        decisions.append(
            RetentionDecision(
                version_id=version.version_id,
                tags=version.tags,
                updated_at=version.updated_at,
                digest=version.digest,
                tag_class=tag_class,
                action=action,
                reason=reason,
            )
        )

    return decisions


def render_decision_table(package: str, decisions: Iterable[RetentionDecision]) -> str:
    rows = list(decisions)
    lines = [
        f"package={package} versions={len(rows)} candidates_to_delete={sum(row.delete for row in rows)}",
        "version_id\tupdated_at\ttag_class\taction\ttags\tdigest\treason",
    ]
    for row in rows:
        tags = ",".join(row.tags) if row.tags else "<untagged>"
        lines.append(
            "\t".join(
                [
                    str(row.version_id),
                    row.updated_at.isoformat().replace("+00:00", "Z"),
                    row.tag_class,
                    row.action,
                    tags,
                    row.digest or "",
                    row.reason,
                ]
            )
        )
    return "\n".join(lines)


def _run(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True)


def infer_owner_from_git_remote() -> str | None:
    try:
        remote = _run(["git", "config", "--get", "remote.origin.url"]).strip()
    except subprocess.CalledProcessError:
        return None
    if remote.startswith("git@") and ":" in remote:
        path = remote.split(":", 1)[1]
    elif "://" in remote:
        path = remote.rsplit("/", 2)[-2] + "/" + remote.rsplit("/", 1)[-1]
    else:
        path = remote
    parts = path.removesuffix(".git").split("/")
    return parts[-2] if len(parts) >= 2 else None


def discover_api_root(owner: str) -> str:
    owner_lc = owner.lower()
    try:
        _run(["gh", "api", f"users/{owner_lc}", "--silent"])
        return f"users/{owner_lc}"
    except subprocess.CalledProcessError:
        return f"orgs/{owner_lc}"


def fetch_versions(api_root: str, package: str) -> list[PackageVersion]:
    output = _run(
        [
            "gh",
            "api",
            "--paginate",
            f"{api_root}/packages/container/{package}/versions",
        ]
    )
    return normalize_versions(parse_gh_api_output(output))


def _read_gitlab_token(path: Path | None) -> str:
    token_path = path or Path(
        os.environ.get(
            "OMNISIGHT_GITLAB_TOKEN_FILE",
            "~/.config/omnisight/gitlab-claude-token",
        )
    ).expanduser()
    return token_path.read_text(encoding="utf-8").strip()


def _gitlab_json(url: str, token: str) -> Any:
    req = Request(url, headers={"PRIVATE-TOKEN": token, "Accept": "application/json"})
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gitlab_json_pages(url: str, token: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page=100&page={page}"
        payload = _gitlab_json(page_url, token)
        if not isinstance(payload, list):
            raise RuntimeError(
                f"GitLab paginated endpoint returned {type(payload).__name__}"
            )
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < 100:
            return rows
        page += 1


def _encode_gitlab_project_path(project_path: str) -> str:
    if "%2f" in project_path.lower():
        return project_path
    return quote(project_path, safe="")


def fetch_gitlab_versions(
    *,
    api_url: str,
    project_path: str,
    repository: str,
    token: str,
) -> tuple[int | str, list[PackageVersion]]:
    base = api_url.rstrip("/")
    encoded_project = _encode_gitlab_project_path(project_path)
    repos = _gitlab_json_pages(
        _gitlab_repositories_url(base, encoded_project),
        token,
    )
    repo = next(
        (
            row for row in repos
            if str(row.get("name", "")) == repository
            or str(row.get("path", "")).endswith(f"/{repository}")
        ),
        None,
    )
    if repo is None:
        raise RuntimeError(
            f"GitLab registry repository not found for package={repository!r}"
        )
    tags_url = (
        f"{base}/api/v4/projects/{encoded_project}/registry/repositories/"
        f"{repo['id']}/tags"
    )
    return repo["id"], normalize_gitlab_tags(_gitlab_json_pages(tags_url, token))


def _gitlab_repositories_url(api_url: str, encoded_project: str) -> str:
    return f"{api_url}/api/v4/projects/{encoded_project}/registry/repositories"


def delete_versions(api_root: str, package: str, decisions: Iterable[RetentionDecision]) -> None:
    for decision in decisions:
        if decision.delete:
            print(
                f"deleting version_id={decision.version_id} "
                f"tags={','.join(decision.tags) or '<untagged>'}"
            )
            _run(
                [
                    "gh",
                    "api",
                    "-X",
                    "DELETE",
                    f"{api_root}/packages/container/{package}/versions/{decision.version_id}",
                ]
            )


def delete_gitlab_versions(
    *,
    api_url: str,
    project_path: str,
    repository_id: int | str,
    token: str,
    decisions: Iterable[RetentionDecision],
) -> None:
    base = api_url.rstrip("/")
    encoded_project = _encode_gitlab_project_path(project_path)
    for decision in decisions:
        if decision.delete:
            tag = str(decision.version_id)
            print(f"deleting gitlab_tag={tag} tags={','.join(decision.tags)}")
            req = Request(
                f"{base}/api/v4/projects/{encoded_project}/registry/repositories/"
                f"{repository_id}/tags/{quote(tag, safe='')}",
                headers={"PRIVATE-TOKEN": token},
                method="DELETE",
            )
            with urlopen(req, timeout=30):
                pass


def protected_digests_from_json_documents(paths: Iterable[Path]) -> set[str]:
    protected: set[str] = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            documents = _json_documents(text)
        except json.JSONDecodeError:
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        for doc in documents:
            protected.update(_digests_in_value(doc))
    return protected


def _digests_in_value(value: Any) -> set[str]:
    if isinstance(value, str):
        found = set(DIGEST_RE.findall(value.lower()))
        stripped = value.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                found.update(_digests_in_value(json.loads(stripped)))
            except json.JSONDecodeError:
                pass
        return found
    if isinstance(value, dict):
        found: set[str] = set()
        for item in value.values():
            found.update(_digests_in_value(item))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for item in value:
            found.update(_digests_in_value(item))
        return found
    return set()


def load_versions_from_file(path: Path) -> list[PackageVersion]:
    return normalize_versions(json.loads(path.read_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        default=os.environ.get("PACKAGE_NAME"),
        required=os.environ.get("PACKAGE_NAME") is None,
    )
    parser.add_argument("--owner", default=os.environ.get("OWNER"))
    parser.add_argument(
        "--registry",
        choices=("ghcr", "gitlab"),
        default=os.environ.get("IMAGE_REGISTRY", "ghcr"),
    )
    parser.add_argument(
        "--gitlab-api-url",
        default=os.environ.get(
            "OMNISIGHT_GITLAB_API_URL",
            "http://sora.services:49154",
        ),
    )
    parser.add_argument(
        "--gitlab-project-path",
        default=os.environ.get(
            "OMNISIGHT_GITLAB_PROJECT_PATH",
            "omnisight/OmniSight-Productizer",
        ),
    )
    parser.add_argument("--gitlab-token-file", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN") == "1",
    )
    parser.add_argument(
        "--input-json",
        type=Path,
        help="Read package versions from a JSON fixture instead of GHCR.",
    )
    parser.add_argument(
        "--protected-digests-json",
        type=Path,
        action="append",
        default=[],
        help=(
            "JSON/JSONL release_train or release_audit export whose "
            "sha256 digests must be kept."
        ),
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        help="Write the per-tag decision table to this path.",
    )
    parser.add_argument(
        "--sha-keep-days",
        type=int,
        default=int(os.environ.get("SHA_KEEP_DAYS", "14")),
    )
    parser.add_argument(
        "--develop-keep-days",
        type=int,
        default=int(os.environ.get("DEVELOP_KEEP_DAYS", "14")),
    )
    parser.add_argument(
        "--develop-min-keep",
        type=int,
        default=int(os.environ.get("DEVELOP_MIN_KEEP", "30")),
    )
    args = parser.parse_args(argv)

    if args.input_json:
        payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        versions = normalize_gitlab_tags(payload) if args.registry == "gitlab" else normalize_versions(payload)
        api_root = None
        gitlab_repository_id = None
        gitlab_token = None
    elif args.registry == "gitlab":
        gitlab_token = _read_gitlab_token(args.gitlab_token_file)
        gitlab_repository_id, versions = fetch_gitlab_versions(
            api_url=args.gitlab_api_url,
            project_path=args.gitlab_project_path,
            repository=args.package,
            token=gitlab_token,
        )
        api_root = None
    else:
        owner = args.owner or infer_owner_from_git_remote()
        if not owner:
            parser.error("--owner, OWNER, or git remote origin owner is required unless --input-json is used")
        api_root = discover_api_root(owner)
        versions = fetch_versions(api_root, args.package)
        gitlab_repository_id = None
        gitlab_token = None

    protected_digests = protected_digests_from_json_documents(args.protected_digests_json)
    decisions = retention_decisions(
        versions,
        protected_digests=protected_digests,
        sha_keep_days=args.sha_keep_days,
        develop_keep_days=args.develop_keep_days,
        develop_min_keep=args.develop_min_keep,
    )
    table = render_decision_table(args.package, decisions)
    print(table)
    if args.summary_file:
        args.summary_file.parent.mkdir(parents=True, exist_ok=True)
        args.summary_file.write_text(table + "\n")

    if args.dry_run:
        return 0
    if args.registry == "gitlab":
        if args.input_json:
            print("--input-json supplied without --dry-run; refusing to delete", file=sys.stderr)
            return 2
        delete_gitlab_versions(
            api_url=args.gitlab_api_url,
            project_path=args.gitlab_project_path,
            repository_id=gitlab_repository_id,
            token=gitlab_token or "",
            decisions=decisions,
        )
        return 0
    if api_root is None:
        print("--input-json supplied without --dry-run; refusing to delete", file=sys.stderr)
        return 2
    delete_versions(api_root, args.package, decisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
