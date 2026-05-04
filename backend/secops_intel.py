"""BP.I.1 -- SecOps threat-intel skill helpers.

This module is the first, standalone slice of the SecOps Threat Intel
Agent. It deliberately stops at three callable helpers:

* ``search_latest_cve`` -- query NVD CVE API 2.0 for recent CVEs.
* ``query_zero_day_feeds`` -- query the CISA Known Exploited
  Vulnerabilities feed as the exploited-in-the-wild signal.
* ``fetch_latest_best_practices`` -- return source-backed hardening
  guidance filtered by topic.

Out of scope for BP.I.1: guild scaffolding, pre-install/pre-blueprint
hooks, Renovate/secret-scanning integration, and the full BP.I.5 test
matrix.

Module-global state audit (SOP Step 1, 2026-04-21 rule)
-------------------------------------------------------
Only immutable constants live at module scope. Each networked helper
creates a fresh ``httpx.Client`` unless tests inject one, and returns a
plain dict. Cross-worker consistency is moot because no mutable
module-level cache, singleton, or in-memory registry is read or written.

Read-after-write audit (SOP Step 1, 2026-04-21 rule)
---------------------------------------------------
N/A -- these helpers perform outbound reads only and do not write to
PG, Redis, filesystem state, or module globals.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Literal

import httpx

logger = logging.getLogger(__name__)


IntelKind = Literal["cve", "zero_day", "best_practice"]

NVD_CVES_20_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_LIMIT = 10
DEFAULT_LOOKBACK_DAYS = 14

SEVERITY_ORDER: tuple[str, ...] = (
    "UNKNOWN",
    "LOW",
    "MEDIUM",
    "HIGH",
    "CRITICAL",
)


@dataclass(frozen=True)
class IntelItem:
    """One normalized threat-intel item for agent consumption."""

    id: str
    title: str
    source: str
    url: str = ""
    severity: str = "UNKNOWN"
    published_at: str = ""
    updated_at: str = ""
    summary: str = ""
    affected: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IntelReport:
    """Uniform envelope returned by all BP.I.1 helpers."""

    kind: IntelKind
    query: str = ""
    source: str = ""
    fetched_at: str = ""
    items: list[IntelItem] = field(default_factory=list)
    error: str = ""

    @property
    def total_items(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "query": self.query,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "total_items": self.total_items,
            "items": [item.to_dict() for item in self.items],
            "error": self.error,
        }


HttpClientFactory = Callable[..., httpx.Client]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime | None = None) -> str:
    return (now or _utcnow()).astimezone(timezone.utc).isoformat()


def _normalise_severity(raw: Any) -> str:
    if raw is None:
        return "UNKNOWN"
    value = str(raw).strip().upper()
    if value == "MODERATE":
        value = "MEDIUM"
    return value if value in SEVERITY_ORDER else "UNKNOWN"


def _nvd_timestamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    client_factory: HttpClientFactory | None = None,
) -> tuple[dict[str, Any] | None, str]:
    factory = client_factory or httpx.Client
    try:
        with factory(timeout=timeout_s) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json(), ""
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("secops intel fetch failed url=%s error=%s", url, exc)
        return None, f"{type(exc).__name__}: {exc}"


def _pick_english_description(entries: Iterable[dict[str, Any]]) -> str:
    fallback = ""
    for entry in entries:
        text = str(entry.get("value") or "")
        if not fallback:
            fallback = text
        if str(entry.get("lang") or "").lower() == "en":
            return text
    return fallback


def _cvss_severity(cve: dict[str, Any]) -> str:
    metrics = cve.get("metrics") or {}
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        values = metrics.get(key) or []
        if not values:
            continue
        data = values[0].get("cvssData") or {}
        sev = values[0].get("baseSeverity") or data.get("baseSeverity")
        normalised = _normalise_severity(sev)
        if normalised != "UNKNOWN":
            return normalised
    return "UNKNOWN"


def _nvd_item(entry: dict[str, Any]) -> IntelItem:
    cve = entry.get("cve") or {}
    cve_id = str(cve.get("id") or "")
    refs = [
        str(ref.get("url") or "")
        for ref in (cve.get("references") or {}).get("referenceData") or []
        if ref.get("url")
    ]
    configurations = cve.get("configurations") or []
    affected: list[str] = []
    for cfg in configurations:
        for node in cfg.get("nodes") or []:
            for match in node.get("cpeMatch") or []:
                criteria = str(match.get("criteria") or "")
                if criteria:
                    affected.append(criteria)
    summary = _pick_english_description(cve.get("descriptions") or [])
    return IntelItem(
        id=cve_id,
        title=cve_id,
        source="nvd",
        url=f"https://nvd.nist.gov/vuln/detail/{cve_id}" if cve_id else "",
        severity=_cvss_severity(cve),
        published_at=str(cve.get("published") or ""),
        updated_at=str(cve.get("lastModified") or ""),
        summary=summary,
        affected=affected[:10],
        references=refs[:10],
        tags=["cve"],
    )


def search_latest_cve(
    query: str = "",
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
    severity: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    client_factory: HttpClientFactory | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Search recent CVEs via NVD CVE API 2.0.

    ``query`` maps to NVD ``keywordSearch``. ``days`` bounds the
    ``pubStartDate``/``pubEndDate`` window so agents get current items
    without dumping the entire CVE corpus.
    """
    current = now or _utcnow()
    window_days = max(1, min(int(days), 120))
    capped_limit = max(1, min(int(limit), 100))
    params: dict[str, Any] = {
        "pubStartDate": _nvd_timestamp(current - timedelta(days=window_days)),
        "pubEndDate": _nvd_timestamp(current),
        "resultsPerPage": capped_limit,
    }
    if query.strip():
        params["keywordSearch"] = query.strip()
    sev = _normalise_severity(severity)
    if sev != "UNKNOWN" and severity:
        params["cvssV3Severity"] = sev

    payload, error = _http_get_json(
        NVD_CVES_20_URL,
        params=params,
        timeout_s=timeout_s,
        client_factory=client_factory,
    )
    if error:
        return IntelReport(
            kind="cve",
            query=query,
            source="nvd",
            fetched_at=_stamp(current),
            error=error,
        ).to_dict()

    items = [_nvd_item(entry) for entry in (payload or {}).get("vulnerabilities") or []]
    return IntelReport(
        kind="cve",
        query=query,
        source="nvd",
        fetched_at=_stamp(current),
        items=items[:capped_limit],
    ).to_dict()


def _kev_item(entry: dict[str, Any]) -> IntelItem:
    cve_id = str(entry.get("cveID") or "")
    vendor = str(entry.get("vendorProject") or "")
    product = str(entry.get("product") or "")
    due = str(entry.get("dueDate") or "")
    title = str(entry.get("vulnerabilityName") or cve_id)
    return IntelItem(
        id=cve_id,
        title=title,
        source="cisa-kev",
        url=f"https://nvd.nist.gov/vuln/detail/{cve_id}" if cve_id else "",
        severity="KNOWN_EXPLOITED",
        published_at=str(entry.get("dateAdded") or ""),
        updated_at=due,
        summary=str(entry.get("shortDescription") or ""),
        affected=[part for part in (vendor, product) if part],
        references=[str(entry.get("notes") or "")] if entry.get("notes") else [],
        tags=["known-exploited", "zero-day-watch"],
    )


def _matches_terms(item: IntelItem, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = " ".join(
        [
            item.id,
            item.title,
            item.summary,
            " ".join(item.affected),
            " ".join(item.tags),
        ]
    ).lower()
    return all(term in haystack for term in terms)


def query_zero_day_feeds(
    product: str = "",
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_s: float = DEFAULT_TIMEOUT_SECONDS,
    client_factory: HttpClientFactory | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Query exploited-in-the-wild feed data for zero-day watch.

    BP.I.1 uses CISA KEV as the source-backed minimum viable feed. It
    is broader than strict "zero-day" disclosure timing, but it is the
    project-local pattern used for security gates: source-backed signal
    first, policy integration later.
    """
    current = now or _utcnow()
    capped_limit = max(1, min(int(limit), 100))
    payload, error = _http_get_json(
        CISA_KEV_URL,
        timeout_s=timeout_s,
        client_factory=client_factory,
    )
    if error:
        return IntelReport(
            kind="zero_day",
            query=product,
            source="cisa-kev",
            fetched_at=_stamp(current),
            error=error,
        ).to_dict()

    terms = [part.lower() for part in product.split() if part.strip()]
    items = [
        _kev_item(entry)
        for entry in (payload or {}).get("vulnerabilities") or []
    ]
    filtered = [item for item in items if _matches_terms(item, terms)]
    return IntelReport(
        kind="zero_day",
        query=product,
        source="cisa-kev",
        fetched_at=_stamp(current),
        items=filtered[:capped_limit],
    ).to_dict()


_BEST_PRACTICES: tuple[IntelItem, ...] = (
    IntelItem(
        id="cisa-kev-remediation",
        title="Patch known exploited vulnerabilities first",
        source="cisa",
        url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        severity="HIGH",
        summary=(
            "Use the KEV catalog as the emergency remediation queue; "
            "known-exploited items outrank ordinary dependency drift."
        ),
        tags=["cve", "kev", "patching", "vulnerability-management"],
    ),
    IntelItem(
        id="nist-sp-800-40r4",
        title="Run risk-based enterprise patch management",
        source="nist",
        url="https://csrc.nist.gov/publications/detail/sp/800-40/rev-4/final",
        severity="MEDIUM",
        summary=(
            "Inventory assets, prioritize vulnerabilities by exploitability "
            "and impact, test updates, deploy, and verify remediation."
        ),
        tags=["patching", "vulnerability-management", "asset-inventory"],
    ),
    IntelItem(
        id="nist-ssdf-sp-800-218",
        title="Apply Secure Software Development Framework practices",
        source="nist",
        url="https://csrc.nist.gov/publications/detail/sp/800-218/final",
        severity="MEDIUM",
        summary=(
            "Protect the development environment, produce well-secured "
            "software, verify security, and respond to vulnerabilities."
        ),
        tags=["secure-sdlc", "supply-chain", "code-review"],
    ),
    IntelItem(
        id="cisa-secure-by-design",
        title="Prefer secure-by-design controls over customer hardening",
        source="cisa",
        url="https://www.cisa.gov/securebydesign",
        severity="MEDIUM",
        summary=(
            "Make secure defaults, memory-safe choices, MFA, logging, and "
            "vulnerability disclosure part of the product baseline."
        ),
        tags=["secure-by-design", "hardening", "default-secure"],
    ),
    IntelItem(
        id="owasp-asvs",
        title="Use OWASP ASVS for application security verification",
        source="owasp",
        url="https://owasp.org/www-project-application-security-verification-standard/",
        severity="MEDIUM",
        summary=(
            "Map web/API controls to ASVS levels so auth, session, input, "
            "crypto, and logging requirements are testable."
        ),
        tags=["owasp", "web", "api", "verification"],
    ),
)


def fetch_latest_best_practices(
    topic: str = "",
    *,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return source-backed SecOps hardening practices filtered by topic.

    This BP.I.1 slice intentionally avoids a mutable local cache or a
    bespoke crawler. The curated list is immutable module data and gives
    later guild/hook work a stable skill output shape.
    """
    current = now or _utcnow()
    capped_limit = max(1, min(int(limit), 100))
    terms = [part.lower() for part in topic.split() if part.strip()]
    items = [item for item in _BEST_PRACTICES if _matches_terms(item, terms)]
    return IntelReport(
        kind="best_practice",
        query=topic,
        source="curated",
        fetched_at=_stamp(current),
        items=items[:capped_limit],
    ).to_dict()


__all__ = [
    "CISA_KEV_URL",
    "DEFAULT_LIMIT",
    "DEFAULT_LOOKBACK_DAYS",
    "IntelItem",
    "IntelReport",
    "NVD_CVES_20_URL",
    "fetch_latest_best_practices",
    "query_zero_day_feeds",
    "search_latest_cve",
]
