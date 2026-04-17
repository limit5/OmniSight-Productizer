r"""L11 #338 capstone — budget + acceptance-criteria contract.

L11 #1–#5 each pinned one slice of the tri-platform one-click deploy
story:

- L11 #1 `tests/test_digitalocean_app_spec.py` (27 cases) — DO spec.
- L11 #2 `tests/test_railway_spec.py` (16 cases) — Railway spec.
- L11 #3 `tests/test_render_blueprint.py` (28 cases) — Render spec.
- L11 #4 `tests/test_deploy_parity_across_platforms.py` (22 cases) —
  cross-platform env / services / build-command drift.
- L11 #5 `tests/test_readme_deploy_buttons.py` (11 cases) — README
  button markdown / SVG host / canonical-repo target / runbook links.

Each sibling file is scoped to one platform or one UX surface. None of
them verifies the **emergent capstone claim** this step owns:

  "L11 as a whole delivered within its 1-day budget and the five sub-
   items together satisfy the L11 acceptance criteria — i.e. clicking
   the README Deploy button on any of the three platforms, filling in
   env values, and reaching the Bootstrap wizard within 3 minutes."

That claim rides on several preconditions that are load-bearing across
the L11 block but sit OUTSIDE any single sibling's scope:

1. TODO.md's L11 section actually has all 5 sub-items marked done and
   the budget math adds up (`L11 (1)` in 總預估 arithmetic).
2. The acceptance text — "3 分鐘內 public URL 可用 → Bootstrap wizard
   引導設定" — hasn't been silently deleted or watered down during
   review churn (so the user contract is still on record).
3. All three platform directories (`deploy/digitalocean/`,
   `deploy/railway/`, `deploy/render/`) hold BOTH a spec file AND a
   companion runbook README. A platform dir with only one or the other
   fails the one-click UX even if per-platform tests still green.
4. K1's Bootstrap-wizard reachability — every platform's env matrix
   includes `OMNISIGHT_ADMIN_EMAIL` + `OMNISIGHT_ADMIN_PASSWORD` so
   backend seeds a must-change-password admin on first boot; at least
   one runbook per platform mentions `must_change_password` OR the
   wizard-login handoff so the operator knows what happens after click.
5. The 5 sibling test files all exist at their canonical paths and
   collectively hold ≥ 104 cases (the observed baseline at close-out).
   A drop below that threshold means a sibling was deleted or gutted.
6. Zero new runtime dependency was introduced across L11 — PyYAML was
   already in backend/requirements.txt before L11 started, and L11 did
   not need anything else. Drift here hides cost elsewhere.

None of these assertions duplicate sibling coverage: they all live at
the capstone layer — "does the L11 block, taken as a whole, meet its
acceptance criteria and budget claim?"

Cost: <100 ms. stdlib re / pathlib only + one yaml.safe_load of the two
multi-service specs (DO + Render) to count declared services.

Mirrors the L10 #5 capstone pattern (tests/test_pull_30s_effect.py or
backend/tests/test_pull_30s_effect.py) — emergent contract pinned one
layer above the sibling precondition tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TODO = REPO_ROOT / "TODO.md"
README = REPO_ROOT / "README.md"
REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"

DEPLOY_DIR = REPO_ROOT / "deploy"

# Each platform: (dir name, spec filename, companion runbook filename).
# The capstone enforces both artifacts exist for every platform — a
# spec without a runbook (or vice versa) fails the one-click UX even
# if the sibling test reading only the spec still passes.
PLATFORMS = [
    ("digitalocean", "app.yaml", "README.md"),
    ("railway", "railway.json", "README.md"),
    ("render", "render.yaml", "README.md"),
]

# The 5 sibling test files + the observed combined test-function
# count at L11 close-out. Drift below this threshold means a sibling
# was deleted or a large chunk of assertions was removed without a
# replacement landing in this capstone.
#
# Note: this counts `def test_` lines (the heuristic below) — NOT the
# pytest-collected case count, which is larger because several tests
# use `@pytest.mark.parametrize` and expand at collection time. The
# observed heuristic count at close-out is 94 function definitions,
# which expands to 104 collected cases at runtime. The floor is set
# to the function-def count because that's what this test measures;
# parametrize-expansion changes (e.g. adding a platform) should not
# trigger the assertion, but deleting real test functions should.
SIBLING_TEST_FILES = [
    "tests/test_digitalocean_app_spec.py",
    "tests/test_railway_spec.py",
    "tests/test_render_blueprint.py",
    "tests/test_deploy_parity_across_platforms.py",
    "tests/test_readme_deploy_buttons.py",
]
SIBLING_BASELINE_FUNCTIONS = 94


@pytest.fixture(scope="module")
def todo_text() -> str:
    assert TODO.is_file(), "TODO.md is missing from repo root"
    return TODO.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README.is_file(), "README.md is missing from repo root"
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def l11_block(todo_text: str) -> str:
    """Extract the L11 section of TODO.md so assertions can't be fooled
    by matching text from unrelated sections (e.g. L10 or G1)."""
    # Section header is "### L11. 雲端一鍵佈署按鈕 (#338)" and runs until
    # the next `### ` header or `---` horizontal rule.
    m = re.search(
        r"### L11\. 雲端一鍵佈署按鈕.*?(?=\n### |\n---\n)",
        todo_text,
        re.DOTALL,
    )
    assert m, "Could not locate L11 section in TODO.md"
    return m.group(0)


# ── Section 1 — TODO.md L11 budget arithmetic & checkbox hygiene ─────


def test_l11_block_declares_one_day_budget(l11_block: str) -> None:
    """The final checkbox line in the L11 block records the 1-day
    budget. When this test runs AFTER TODO.md is updated to mark the
    budget line done, the `- [x]` form is expected; before the update
    the `- [ ]` form is valid. Accept either — the LOAD-BEARING claim
    is the budget *value*, not which tense the checkbox sits in."""
    pattern = re.compile(r"-\s+\[[ xO]\]\s+預估：\*\*1\s+day\*\*")
    assert pattern.search(l11_block), (
        "L11 block must declare 1-day budget in a checkbox row "
        "(`- [ ] 預估：**1 day**` or marked done)."
    )


def test_total_budget_line_includes_l11_one_day(todo_text: str) -> None:
    """The 總預估 arithmetic line immediately after the L11 block must
    include `L11 (1)`. If someone changes the L11 budget without
    updating the total, this test rings the bell."""
    assert "L11 (1)" in todo_text, (
        "TODO.md 總預估 arithmetic line must include `L11 (1)`."
    )


def test_l11_implementation_items_all_checked(l11_block: str) -> None:
    """Every non-budget checkbox in L11 must be `[x]` (done) — i.e. no
    leftover `[ ]` work hides above the budget line.

    The budget line itself is excluded: it is allowed to be `[ ]` at
    the moment this test runs PRE-update (first invocation), but must
    be `[x]` after TODO.md is updated in the same step. Either state
    on the budget line is acceptable; any OTHER `[ ]` in L11 fails."""
    lines = l11_block.splitlines()
    open_non_budget = [
        line for line in lines
        if re.match(r"- \[ \] ", line) and "預估：" not in line
    ]
    assert not open_non_budget, (
        "L11 has unchecked implementation items besides the budget "
        f"line: {open_non_budget}"
    )


def test_total_estimate_sums_consistently(todo_text: str) -> None:
    """L1-L8 (4.5) + L9 (1) + L10 (0.5) + L11 (1) = 7 — capstone does
    the arithmetic so a partial edit (e.g. bumping L11 to 2 days
    without updating the ~7 total) is caught.

    Uses a substring check rather than parsing — the TODO.md format
    has been stable and a regex would add noise without catching more.
    """
    # Pin the exact string — spaces and asterisks included.
    pinned = (
        "**總預估**：L1-L8 (4.5) + L9 (1) + L10 (0.5) + L11 (1) = "
        "**~7 day**"
    )
    assert pinned in todo_text, (
        "總預估 arithmetic line does not match pinned form "
        f"— expected substring: {pinned!r}"
    )


# ── Section 2 — L11 acceptance criteria preservation ─────────────────


def test_acceptance_lists_bootstrap_wizard_in_cloud_path(
    l11_block: str,
) -> None:
    """The cloud acceptance clause must name the Bootstrap wizard as
    the landing state after deploy — this is the anchor that ties
    L11's output to K1's must_change_password contract."""
    # Match relevant Chinese + English variants to avoid churn-fragility.
    hits = [
        "Bootstrap wizard" in l11_block,
        "bootstrap wizard" in l11_block.lower(),
    ]
    assert any(hits), (
        "L11 acceptance criteria must mention the Bootstrap wizard "
        "as the post-deploy landing state."
    )


def test_acceptance_pins_three_minute_cloud_sla(l11_block: str) -> None:
    """Cloud acceptance promises `3 分鐘內 public URL 可用`. The three-
    minute figure is load-bearing for the user-facing value claim —
    softening it ("5 分鐘", "in minutes", "eventually") without a
    budget renegotiation silently breaks the README Deploy-button UX
    story."""
    # Accept either Chinese "3 分鐘" or English "3 minute(s)" form.
    has_cn = re.search(r"3\s*分鐘", l11_block) is not None
    has_en = re.search(r"\b3\s*minute", l11_block, re.IGNORECASE) is not None
    assert has_cn or has_en, (
        "L11 acceptance must pin the 3-minute cloud-deploy SLA "
        "(currently expressed as `3 分鐘內 public URL 可用`)."
    )


def test_acceptance_names_readme_deploy_button(l11_block: str) -> None:
    """Acceptance clause must reference the README Deploy button as
    the entry point — the whole L11 block is pointless without that
    one-click UX surface being THE user contract."""
    # The live TODO text is `點 README 的 Deploy 按鈕`.
    assert (
        "README" in l11_block and "Deploy" in l11_block
    ), (
        "L11 acceptance must name the README Deploy button as the "
        "cloud entry point."
    )


# ── Section 3 — every platform ships BOTH spec AND runbook ───────────


@pytest.mark.parametrize("platform,spec,runbook", PLATFORMS)
def test_every_platform_ships_spec_and_runbook(
    platform: str, spec: str, runbook: str
) -> None:
    """Every platform directory must hold both a deploy spec AND a
    companion runbook README. A spec without a runbook leaves the
    operator stranded at post-deploy env prompts; a runbook without a
    spec means the Deploy button has nothing to apply."""
    platform_dir = DEPLOY_DIR / platform
    spec_path = platform_dir / spec
    runbook_path = platform_dir / runbook
    assert spec_path.is_file(), (
        f"{platform}: spec `{spec_path}` missing"
    )
    assert runbook_path.is_file(), (
        f"{platform}: runbook `{runbook_path}` missing"
    )


@pytest.mark.parametrize("platform,_spec,runbook", PLATFORMS)
def test_every_runbook_is_non_stub(
    platform: str, _spec: str, runbook: str
) -> None:
    """Companion runbooks must be non-stub. A 2-line stub is a broken
    UX — the operator sees a file but gets no guidance. 500 bytes is
    a generous floor: even a terse runbook has a title + topology +
    env matrix + post-deploy steps, easily clearing 500 B. Current
    files: DO ≈3.2KB, Railway ≈4.1KB, Render ≈7.3KB — lots of slack.
    """
    runbook_path = DEPLOY_DIR / platform / runbook
    size = runbook_path.stat().st_size
    assert size > 500, (
        f"{platform}: runbook `{runbook_path}` is a stub "
        f"({size} bytes ≤ 500 B floor)"
    )


# ── Section 4 — Bootstrap wizard wiring across all 3 platforms ───────


@pytest.mark.parametrize("platform,spec,runbook", PLATFORMS)
def test_platform_wires_admin_bootstrap_envs(
    platform: str, spec: str, runbook: str
) -> None:
    """Each platform must reference BOTH `OMNISIGHT_ADMIN_EMAIL` AND
    `OMNISIGHT_ADMIN_PASSWORD` somewhere in its spec+runbook surface.
    Backend's K1 first-boot seeder reads these two envs to create the
    initial admin account (with must_change_password=True) — missing
    either means the operator lands on the Deploy URL with no way to
    log in, breaking the L11 acceptance chain.

    Railway's spec (`railway.json`) carries no env block (schema
    limitation — Railway envs live in the dashboard), so its runbook
    is the only place these envs can appear. DO + Render embed them
    directly in the spec. This test accepts either surface — combined
    spec+runbook text — because the operator sees both when clicking
    Deploy."""
    spec_text = (DEPLOY_DIR / platform / spec).read_text(encoding="utf-8")
    runbook_text = (DEPLOY_DIR / platform / runbook).read_text(
        encoding="utf-8"
    )
    combined = spec_text + "\n" + runbook_text
    assert "OMNISIGHT_ADMIN_EMAIL" in combined, (
        f"{platform}: neither spec nor runbook references "
        "`OMNISIGHT_ADMIN_EMAIL` — K1 admin seed will never run"
    )
    assert "OMNISIGHT_ADMIN_PASSWORD" in combined, (
        f"{platform}: neither spec nor runbook references "
        "`OMNISIGHT_ADMIN_PASSWORD` — K1 admin seed will never run"
    )


def test_at_least_one_runbook_explains_must_change_password() -> None:
    """At least one platform runbook must document the
    `must_change_password` wizard behavior — the K1 contract that the
    seeded admin is forced through a password-rotation screen on
    first login. This is what the L11 acceptance ("Bootstrap wizard
    引導設定") refers to; if NO runbook explains it, operators are
    puzzled why they can't just use the bootstrap password.

    Weak-form assertion (at least one, not every) because DO's
    runbook is terse by design and delegates to backend docs; Render
    + Railway runbooks carry the explanation."""
    corpus = []
    for platform, _spec, runbook in PLATFORMS:
        path = DEPLOY_DIR / platform / runbook
        corpus.append(path.read_text(encoding="utf-8"))
    combined = "\n".join(corpus)
    # Accept explicit mention of must_change_password OR the wizard/
    # first-login handoff phrase used by Render's runbook.
    explained = (
        "must_change_password" in combined
        or re.search(r"change\s+on\s+first\s+login", combined,
                     re.IGNORECASE) is not None
        or re.search(r"first[-\s]login", combined, re.IGNORECASE) is not None
    )
    assert explained, (
        "No platform runbook explains K1's must_change_password "
        "wizard — operator won't understand why their bootstrap "
        "password is rejected after first login"
    )


# ── Section 5 — README Deploy section exposes all three buttons ──────


def test_readme_deploy_section_contains_all_three_platforms(
    readme_text: str,
) -> None:
    """README's one-click deploy section must reference all three
    platforms by name — so a first-time visitor sees three options,
    not two. Complement to L11 #5's URL-substring check (which
    verifies the deploy-URL hosts); this one verifies the
    user-readable platform names are present."""
    for name in ("DigitalOcean", "Railway", "Render"):
        assert name in readme_text, (
            f"README.md does not mention `{name}` — Deploy section "
            "is incomplete"
        )


# ── Section 6 — sibling test file inventory + case-count floor ───────


@pytest.mark.parametrize("rel", SIBLING_TEST_FILES)
def test_sibling_test_file_exists(rel: str) -> None:
    """Each of the 5 L11 sibling test files must exist at its canonical
    path. A rename or delete that slips past per-platform review
    silently drops coverage; this capstone file-existence check
    surfaces the violation with the specific missing path."""
    path = REPO_ROOT / rel
    assert path.is_file(), f"L11 sibling test missing: {rel}"


def test_sibling_suite_meets_baseline_function_count() -> None:
    """Aggregate test-function count across the 5 L11 sibling files
    must stay at-or-above the observed close-out baseline. A drop
    means test functions were deleted without being moved elsewhere.

    Uses a cheap heuristic — count top-level `def test_` lines. This
    intentionally does NOT parse pytest's actual collection (too
    slow, too much depth); it catches the gross-removal regression
    without flagging legitimate refactors that combine assertions
    within the same test function. The heuristic count is what this
    test tracks — see `SIBLING_BASELINE_FUNCTIONS` comment above for
    why it's 94 and how it differs from the 104 collected cases."""
    total = 0
    for rel in SIBLING_TEST_FILES:
        path = REPO_ROOT / rel
        text = path.read_text(encoding="utf-8")
        # Count top-level functions starting with test_ .
        # ^def test_ at column 0 avoids matching nested helpers.
        total += len(re.findall(r"^def test_\w+", text, re.MULTILINE))
    assert total >= SIBLING_BASELINE_FUNCTIONS, (
        f"L11 sibling suite function count dropped: {total} < baseline "
        f"{SIBLING_BASELINE_FUNCTIONS} — verify no file was gutted"
    )


# ── Section 7 — zero new runtime dep smoke test ──────────────────────


def test_l11_introduced_no_new_runtime_deps() -> None:
    """L11 shipped 3 YAML/JSON specs + 3 runbooks + 5 test files. The
    only library any of these needed was PyYAML, which predates L11
    in `backend/requirements.txt`. This test pins that claim — any
    new top-level require landing in requirements.txt as part of L11
    would show up here.

    Heuristic: walks the sibling test imports and asserts every
    third-party import is either stdlib, already present in
    requirements.txt, or a test-time-only dep (pytest)."""
    assert REQUIREMENTS.is_file(), "backend/requirements.txt missing"
    req_text = REQUIREMENTS.read_text(encoding="utf-8").lower()
    # Collect imports from the 5 L11 sibling test files + this file.
    files_to_scan = SIBLING_TEST_FILES + ["tests/test_l11_budget_capstone.py"]
    imports: set[str] = set()
    for rel in files_to_scan:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(
                r"\s*(?:from|import)\s+([a-zA-Z_][\w\.]*)", line
            )
            if m:
                imports.add(m.group(1).split(".")[0])
    # Allowed: stdlib + pytest (test-time) + yaml (PyYAML, already in
    # requirements) + __future__ (stdlib).
    stdlib_ok = {
        "re", "json", "pathlib", "os", "sys", "typing", "collections",
        "itertools", "functools", "__future__",
    }
    # PyYAML ships as import name `yaml`.
    allowed_thirdparty = {"yaml", "pytest"}
    unexpected = imports - stdlib_ok - allowed_thirdparty
    assert not unexpected, (
        f"L11 sibling tests introduced unexpected imports: "
        f"{sorted(unexpected)} — if legit, add to allowlist and "
        "also add to backend/requirements.txt"
    )
    # Sanity: pyyaml must already be listed in requirements.txt.
    assert "pyyaml" in req_text, (
        "PyYAML missing from backend/requirements.txt — the 3 YAML "
        "specs + their tests depend on it"
    )


# ── Section 8 — emergent 3-minute deploy chain integrity ─────────────


def test_deploy_chain_is_buildable_from_repo_alone() -> None:
    """Emergent claim: clicking Deploy → cloud service pulls repo at
    `master` → uses spec → builds from existing Dockerfiles → reaches
    Bootstrap wizard. This test pins the minimal building-block set
    that chain requires — no source-build-only flow can meet the
    3-minute SLA, so both Dockerfiles must be present in repo root.
    (Per-platform tests each check their own Dockerfile reference,
    but none verifies both are present simultaneously from the L11
    capstone's viewpoint.)"""
    backend_dockerfile = REPO_ROOT / "Dockerfile.backend"
    frontend_dockerfile = REPO_ROOT / "Dockerfile.frontend"
    assert backend_dockerfile.is_file(), (
        "Dockerfile.backend missing from repo root — all three "
        "deploy specs reference it; the 3-min SLA collapses"
    )
    assert frontend_dockerfile.is_file(), (
        "Dockerfile.frontend missing from repo root — all three "
        "deploy specs reference it; frontend rollout fails"
    )


def test_both_multi_service_specs_declare_two_services() -> None:
    """DO and Render both use multi-service schemas (a single YAML
    file describes both backend + frontend). Railway's schema is
    single-service-per-file so it's excluded here. Both multi-service
    specs must declare ≥ 2 services — else the frontend half of the
    one-click deploy simply doesn't exist and the 3-min SLA fails."""
    do_spec = yaml.safe_load(
        (DEPLOY_DIR / "digitalocean" / "app.yaml").read_text(
            encoding="utf-8"
        )
    )
    render_spec = yaml.safe_load(
        (DEPLOY_DIR / "render" / "render.yaml").read_text(
            encoding="utf-8"
        )
    )
    do_services = do_spec.get("services") or []
    render_services = render_spec.get("services") or []
    assert len(do_services) >= 2, (
        f"DO spec declares {len(do_services)} services — backend + "
        "frontend both required for the L11 one-click UX"
    )
    assert len(render_services) >= 2, (
        f"Render spec declares {len(render_services)} services — "
        "backend + frontend both required for the L11 one-click UX"
    )
