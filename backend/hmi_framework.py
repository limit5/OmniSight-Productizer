"""C26 / L4-CORE-26 HMI embedded web UI framework (#261).

Core module providing:
    * per-platform flash-partition-aware bundle size budgets + CI gate
    * IEC 62443 security baseline gate (CSP/XSS/CSRF/session/auth)
    * embedded browser ABI compatibility matrix (aarch64/armv7/riscv64)
    * shared i18n language pool (en/zh-TW/ja/de)
    * generator framework whitelist (Preact/lit-html/vanilla)

Sub-modules ``hmi_generator``, ``hmi_binding``, ``hmi_components`` and
``hmi_llm`` consume the config loaded here.

All security decisions reference the YAML at ``configs/hmi_framework.yaml``
so operators can tune thresholds without code changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "configs" / "hmi_framework.yaml"

FRAMEWORK_VERSION = "1.0.0"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dataclasses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class BundleBudget:
    platform: str
    flash_partition_bytes: int
    hmi_budget_bytes: int
    html_css_max_bytes: int
    js_max_bytes: int
    fonts_max_bytes: int

    @property
    def hmi_budget_kib(self) -> float:
        return round(self.hmi_budget_bytes / 1024.0, 2)


@dataclass
class BundleMeasurement:
    html_bytes: int = 0
    css_bytes: int = 0
    js_bytes: int = 0
    fonts_bytes: int = 0
    other_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.html_bytes + self.css_bytes + self.js_bytes + self.fonts_bytes + self.other_bytes

    @property
    def html_css_bytes(self) -> int:
        return self.html_bytes + self.css_bytes


@dataclass
class BudgetVerdict:
    platform: str
    status: str                    # "pass" | "fail"
    total_bytes: int
    budget_bytes: int
    violations: list[str] = field(default_factory=list)
    measurement: BundleMeasurement | None = None


@dataclass
class ABIEntry:
    engine: str
    version: str
    min_es_version: str
    supports_webgl2: bool
    supports_wasm: bool
    supports_webrtc: bool
    notes: str = ""


@dataclass
class SecurityFinding:
    severity: str                  # "error" | "warn" | "info"
    rule: str
    detail: str


@dataclass
class SecurityReport:
    status: str                    # "pass" | "fail"
    standard: str
    findings: list[SecurityFinding] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")


@dataclass
class LocaleInfo:
    code: str
    name: str
    rtl: bool


@dataclass
class FrameworkEntry:
    name: str
    version: str
    size_bytes: int
    license: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Config loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


_CONFIG_CACHE: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    if not _CONFIG_PATH.exists():
        logger.warning("hmi_framework.yaml not found at %s — using empty config", _CONFIG_PATH)
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE
    try:
        _CONFIG_CACHE = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.error("hmi_framework.yaml parse failed: %s", exc)
        _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def reload_config() -> None:
    """Force reload (used by tests)."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    _load_config()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Bundle budget API
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_platforms() -> list[str]:
    cfg = _load_config()
    return sorted((cfg.get("bundle_budgets") or {}).keys())


def get_bundle_budget(platform: str) -> BundleBudget:
    cfg = _load_config()
    budgets = cfg.get("bundle_budgets") or {}
    entry = budgets.get(platform)
    if entry is None:
        raise KeyError(f"No bundle budget defined for platform '{platform}'")
    return BundleBudget(
        platform=platform,
        flash_partition_bytes=int(entry.get("flash_partition_bytes", 0)),
        hmi_budget_bytes=int(entry.get("hmi_budget_bytes", 0)),
        html_css_max_bytes=int(entry.get("html_css_max_bytes", 0)),
        js_max_bytes=int(entry.get("js_max_bytes", 0)),
        fonts_max_bytes=int(entry.get("fonts_max_bytes", 0)),
    )


def measure_bundle(files: dict[str, bytes | str]) -> BundleMeasurement:
    """Compute sizes from {path: content} dict. Classifies by extension."""
    m = BundleMeasurement()
    for path, content in files.items():
        size = len(content.encode("utf-8") if isinstance(content, str) else content)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext == "html" or ext == "htm":
            m.html_bytes += size
        elif ext == "css":
            m.css_bytes += size
        elif ext in ("js", "mjs"):
            m.js_bytes += size
        elif ext in ("woff", "woff2", "ttf", "otf", "eot"):
            m.fonts_bytes += size
        else:
            m.other_bytes += size
    return m


def check_bundle_budget(platform: str, measurement: BundleMeasurement) -> BudgetVerdict:
    budget = get_bundle_budget(platform)
    violations: list[str] = []

    if measurement.total_bytes > budget.hmi_budget_bytes:
        violations.append(
            f"Total bundle {measurement.total_bytes}B exceeds budget "
            f"{budget.hmi_budget_bytes}B for platform '{platform}'"
        )
    if measurement.html_css_bytes > budget.html_css_max_bytes:
        violations.append(
            f"HTML+CSS {measurement.html_css_bytes}B exceeds "
            f"{budget.html_css_max_bytes}B"
        )
    if measurement.js_bytes > budget.js_max_bytes:
        violations.append(f"JS {measurement.js_bytes}B exceeds {budget.js_max_bytes}B")
    if measurement.fonts_bytes > budget.fonts_max_bytes:
        violations.append(
            f"Fonts {measurement.fonts_bytes}B exceeds {budget.fonts_max_bytes}B"
        )
    # Flash partition awareness: the HMI budget + reserved OS must fit in flash
    if budget.flash_partition_bytes > 0 and measurement.total_bytes > budget.flash_partition_bytes:
        violations.append(
            f"Total bundle {measurement.total_bytes}B exceeds flash partition "
            f"{budget.flash_partition_bytes}B"
        )

    return BudgetVerdict(
        platform=platform,
        status="fail" if violations else "pass",
        total_bytes=measurement.total_bytes,
        budget_bytes=budget.hmi_budget_bytes,
        violations=violations,
        measurement=measurement,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ABI matrix
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_abi_entries(platform: str) -> list[ABIEntry]:
    cfg = _load_config()
    entries = (cfg.get("abi_matrix") or {}).get(platform) or []
    return [
        ABIEntry(
            engine=e.get("engine", ""),
            version=e.get("version", ""),
            min_es_version=e.get("min_es_version", ""),
            supports_webgl2=bool(e.get("supports_webgl2", False)),
            supports_wasm=bool(e.get("supports_wasm", False)),
            supports_webrtc=bool(e.get("supports_webrtc", False)),
            notes=e.get("notes", ""),
        )
        for e in entries
    ]


def all_abi_matrix() -> dict[str, list[ABIEntry]]:
    cfg = _load_config()
    result: dict[str, list[ABIEntry]] = {}
    for platform in (cfg.get("abi_matrix") or {}).keys():
        result[platform] = list_abi_entries(platform)
    return result


def check_abi_compatibility(
    platform: str,
    needs: dict[str, bool] | None = None,
    needs_es_version: str = "ES2019",
) -> dict[str, Any]:
    """Return compatibility report for all engines on ``platform``.

    ``needs`` is a dict of required capability → bool (e.g. ``{"webgl2": True}``).
    """
    entries = list_abi_entries(platform)
    if not entries:
        return {"platform": platform, "compatible": [], "incompatible": [], "status": "unknown"}

    compatible = []
    incompatible = []
    needs = needs or {}

    for e in entries:
        reasons = []
        if needs.get("webgl2") and not e.supports_webgl2:
            reasons.append(f"{e.engine} lacks WebGL2")
        if needs.get("wasm") and not e.supports_wasm:
            reasons.append(f"{e.engine} lacks WASM")
        if needs.get("webrtc") and not e.supports_webrtc:
            reasons.append(f"{e.engine} lacks WebRTC")
        if _es_version_num(needs_es_version) > _es_version_num(e.min_es_version):
            reasons.append(
                f"{e.engine} supports {e.min_es_version} < required {needs_es_version}"
            )
        target = {"engine": e.engine, "version": e.version, "reasons": reasons}
        if reasons:
            incompatible.append(target)
        else:
            compatible.append(target)

    return {
        "platform": platform,
        "compatible": compatible,
        "incompatible": incompatible,
        "status": "pass" if compatible else "fail",
    }


def _es_version_num(v: str) -> int:
    m = re.match(r"ES(\d{4})", v or "")
    return int(m.group(1)) if m else 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Security baseline (IEC 62443)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def security_baseline() -> dict[str, Any]:
    cfg = _load_config()
    return cfg.get("security_baseline") or {}


def scan_security(
    html: str = "",
    js: str = "",
    headers: dict[str, str] | None = None,
    csp: str = "",
) -> SecurityReport:
    """Scan HMI bundle artefacts against the IEC 62443 baseline."""
    baseline = security_baseline()
    findings: list[SecurityFinding] = []
    headers = {k.lower(): v for k, v in (headers or {}).items()}

    # Required headers
    for hdr in baseline.get("required_headers", []):
        if hdr.lower() not in headers:
            findings.append(SecurityFinding("error", "missing_header", f"Required header '{hdr}' not set"))

    # CSP directives
    effective_csp = csp or headers.get("content-security-policy", "")
    for directive in baseline.get("required_csp_directives", []):
        if directive not in effective_csp:
            findings.append(
                SecurityFinding("error", "csp_directive_missing", f"CSP directive '{directive}' missing")
            )

    # Forbidden patterns
    combined = html + "\n" + js
    for pat in baseline.get("forbidden_patterns", []):
        if pat in combined:
            findings.append(SecurityFinding("error", "forbidden_pattern", f"Forbidden pattern '{pat}' found"))

    # Forbidden inline event attributes
    for attr in baseline.get("forbidden_attributes", []):
        if re.search(rf"\b{re.escape(attr)}\s*=", html, flags=re.IGNORECASE):
            findings.append(
                SecurityFinding("error", "inline_event_attr", f"Inline event handler '{attr}=' detected")
            )

    status = "fail" if any(f.severity == "error" for f in findings) else "pass"
    return SecurityReport(
        status=status,
        standard=baseline.get("name", "IEC 62443-4-2 SL2"),
        findings=findings,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  i18n helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_locales() -> list[LocaleInfo]:
    cfg = _load_config()
    items = (cfg.get("i18n") or {}).get("supported_locales") or []
    return [
        LocaleInfo(code=i["code"], name=i["name"], rtl=bool(i.get("rtl", False)))
        for i in items
    ]


def default_locale() -> str:
    cfg = _load_config()
    return (cfg.get("i18n") or {}).get("default_locale") or "en"


def base_i18n_keys() -> list[str]:
    cfg = _load_config()
    return list((cfg.get("i18n") or {}).get("base_keys") or [])


def build_i18n_catalog(overrides: dict[str, dict[str, str]] | None = None) -> dict[str, dict[str, str]]:
    """Assemble a flat {locale: {key: text}} catalog from base keys.

    Missing translations default to the English string pool; caller can
    supply overrides to inject per-device strings.
    """
    overrides = overrides or {}
    keys = base_i18n_keys()
    english = _DEFAULT_EN_POOL
    catalog: dict[str, dict[str, str]] = {}
    for locale in list_locales():
        loc_map: dict[str, str] = {}
        for key in keys:
            if locale.code in overrides and key in overrides[locale.code]:
                loc_map[key] = overrides[locale.code][key]
                continue
            # Fallback: use the default English if no translation provided
            loc_map[key] = _DEFAULT_POOL.get(locale.code, english).get(key, english.get(key, key))
        catalog[locale.code] = loc_map
    return catalog


_DEFAULT_EN_POOL: dict[str, str] = {
    "nav.home": "Home", "nav.network": "Network", "nav.ota": "Firmware", "nav.logs": "Logs",
    "nav.settings": "Settings", "nav.logout": "Logout",
    "action.save": "Save", "action.cancel": "Cancel",
    "action.reboot": "Reboot", "action.factory_reset": "Factory Reset",
    "status.online": "Online", "status.offline": "Offline",
    "status.updating": "Updating", "status.error": "Error",
    "form.username": "Username", "form.password": "Password", "form.required": "Required",
    "error.unauthorized": "Unauthorized", "error.server": "Server error",
}

_DEFAULT_ZH_TW_POOL: dict[str, str] = {
    "nav.home": "首頁", "nav.network": "網路", "nav.ota": "韌體",
    "nav.logs": "記錄", "nav.settings": "設定", "nav.logout": "登出",
    "action.save": "儲存", "action.cancel": "取消",
    "action.reboot": "重新開機", "action.factory_reset": "恢復原廠設定",
    "status.online": "連線中", "status.offline": "離線",
    "status.updating": "更新中", "status.error": "錯誤",
    "form.username": "使用者名稱", "form.password": "密碼", "form.required": "必填",
    "error.unauthorized": "未授權", "error.server": "伺服器錯誤",
}

_DEFAULT_JA_POOL: dict[str, str] = {
    "nav.home": "ホーム", "nav.network": "ネットワーク", "nav.ota": "ファームウェア",
    "nav.logs": "ログ", "nav.settings": "設定", "nav.logout": "ログアウト",
    "action.save": "保存", "action.cancel": "キャンセル",
    "action.reboot": "再起動", "action.factory_reset": "工場出荷時設定",
    "status.online": "オンライン", "status.offline": "オフライン",
    "status.updating": "更新中", "status.error": "エラー",
    "form.username": "ユーザー名", "form.password": "パスワード", "form.required": "必須",
    "error.unauthorized": "認証エラー", "error.server": "サーバーエラー",
}

_DEFAULT_ZH_CN_POOL: dict[str, str] = {
    "nav.home": "首页", "nav.network": "网络", "nav.ota": "固件",
    "nav.logs": "日志", "nav.settings": "设置", "nav.logout": "退出",
    "action.save": "保存", "action.cancel": "取消",
    "action.reboot": "重启", "action.factory_reset": "恢复出厂设置",
    "status.online": "在线", "status.offline": "离线",
    "status.updating": "升级中", "status.error": "错误",
    "form.username": "用户名", "form.password": "密码", "form.required": "必填",
    "error.unauthorized": "未授权", "error.server": "服务器错误",
}

_DEFAULT_POOL: dict[str, dict[str, str]] = {
    "en": _DEFAULT_EN_POOL,
    "zh-TW": _DEFAULT_ZH_TW_POOL,
    "ja": _DEFAULT_JA_POOL,
    "zh-CN": _DEFAULT_ZH_CN_POOL,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Generator whitelist
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def list_allowed_frameworks() -> list[FrameworkEntry]:
    cfg = _load_config()
    items = (cfg.get("generator_whitelist") or {}).get("frameworks") or []
    return [
        FrameworkEntry(
            name=i.get("name", ""),
            version=i.get("version", ""),
            size_bytes=int(i.get("size_bytes", 0)),
            license=i.get("license", ""),
        )
        for i in items
    ]


def list_forbidden_frameworks() -> list[str]:
    cfg = _load_config()
    return list((cfg.get("generator_whitelist") or {}).get("forbidden_frameworks") or [])


def is_framework_allowed(name: str) -> bool:
    allowed = {f.name.lower() for f in list_allowed_frameworks()}
    return name.lower() in allowed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Summary
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def framework_summary() -> dict[str, Any]:
    return {
        "version": FRAMEWORK_VERSION,
        "platforms": list_platforms(),
        "abi_platforms": sorted(all_abi_matrix().keys()),
        "locales": [loc.code for loc in list_locales()],
        "default_locale": default_locale(),
        "allowed_frameworks": [f.name for f in list_allowed_frameworks()],
        "security_standard": security_baseline().get("name", ""),
    }
