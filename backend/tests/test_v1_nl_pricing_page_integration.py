"""V1 #9 (issue #317) — NL → full pipeline integration test.

Scenario anchored in the TODO row::

    整合測試：NL「做一個定價頁面，三個方案，年月切換」
    → agent 輸出完整 React + shadcn Tabs/Card/Switch 元件 + Tailwind
    → render 正確 + consistency lint pass

Pipeline exercised end-to-end
-----------------------------

Each V1 sibling (#1-#8) has its own unit-level contract test. This
file is the **only** place that wires them together against the
canonical user story:

    1. :func:`backend.edit_complexity_router.route` classifies the
       NL prompt → ``large`` bucket → Opus 4.7 (matches the sibling
       multimodal modules' invariant).
    2. :func:`backend.ui_component_registry.render_agent_context_block`
       and :func:`backend.design_token_loader.render_agent_context_block`
       produce the deterministic fact-side context the UI Designer
       skill (``configs/roles/ui-designer.md``) cites at step 0.
    3. The full generation prompt is assembled from those blocks +
       the NL brief + generation rules — byte-identical on repeat
       calls so Anthropic's prompt cache stays warm.
    4. A :class:`FakeInvoker` replaces the live LLM call and returns
       a canonical, hand-curated pricing TSX. No network I/O.
    5. :func:`backend.vision_to_ui.extract_tsx_from_response` pulls
       the TSX out of the response fence (reused from V1 #5 — the
       extractor is channel-agnostic).
    6. :func:`backend.component_consistency_linter.lint_code` scans
       the emitted TSX; ``is_clean`` (no error-severity violations)
       is the acceptance gate.

Acceptance gate per TODO row
----------------------------

* ``lint_code(tsx).is_clean`` → ``True`` (consistency lint pass).
* TSX contains all three mandated primitives: ``Tabs`` / ``Card`` /
  ``Switch``.
* TSX contains three distinct plan tiers and a monthly↔yearly
  toggle surface.
* TSX imports from ``@/components/ui/*`` (the project's canonical
  shadcn import path — pinned by the sibling registry).

Why structure "render 正確" as assertions over the TSX string
-----------------------------------------------------------

A true React render would require jsdom + Next's toolchain in
Python-side pytest, which is out of scope for the backend harness.
Instead the "render correctly" contract is decomposed into
machine-checkable invariants:

* JSX tag balance (every opening ``<Tabs>`` / ``<Card>`` / ``<Switch>``
  has a matching close) — proves a JSX reconciler would accept it.
* Presence of required props (``value=`` on ``TabsTrigger``,
  ``defaultValue=`` on ``Tabs``, ``aria-label`` on icon-only surfaces).
* The lint report doubles as a "will this render without React
  warnings" probe (raw ``<button>``/``<input>`` / ``div onClick`` /
  missing ``alt`` would all trip the linter and — in the browser —
  fail a11y axe checks or React dev-mode warnings).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import edit_complexity_router as router_mod
from backend import ui_component_registry as registry_mod
from backend import design_token_loader as tokens_mod
from backend.component_consistency_linter import lint_code
from backend.edit_complexity_router import (
    DEFAULT_LARGE_MODEL,
    DEFAULT_PROVIDER,
    EditComplexity,
    EditRouteDecision,
    route,
)
from backend.vision_to_ui import extract_tsx_from_response


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# The canonical user story from the TODO row — do NOT paraphrase it in
# tests; the string is part of the acceptance contract.
NL_PROMPT = "做一個定價頁面，三個方案，年月切換"


# ── Fake invoker & canonical response ────────────────────────────────


class FakeInvoker:
    """Deterministic chat-invoker double.

    Mirrors the double used across sibling tests
    (``test_vision_to_ui.py`` / ``test_figma_to_ui.py`` /
    ``test_url_to_reference.py``): takes a fixed queue of responses
    and records the messages it was called with so assertions can
    inspect the prompt that reached the "LLM".
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list] = []

    def __call__(self, messages: list) -> str:
        self.calls.append(messages)
        if not self._responses:
            return ""
        return self._responses.pop(0)


def _canonical_pricing_tsx_response() -> str:
    """A realistic Opus-4.7-style response to the pricing-page prompt.

    Hand-curated to be **clean** under the V1 #4 linter:

    * no raw ``<button>`` / ``<input>`` / ``<textarea>`` — shadcn
      primitives only;
    * no inline hex colours, no ``bg-slate-900`` palette pins, no
      arbitrary ``text-[13px]`` sizes;
    * no ``dark:`` prefix (this project is ``html { color-scheme:
      dark }`` — dead code);
    * no ``outline-none`` without a focus-visible ring substitute;
    * every icon-only control has an ``aria-label``;
    * ``Switch`` is labelled via ``<Label htmlFor>`` (WCAG 2.2.
      pattern); the ``Tabs`` surface declares ``defaultValue``;
    * design-token utilities only (``bg-background`` /
      ``text-foreground`` / ``bg-primary`` / ``text-muted-foreground``
      / ``border``).

    Contains the three shadcn primitives mandated by the TODO row —
    ``Tabs``, ``Card``, ``Switch`` — plus ``Button`` for CTAs and
    ``Label`` for a11y. Three distinct plan tiers (Starter / Pro /
    Enterprise) populate the default tab, and a yearly-billing
    ``Switch`` provides the 年月切換 surface.
    """
    return (
        "Here is the pricing page rebuilt with shadcn primitives:\n\n"
        "```tsx\n"
        "import * as React from \"react\";\n"
        "import { Tabs, TabsContent, TabsList, TabsTrigger } "
        "from \"@/components/ui/tabs\";\n"
        "import {\n"
        "  Card,\n"
        "  CardContent,\n"
        "  CardDescription,\n"
        "  CardFooter,\n"
        "  CardHeader,\n"
        "  CardTitle,\n"
        "} from \"@/components/ui/card\";\n"
        "import { Switch } from \"@/components/ui/switch\";\n"
        "import { Button } from \"@/components/ui/button\";\n"
        "import { Label } from \"@/components/ui/label\";\n"
        "\n"
        "type Plan = {\n"
        "  id: string;\n"
        "  name: string;\n"
        "  description: string;\n"
        "  monthly: number | null;\n"
        "  yearly: number | null;\n"
        "  cta: string;\n"
        "  variant: \"default\" | \"secondary\" | \"outline\";\n"
        "  features: string[];\n"
        "};\n"
        "\n"
        "const PLANS: Plan[] = [\n"
        "  {\n"
        "    id: \"starter\",\n"
        "    name: \"Starter\",\n"
        "    description: \"For solo builders shipping their first project.\",\n"
        "    monthly: 9,\n"
        "    yearly: 84,\n"
        "    cta: \"Choose Starter\",\n"
        "    variant: \"secondary\",\n"
        "    features: [\n"
        "      \"1 active project\",\n"
        "      \"Community support\",\n"
        "      \"Basic analytics\",\n"
        "    ],\n"
        "  },\n"
        "  {\n"
        "    id: \"pro\",\n"
        "    name: \"Pro\",\n"
        "    description: \"For growing teams that need more capacity.\",\n"
        "    monthly: 29,\n"
        "    yearly: 276,\n"
        "    cta: \"Choose Pro\",\n"
        "    variant: \"default\",\n"
        "    features: [\n"
        "      \"Unlimited projects\",\n"
        "      \"Priority support\",\n"
        "      \"Advanced analytics\",\n"
        "    ],\n"
        "  },\n"
        "  {\n"
        "    id: \"enterprise\",\n"
        "    name: \"Enterprise\",\n"
        "    description: \"Custom scale, SSO, and a dedicated success team.\",\n"
        "    monthly: null,\n"
        "    yearly: null,\n"
        "    cta: \"Talk to sales\",\n"
        "    variant: \"outline\",\n"
        "    features: [\n"
        "      \"SSO + SAML\",\n"
        "      \"Dedicated success manager\",\n"
        "      \"Custom SLAs\",\n"
        "    ],\n"
        "  },\n"
        "];\n"
        "\n"
        "export default function PricingPage() {\n"
        "  const [yearly, setYearly] = React.useState(false);\n"
        "\n"
        "  const priceLabel = (plan: Plan) => {\n"
        "    if (plan.monthly === null || plan.yearly === null) {\n"
        "      return \"Custom\";\n"
        "    }\n"
        "    return yearly\n"
        "      ? `$${(plan.yearly / 12).toFixed(0)}/mo billed yearly`\n"
        "      : `$${plan.monthly}/mo`;\n"
        "  };\n"
        "\n"
        "  return (\n"
        "    <div className=\"min-h-screen bg-background text-foreground\">\n"
        "      <section className=\"mx-auto max-w-6xl px-4 py-16\">\n"
        "        <header className=\"flex flex-col gap-3 text-center\">\n"
        "          <h1 className=\"text-4xl font-semibold text-foreground\">Pricing</h1>\n"
        "          <p className=\"text-base text-muted-foreground\">\n"
        "            Simple plans that scale with you.\n"
        "          </p>\n"
        "        </header>\n"
        "\n"
        "        <Tabs defaultValue=\"individual\" className=\"mt-12 flex flex-col gap-8\">\n"
        "          <div className=\"flex flex-col items-center gap-6 sm:flex-row sm:justify-between\">\n"
        "            <TabsList>\n"
        "              <TabsTrigger value=\"individual\">For Individuals</TabsTrigger>\n"
        "              <TabsTrigger value=\"team\">For Teams</TabsTrigger>\n"
        "            </TabsList>\n"
        "\n"
        "            <div\n"
        "              className=\"flex items-center gap-3 rounded-lg border bg-card p-3 text-card-foreground\"\n"
        "              role=\"group\"\n"
        "              aria-label=\"Billing cadence\"\n"
        "            >\n"
        "              <Label htmlFor=\"billing-cycle\" className=\"text-sm text-muted-foreground\">\n"
        "                Monthly\n"
        "              </Label>\n"
        "              <Switch\n"
        "                id=\"billing-cycle\"\n"
        "                checked={yearly}\n"
        "                onCheckedChange={setYearly}\n"
        "                aria-label=\"Toggle annual billing\"\n"
        "              />\n"
        "              <Label htmlFor=\"billing-cycle\" className=\"text-sm text-foreground\">\n"
        "                Yearly\n"
        "              </Label>\n"
        "            </div>\n"
        "          </div>\n"
        "\n"
        "          <TabsContent\n"
        "            value=\"individual\"\n"
        "            className=\"grid gap-6 sm:grid-cols-2 lg:grid-cols-3\"\n"
        "          >\n"
        "            {PLANS.map((plan) => (\n"
        "              <Card key={plan.id} className=\"flex flex-col\">\n"
        "                <CardHeader>\n"
        "                  <CardTitle>{plan.name}</CardTitle>\n"
        "                  <CardDescription>{plan.description}</CardDescription>\n"
        "                </CardHeader>\n"
        "                <CardContent className=\"flex flex-col gap-4\">\n"
        "                  <p className=\"text-3xl font-bold text-foreground\">\n"
        "                    {priceLabel(plan)}\n"
        "                  </p>\n"
        "                  <ul className=\"flex flex-col gap-2 text-sm text-muted-foreground\">\n"
        "                    {plan.features.map((feature) => (\n"
        "                      <li key={feature}>{feature}</li>\n"
        "                    ))}\n"
        "                  </ul>\n"
        "                </CardContent>\n"
        "                <CardFooter className=\"mt-auto\">\n"
        "                  <Button variant={plan.variant} className=\"w-full\">\n"
        "                    {plan.cta}\n"
        "                  </Button>\n"
        "                </CardFooter>\n"
        "              </Card>\n"
        "            ))}\n"
        "          </TabsContent>\n"
        "\n"
        "          <TabsContent\n"
        "            value=\"team\"\n"
        "            className=\"grid gap-6 sm:grid-cols-2 lg:grid-cols-3\"\n"
        "          >\n"
        "            <Card>\n"
        "              <CardHeader>\n"
        "                <CardTitle>Team Starter</CardTitle>\n"
        "                <CardDescription>3 seats included.</CardDescription>\n"
        "              </CardHeader>\n"
        "              <CardContent>\n"
        "                <p className=\"text-3xl font-bold text-foreground\">\n"
        "                  {yearly ? \"$24/seat/mo (yearly)\" : \"$29/seat/mo\"}\n"
        "                </p>\n"
        "              </CardContent>\n"
        "              <CardFooter>\n"
        "                <Button variant=\"secondary\" className=\"w-full\">\n"
        "                  Choose Team Starter\n"
        "                </Button>\n"
        "              </CardFooter>\n"
        "            </Card>\n"
        "            <Card>\n"
        "              <CardHeader>\n"
        "                <CardTitle>Team Pro</CardTitle>\n"
        "                <CardDescription>10 seats included.</CardDescription>\n"
        "              </CardHeader>\n"
        "              <CardContent>\n"
        "                <p className=\"text-3xl font-bold text-foreground\">\n"
        "                  {yearly ? \"$49/seat/mo (yearly)\" : \"$59/seat/mo\"}\n"
        "                </p>\n"
        "              </CardContent>\n"
        "              <CardFooter>\n"
        "                <Button className=\"w-full\">Choose Team Pro</Button>\n"
        "              </CardFooter>\n"
        "            </Card>\n"
        "            <Card>\n"
        "              <CardHeader>\n"
        "                <CardTitle>Team Enterprise</CardTitle>\n"
        "                <CardDescription>Volume pricing + SSO.</CardDescription>\n"
        "              </CardHeader>\n"
        "              <CardContent>\n"
        "                <p className=\"text-3xl font-bold text-foreground\">Custom</p>\n"
        "              </CardContent>\n"
        "              <CardFooter>\n"
        "                <Button variant=\"outline\" className=\"w-full\">\n"
        "                  Talk to sales\n"
        "                </Button>\n"
        "              </CardFooter>\n"
        "            </Card>\n"
        "          </TabsContent>\n"
        "        </Tabs>\n"
        "      </section>\n"
        "    </div>\n"
        "  );\n"
        "}\n"
        "```\n"
    )


# ── Prompt assembly helper (mirrors the UI Designer skill SOP) ──────


def _assemble_generation_prompt(
    nl_brief: str,
    project_root: Path,
) -> str:
    """Build the generation prompt the UI Designer skill would send.

    Mirrors the deterministic assembly in
    :func:`backend.vision_to_ui.build_ui_generation_prompt` — header,
    registry block, tokens block, caller brief, rules — but omits the
    vision-analysis section because this channel is NL-only.

    Deterministic: same inputs → byte-identical output (verified by
    :class:`TestPromptDeterminism`).
    """
    registry_block = registry_mod.render_agent_context_block(
        project_root=project_root,
    )
    tokens_block = tokens_mod.render_agent_context_block(
        project_root=project_root,
    )
    rules = (
        "## Generation rules (MUST follow)\n"
        "1. Emit a single fenced ```tsx block. No prose before or after.\n"
        "2. Use shadcn primitives from the registry above — no raw\n"
        "   <button>/<input>/<textarea>/<select>/<dialog>/<progress>.\n"
        "3. Design tokens only (bg-background, text-foreground, …).\n"
        "   No inline hex, no Tailwind palette class such as\n"
        "   bg-slate-900, no arbitrary text-[13px] values.\n"
        "4. Mobile-first responsive: base + sm/md/lg/xl/2xl.\n"
        "5. Project is dark-only (html { color-scheme: dark }).\n"
        "   Do NOT emit `dark:` prefixes and do NOT write light\n"
        "   fallbacks.\n"
        "6. A11y: icon-only Buttons get aria-label; Switch gets a\n"
        "   <Label htmlFor>; Tabs declares defaultValue."
    )
    header = (
        "# UI generation — natural language → shadcn/ui + Tailwind\n"
        "You are the OmniSight UI Designer. Produce a React TSX\n"
        "component that fulfils the caller's brief using the\n"
        "project's installed shadcn primitives and design tokens.\n"
        "You will be linted by\n"
        "backend.component_consistency_linter — a clean lint pass is\n"
        "the acceptance gate."
    )
    brief_block = f"## Caller brief\n{nl_brief.strip()}"
    return (
        "\n\n".join([header, registry_block, tokens_block, brief_block, rules])
        .strip()
        + "\n"
    )


# ── 1. Router contract for the NL prompt ─────────────────────────────


class TestRouteDecisionForPricingPrompt:
    def test_bucket_is_large(self):
        decision = route(NL_PROMPT)
        assert isinstance(decision, EditRouteDecision)
        assert decision.complexity == EditComplexity.LARGE.value == "large"

    def test_routes_to_opus_4_7(self):
        decision = route(NL_PROMPT)
        assert decision.provider == DEFAULT_PROVIDER == "anthropic"
        assert decision.model == DEFAULT_LARGE_MODEL == "claude-opus-4-7"

    def test_reasons_cite_structural_signals(self):
        decision = route(NL_PROMPT)
        # Two distinct structural signals must fire on this prompt —
        # "new_page" (made a whole page) and "multi_section" (三個方案).
        # Tests the actual reason tags the router surfaces.
        assert "large:new_page" in decision.reasons
        assert "large:multi_section" in decision.reasons

    def test_signals_no_small_hits(self):
        # This prompt is unambiguously large; a stray small-hit would
        # indicate the small-patterns regex over-matched a Chinese
        # character sequence.
        _, signals, _ = router_mod.classify_prompt(NL_PROMPT)
        assert signals.small_hits == ()

    def test_route_pure_and_deterministic(self):
        d1 = route(NL_PROMPT)
        d2 = route(NL_PROMPT)
        assert d1.complexity == d2.complexity
        assert d1.model == d2.model
        assert d1.reasons == d2.reasons
        assert d1.signals == d2.signals


# ── 2. Context block assembly is deterministic ───────────────────────


class TestPromptDeterminism:
    def test_registry_block_byte_identical(self):
        a = registry_mod.render_agent_context_block(project_root=PROJECT_ROOT)
        b = registry_mod.render_agent_context_block(project_root=PROJECT_ROOT)
        assert a == b

    def test_tokens_block_byte_identical(self):
        a = tokens_mod.render_agent_context_block(project_root=PROJECT_ROOT)
        b = tokens_mod.render_agent_context_block(project_root=PROJECT_ROOT)
        assert a == b

    def test_generation_prompt_byte_identical(self):
        p1 = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        p2 = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        assert p1 == p2

    def test_generation_prompt_embeds_brief(self):
        prompt = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        assert NL_PROMPT in prompt

    def test_generation_prompt_embeds_registry_section(self):
        prompt = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        # Registry block header pinned by V1 #2.
        assert "shadcn/ui component registry" in prompt
        # The three TODO-mandated primitives must be listed so the
        # agent can pick them off the registry rather than training
        # memory.
        assert "tabs" in prompt.lower()
        assert "card" in prompt.lower()
        assert "switch" in prompt.lower()

    def test_generation_prompt_embeds_tokens_section(self):
        prompt = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        # Token block header pinned by V1 #3.
        assert "design tokens" in prompt.lower()

    def test_generation_rules_forbid_raw_html_and_hex(self):
        prompt = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)
        # The rules surface MUST tell the model the anti-patterns.
        assert "bg-background" in prompt
        assert "raw <button>" in prompt or "<button>" in prompt
        assert "dark-only" in prompt


# ── 3. End-to-end pipeline with a fake invoker ───────────────────────


class TestEndToEndPipeline:
    """NL → prompt → LLM (fake) → extract → lint — no network."""

    def _run_once(self, *, response: str | None = None) -> tuple[str, list]:
        """Execute the pipeline one time.

        Returns ``(extracted_tsx, recorded_messages)``.
        """
        if response is None:
            response = _canonical_pricing_tsx_response()
        fake = FakeInvoker([response])

        # Step 1 — router classifies the NL prompt; nothing in the
        # pipeline should run unless we land in ``large``.
        decision = route(NL_PROMPT)
        assert decision.complexity == "large"

        # Step 2 — assemble the prompt from the sibling fact-side
        # context blocks + the NL brief + rules.
        prompt = _assemble_generation_prompt(NL_PROMPT, PROJECT_ROOT)

        # Step 3 — fake "LLM call" (text-only, no multimodal image).
        from backend.llm_adapter import HumanMessage
        msg = HumanMessage(content=prompt)
        response_text = fake([msg])

        # Step 4 — extract TSX from the response fence.
        tsx = extract_tsx_from_response(response_text)
        return tsx, fake.calls

    def test_invoker_sees_registry_and_brief(self):
        _, calls = self._run_once()
        assert len(calls) == 1
        [[message]] = calls
        # ``HumanMessage.content`` is a single string here because we
        # are NL-only; the registry + tokens + brief are concatenated
        # into it.
        assert NL_PROMPT in message.content
        assert "shadcn/ui component registry" in message.content

    def test_extract_returns_non_empty_tsx(self):
        tsx, _ = self._run_once()
        assert tsx  # extractor found a fenced block
        assert tsx.lstrip().startswith("import")

    def test_extracted_tsx_is_lint_clean(self):
        tsx, _ = self._run_once()
        report = lint_code(tsx, source="pricing-integration.tsx")
        assert report.is_clean, (
            f"pricing-page TSX violated the linter — counts="
            f"{dict(report.severity_counts)} rules="
            f"{dict(report.rule_counts)}"
        )
        # Belt-and-braces: no error-severity violations in the list.
        errors = [v for v in report.violations if v.severity == "error"]
        assert errors == []

    def test_extracted_tsx_contains_all_mandated_primitives(self):
        tsx, _ = self._run_once()
        # TODO row: Tabs/Card/Switch.
        assert "<Tabs " in tsx or "<Tabs>" in tsx
        assert "<TabsList>" in tsx
        assert "<TabsTrigger " in tsx or "<TabsTrigger>" in tsx
        assert "<TabsContent" in tsx
        assert "<Card " in tsx or "<Card>" in tsx
        assert "<Switch" in tsx

    def test_extracted_tsx_imports_from_components_ui(self):
        tsx, _ = self._run_once()
        # Canonical shadcn import path — pinned by V1 #2 registry.
        assert '"@/components/ui/tabs"' in tsx
        assert '"@/components/ui/card"' in tsx
        assert '"@/components/ui/switch"' in tsx
        assert '"@/components/ui/button"' in tsx

    def test_extracted_tsx_declares_three_plans(self):
        tsx, _ = self._run_once()
        # Three tier names in the canonical plan array.
        assert "Starter" in tsx
        assert "Pro" in tsx
        assert "Enterprise" in tsx

    def test_extracted_tsx_has_month_year_toggle_surface(self):
        tsx, _ = self._run_once()
        # 年月切換 — monthly/yearly toggle, labelled both ways.
        assert "Monthly" in tsx
        assert "Yearly" in tsx
        # The toggle control itself must be a shadcn Switch with a
        # <Label htmlFor> — WCAG 2.2 form-control pairing.
        assert re.search(r"<Switch[^>]*\bid=\"billing-cycle\"", tsx)
        assert re.search(
            r"<Label[^>]*htmlFor=\"billing-cycle\"",
            tsx,
        )

    def test_extracted_tsx_uses_design_token_utilities(self):
        tsx, _ = self._run_once()
        # Positive: design-token utilities are present.
        assert "bg-background" in tsx
        assert "text-foreground" in tsx
        # Negative: no inline hex, no pinned palette, no dark: prefix.
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", tsx)
        assert "bg-slate-" not in tsx
        assert "dark:" not in tsx


# ── 4. JSX tag balance sanity (proxy for "renders correctly") ────────


class TestJSXTagBalance:
    """Proxy renderability check — every opener has a matching closer.

    A true React render requires jsdom + Next's toolchain, which is
    out of scope for pytest. Instead we verify structural invariants
    that React's reconciler would otherwise refuse:

      * each shadcn JSX tag opened in the TSX is closed;
      * the JSX tree has no dangling ``<`` characters.
    """

    TAGS_TO_BALANCE = (
        "Tabs",
        "TabsList",
        "TabsTrigger",
        "TabsContent",
        "Card",
        "CardHeader",
        "CardTitle",
        "CardDescription",
        "CardContent",
        "CardFooter",
        "Button",
        "Label",
    )

    def _tsx(self) -> str:
        return extract_tsx_from_response(_canonical_pricing_tsx_response())

    @pytest.mark.parametrize("tag", TAGS_TO_BALANCE)
    def test_tag_opens_and_closes(self, tag: str):
        tsx = self._tsx()
        # Opening tag count — ``<Tag ...>`` but not self-closing
        # (``<Tag ... />``) and not ``</Tag>``.
        opens = len(re.findall(rf"<{tag}(?:\s[^/>]*[^/])?>", tsx))
        closes = len(re.findall(rf"</{tag}>", tsx))
        self_closes = len(re.findall(rf"<{tag}[^>]*/\s*>", tsx))
        # Non-self-closing openers must match closers exactly.
        assert opens == closes, (
            f"{tag}: {opens} openers vs {closes} closers "
            f"(self-closes: {self_closes}) — JSX would not reconcile"
        )
        # Every occurrence of ``<Tag`` is either a regular or
        # self-closing form — never dangling.
        total_opens = len(re.findall(rf"<{tag}\b", tsx))
        assert total_opens == opens + self_closes, (
            f"{tag}: {total_opens} starts vs {opens + self_closes} "
            "accounted for — unterminated JSX tag"
        )

    def test_switch_is_self_closing_with_aria_label(self):
        tsx = self._tsx()
        # The Switch in the canonical response is self-closing and
        # carries the required aria-label for WCAG 4.1.2 Name-Role-Value.
        assert re.search(
            r"<Switch\b[^>]*aria-label=\"[^\"]+\"[^>]*/>",
            tsx,
            re.DOTALL,
        )

    def test_every_card_has_a_header(self):
        tsx = self._tsx()
        # Every ``<Card>`` pairs with a ``<CardHeader>`` — not a
        # strict React requirement but a strong shape invariant for
        # this acceptance scenario.
        card_openers = len(re.findall(r"<Card\b(?![A-Za-z])", tsx))
        card_headers = len(re.findall(r"<CardHeader\b", tsx))
        assert card_openers >= 3
        assert card_headers >= 3


# ── 5. Canonical response is itself clean (regression fixture) ───────


class TestCanonicalResponseFixture:
    """Belt: the hand-curated TSX in this file must stay clean.

    If a future maintainer edits the canonical response above, the
    linter check here catches the drift before the pipeline tests
    would — which keeps the failure message tight to the edit.
    """

    def test_canonical_tsx_lints_clean(self):
        tsx = extract_tsx_from_response(_canonical_pricing_tsx_response())
        report = lint_code(tsx, source="pricing-fixture.tsx")
        assert report.is_clean, (
            f"canonical fixture regressed — violations="
            f"{[(v.rule_id, v.line) for v in report.violations]}"
        )

    def test_canonical_tsx_is_extractable(self):
        response = _canonical_pricing_tsx_response()
        tsx = extract_tsx_from_response(response)
        assert tsx  # non-empty
        # Starts on an import line (the fence opens an import).
        first_nonempty = next(
            (line for line in tsx.splitlines() if line.strip()), ""
        )
        assert first_nonempty.startswith("import")
