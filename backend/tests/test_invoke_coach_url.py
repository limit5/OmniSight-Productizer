"""W16.1 — URL detection coaching trigger unit tests.

Scope
─────
Locks the W16.1 helpers in ``backend/routers/invoke.py`` that detect
``http(s)://`` URLs in the operator's INVOKE command and turn them into
the four-option coach menu (clone / brand / screenshot / skip) backed by
the W11–W13 capabilities.

Coverage axes:

  1.  ``_detect_urls_in_text`` matches ``http(s)://`` URLs while skipping
      bare-domain mentions, deduplicates, preserves order, strips
      trailing punctuation, and caps at ``_MAX_URL_TRIGGERS``.
  2.  ``_detect_coaching_triggers`` emits one ``url_in_message:<url>``
      trigger per URL pasted into ``command`` (sorted by paste order),
      honours per-URL ``suppress``, and stays a no-op for URL-free
      commands.
  3.  ``_build_templated_coach_message`` renders the 1-of-1 URL menu
      with the four bilingual options + slash commands carrying the
      full URL, and overrides the legacy ``empty_workspace`` framing
      because the operator declared intent by pasting.
  4.  ``_build_templated_coach_message`` renders the multi-URL menu
      under per-URL sub-headings while keeping the four options for
      each.
  5.  ``_build_templated_coach_message`` keeps the ``missing_toolchain``
      banner first when both fire, appends the URL menu as a secondary
      section, and still appends the stale-PEP reminder.
  6.  ``_build_coach_context`` (LLM-driven path) hands the LLM a single
      URL bullet that pre-renders the slash commands so the LLM never
      has to invent the syntax.
  7.  ``_truncate_url_for_display`` keeps short URLs intact and clamps
      long ones to ``_MAX_URL_DISPLAY_CHARS`` while preserving the full
      URL inside the trigger key.

These tests are PG-free and LLM-free — every helper under test is a
pure function so the tests stay fast and don't need fixtures.

Module-global / cross-worker state audit (per
docs/sop/implement_phase_step.md Step 1): ``_URL_PATTERN`` is a frozen
compiled regex; every uvicorn worker derives the same value from
source code (Answer #1). No mutable module-level state on the test
side either.
"""

from __future__ import annotations


from backend.routers import invoke as inv


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  1) _detect_urls_in_text — regex contract
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDetectUrlsInText:

    def test_strict_scheme_required(self):
        # Bare domains and path-only references must NOT match — the
        # toolchain coach already absorbs those false positives, the URL
        # coach is strictly for pasted ``http(s)://`` links.
        assert inv._detect_urls_in_text("esp32-c3.local on /var/log") == []
        assert inv._detect_urls_in_text("see example.com/page") == []

    def test_https_and_http_match(self):
        assert inv._detect_urls_in_text("plain http://x.test/abc") == [
            "http://x.test/abc",
        ]
        assert inv._detect_urls_in_text("secure HTTPS://Y.test/Q") == [
            "HTTPS://Y.test/Q",
        ]

    def test_trailing_punctuation_stripped(self):
        # Operators paste URLs inside sentences; the trigger key must
        # not carry the trailing ``.`` / ``,`` / ``)`` etc. or the W11
        # router would reject the URL on shape validation.
        out = inv._detect_urls_in_text(
            "go https://a.com/page?q=1, then https://b.com/x.",
        )
        assert out == ["https://a.com/page?q=1", "https://b.com/x"]

    def test_dedup_preserves_first_occurrence_order(self):
        out = inv._detect_urls_in_text(
            "look at https://b.com then https://a.com and https://b.com again",
        )
        assert out == ["https://b.com", "https://a.com"]

    def test_cap_at_max_url_triggers(self):
        # 4 distinct URLs → only first 3 emitted (hard cap protects the
        # coach card / LLM context from a runaway paste).
        text = " ".join(f"https://host{i}.com/p" for i in range(5))
        out = inv._detect_urls_in_text(text)
        assert len(out) == inv._MAX_URL_TRIGGERS
        assert out == [f"https://host{i}.com/p" for i in range(inv._MAX_URL_TRIGGERS)]

    def test_empty_or_none_input(self):
        assert inv._detect_urls_in_text("") == []
        assert inv._detect_urls_in_text(None) == []
        assert inv._detect_urls_in_text("   ") == []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  2) _detect_coaching_triggers — emit / suppress / no-op
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _empty_state(installed=frozenset()):
    return {
        "agents": [],
        "tasks": [],
        "running_agents": [],
        "idle_agents": [],
        "installed_entries": installed,
    }


class TestDetectCoachingTriggersUrl:

    def test_url_in_command_emits_trigger(self):
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(),
            command="please clone https://example.com/landing for me",
        )
        assert "url_in_message:https://example.com/landing" in triggers

    def test_url_free_command_emits_no_url_trigger(self):
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(), command="just a text command",
        )
        assert all(not t.startswith("url_in_message:") for t in triggers)

    def test_per_url_suppress(self):
        # Operator dismissed a specific URL earlier — re-emitting that
        # exact URL must NOT re-coach. A different URL in the same
        # command still fires because suppress is per-URL, not per-
        # trigger-family.
        suppress = frozenset({"url_in_message:https://a.com"})
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), suppress,
            command="compare https://a.com against https://b.com please",
        )
        assert "url_in_message:https://a.com" not in triggers
        assert "url_in_message:https://b.com" in triggers

    def test_multiple_urls_emit_in_paste_order(self):
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(),
            command="ref https://b.com then https://a.com and https://c.com",
        )
        url_triggers = [t for t in triggers if t.startswith("url_in_message:")]
        assert url_triggers == [
            "url_in_message:https://b.com",
            "url_in_message:https://a.com",
            "url_in_message:https://c.com",
        ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  3) _build_templated_coach_message — single URL leads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachUrlSingle:

    def test_single_url_overrides_empty_workspace_with_four_options(self):
        # URL menu lands BETWEEN missing_toolchain and empty_workspace
        # in priority. With only empty_workspace + url co-firing, the
        # URL menu leads and the empty-workspace prompts are skipped.
        msg = inv._build_templated_coach_message(
            triggers=[
                "empty_workspace",
                "url_in_message:https://example.com/landing",
            ],
            pending_count=0,
        )
        # All four bilingual options must be rendered as their own bullets.
        assert "(a) 克隆網站 / Clone" in msg
        assert "(b) 抽取品牌風格 / Extract brand" in msg
        assert "(c) 多斷點截圖 / Screenshot" in msg
        assert "(d) 不用 / Skip" in msg
        # Slash commands carry the full URL — operators copy-paste the
        # bullet into the chat, a truncated URL would 4xx the W11/W12/
        # W13 routers.
        assert "/clone https://example.com/landing" in msg
        assert "/brand https://example.com/landing" in msg
        assert "/screenshot https://example.com/landing" in msg
        # empty_workspace framing must NOT appear (operator declared
        # intent by pasting, install-first-then-run analogue).
        assert "工作台是空的喔" not in msg
        assert "/tour" not in msg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  4) _build_templated_coach_message — multi-URL menu
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachUrlMulti:

    def test_multi_url_emits_per_url_subheading_and_options(self):
        msg = inv._build_templated_coach_message(
            triggers=[
                "url_in_message:https://a.com",
                "url_in_message:https://b.com",
            ],
            pending_count=0,
        )
        # Sub-heading per URL with the full URL displayed.
        assert "URL #1: https://a.com" in msg
        assert "URL #2: https://b.com" in msg
        # Each URL gets its own slash-command bullets.
        assert msg.count("/clone https://a.com") == 1
        assert msg.count("/clone https://b.com") == 1
        assert msg.count("/brand https://a.com") == 1
        assert msg.count("/brand https://b.com") == 1
        assert msg.count("/screenshot https://a.com") == 1
        assert msg.count("/screenshot https://b.com") == 1
        # Skip option is universal so it appears once per URL block.
        assert msg.count("(d) 不用 / Skip") == 2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  5) _build_templated_coach_message — toolchain leads, URL trails
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTemplatedCoachUrlWithToolchain:

    def test_missing_toolchain_leads_url_appended_pep_reminder(self):
        # Toolchain install is the binding constraint — must lead. URL
        # menu is appended as a secondary section so the operator sees
        # both sets of follow-ups in one card. Stale-PEP reminder still
        # appended at the end.
        msg = inv._build_templated_coach_message(
            triggers=[
                "stale_pep",
                "missing_toolchain:nodejs-lts-20",
                "url_in_message:https://example.com",
            ],
            pending_count=2,
        )
        # Toolchain headline first.
        nodejs_idx = msg.find("Node.js LTS 20")
        url_intro_idx = msg.find("裝完 toolchain 之後")
        assert 0 <= nodejs_idx < url_intro_idx, (
            "Node.js display name must appear before the URL appendix intro"
        )
        # URL menu appears with full slash commands.
        assert "/clone https://example.com" in msg
        assert "/brand https://example.com" in msg
        assert "/screenshot https://example.com" in msg
        # PEP reminder still rendered as ONE additional line.
        reminder_lines = [
            line for line in msg.splitlines()
            if "PEP HOLD" in line and "2" in line
        ]
        assert len(reminder_lines) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  6) _build_coach_context — LLM-driven path
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestBuildCoachContextUrl:

    def test_url_block_pre_renders_all_three_slash_commands(self):
        block = inv._build_coach_context(
            triggers=["url_in_message:https://example.com/landing"],
            pending_count=0,
        )
        # The LLM must see the full slash-command syntax for each
        # capability; otherwise it has to invent the command names.
        assert "/clone https://example.com/landing" in block
        assert "/brand https://example.com/landing" in block
        assert "/screenshot https://example.com/landing" in block
        # The display URL is rendered for the headline.
        assert "https://example.com/landing" in block


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  7) _truncate_url_for_display — display vs trigger-key invariant
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTruncateUrlForDisplay:

    def test_short_url_unchanged(self):
        short = "https://example.com/abc"
        assert inv._truncate_url_for_display(short) == short

    def test_long_url_truncated_with_ellipsis(self):
        long_url = "https://example.com/" + ("x" * 200)
        out = inv._truncate_url_for_display(long_url)
        assert len(out) == inv._MAX_URL_DISPLAY_CHARS
        assert out.endswith("…")

    def test_full_url_preserved_in_trigger_key(self):
        # The trigger key carries the full URL so the suppress system
        # and the coach-rendered slash command both reference the
        # exact paste — only the display headline is truncated.
        long_url = "https://example.com/" + ("x" * 200)
        triggers, _ = inv._detect_coaching_triggers(
            _empty_state(), frozenset(),
            command=f"check {long_url}",
        )
        url_triggers = [t for t in triggers if t.startswith("url_in_message:")]
        assert url_triggers == [f"url_in_message:{long_url}"]
        msg = inv._build_templated_coach_message(url_triggers, 0)
        # Slash command always uses full URL.
        assert f"/clone {long_url}" in msg
        # Display headline is truncated.
        assert "…" in msg
