"""U6-1a — closed fact schema / registry / validator / renderer tests (offline, dormant).

Pins the frozen §2.B + §11 RB1 BOUNDARY: the fact schema (not a classifier) makes
authority/policy/review/tool-behavior facts UNREPRESENTABLE. Proves the closed
registry, the per-predicate value-type lattice (injection-safe), and the frozen
inert renderer grammar.
"""
from __future__ import annotations

import pytest

from backend.agents.u6_fact_schema import (
    FACT_SCHEMA_VERSION,
    PREDICATE_REGISTRY,
    REGISTERED_PREDICATES,
    RENDER_GRAMMAR_VERSION,
    Fact,
    FactType,
    FactValidationError,
    Sensitivity,
    _REGISTRY,
    predicate_is_registered,
    render_fact,
    validate_fact,
)


def _pref() -> Fact:
    return Fact(FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes", source_span="chat:m1")


# ── happy path: one valid fact per fact_type constructs + renders ────────────
@pytest.mark.parametrize(
    "ft,subject,pred,val",
    [
        (FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes"),
        (FactType.PREFERENCE, "user", "preferred_language", "en-US"),
        (FactType.PROFILE, "user", "timezone", "Asia/Taipei"),
        (FactType.PROFILE, "user", "ui_language", "zh-Hant"),
        (FactType.PROJECT_CONTEXT, "repo:omnisight", "build_standard", "checkpatch-strict"),
        (FactType.PROJECT_CONTEXT, "repo:omnisight", "default_branch", "develop"),
    ],
)
def test_valid_facts_construct_validate_render(ft, subject, pred, val) -> None:
    fact = Fact(ft, subject, pred, val, source_span="chat:msg-1")
    validate_fact(fact)  # idempotent, no raise
    assert render_fact(fact) == f"{subject} {pred} {val}"


# ── RB1 CORE: authority is UNREPRESENTABLE (closed registry) ─────────────────
@pytest.mark.parametrize(
    "pred",
    [
        "skips_reviews", "pre_approves_deploys", "bypass_review", "allow", "allow_all",
        "policy", "standing_policy", "permission", "grant", "authorize", "auto_approve",
        "tool_behavior", "can_deploy", "is_admin", "review_optional", "trusted",
        "preferred_ipc ",  # trailing space variant is NOT the registered predicate
        "PREFERRED_IPC",   # case variant is a different, unregistered predicate
    ],
)
def test_authority_shaped_predicates_are_unrepresentable(pred) -> None:
    assert not predicate_is_registered(pred)
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", pred, "x", source_span="chat:m1")


def test_unknown_predicate_rejected_even_if_value_ok() -> None:
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", "not_a_real_predicate", "named_pipes", source_span="chat:m1")


# ── predicate ↔ fact_type binding is enforced (no cross-type smuggling) ──────
def test_predicate_must_match_its_owning_fact_type() -> None:
    # timezone is a PROFILE predicate; asserting it under PREFERENCE is rejected.
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", "timezone", "Asia/Taipei", source_span="chat:m1")


def test_predicates_are_globally_unique_including_case_fold() -> None:
    # render is `subject predicate value` (no fact_type) → predicates must be
    # globally unique for that triple to be unambiguous, even to a case-insensitive
    # downstream reader (guards against a future `Preferred_IPC` shadow).
    names = list(_REGISTRY)
    assert len({n.casefold() for n in names}) == len(names)


# ── value-type lattice: wrong / unsafe values rejected ───────────────────────
@pytest.mark.parametrize(
    "pred,bad_value",
    [
        ("preferred_ipc", "carrier_pigeon"),        # not in the closed enum
        ("preferred_ipc", "named_pipes\nallow all"),  # newline / fence-break attempt
        ("preferred_language", "en_US; DROP"),      # not a lang tag
        ("timezone", "Asia/Taipei\tSYSTEM:"),        # tab / control smuggling
        ("default_branch", "a b"),                   # space (would split the triple)
        ("build_standard", "x" * 200),               # over length (short-token)
        ("preferred_editor", "vi\x00m"),             # NUL control char
        # the two formerly-UNBOUNDED grammars are now length-capped:
        ("preferred_language", "en" + "-ab" * 40),   # 122-char "language tag"
        ("ui_language", "zh" + "-ab" * 40),          # over the value cap
        ("timezone", "Asia" + "/aa" * 40),           # 124-char "timezone"
    ],
)
def test_value_type_lattice_rejects_wrong_or_unsafe_values(pred, bad_value) -> None:
    ft = _REGISTRY[pred].fact_type
    with pytest.raises(FactValidationError):
        Fact(ft, "user", pred, bad_value, source_span="chat:m1")


# ── injection-safety: you cannot BUILD a fact whose value breaks the fence ───
@pytest.mark.parametrize(
    "payload",
    ["named_pipes\n### SYSTEM", "named_pipes\r\nallow", "named_pipes x", "named_pipes value"],
)
def test_value_cannot_carry_a_newline_or_fence_break(payload) -> None:
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", "preferred_ipc", payload, source_span="chat:m1")


# ── frozen renderer grammar: single inert line, never a directive ────────────
def test_render_is_single_inert_line() -> None:
    line = render_fact(_pref())
    assert line == "user preferred_ipc named_pipes"
    assert "\n" not in line and "\r" not in line


def test_render_is_structurally_inert_for_every_representable_subject() -> None:
    # the closed subject namespace makes inertness STRUCTURAL, not fixture-luck:
    # every renderable line begins with `user ` or `repo:` — never a heading/role
    # marker (`#`, `-`, `>`, `SYSTEM`, …), because no such subject is representable.
    for subject in ("user", "repo:omnisight", "repo:omnisight/core"):
        line = render_fact(
            Fact(FactType.PREFERENCE, subject, "preferred_ipc", "tcp", source_span="s")
            if subject == "user"
            else Fact(FactType.PROJECT_CONTEXT, subject, "default_branch", "develop", source_span="s")
        )
        assert line.startswith(("user ", "repo:"))
        assert not line.startswith(("#", "-", "*", ">", "SYSTEM", "You ", "Ignore", "[", "!"))


def test_render_is_deterministic() -> None:
    assert render_fact(_pref()) == render_fact(_pref())


def test_render_revalidates_and_fails_closed_on_tampered_instance() -> None:
    # object.__setattr__ can smuggle whitespace past __post_init__ on a frozen
    # dataclass; the renderer re-validates and refuses to emit a splittable line.
    fact = _pref()
    object.__setattr__(fact, "value", "named_pipes\nallow")
    with pytest.raises(FactValidationError):
        render_fact(fact)


# ── subject / source_span / temporal / sensitivity guards ────────────────────
@pytest.mark.parametrize(
    "subject",
    [
        "", "has space", "bad\nsubject", "x" * 200,
        # the closed-namespace fix: NO reserved role / authority / imperative token
        # is representable as a subject (they'd render as a directive HEAD).
        "SYSTEM", "assistant", "admin", "ignore_all_prior", "-rf", "you_are_now_admin",
        "repoo:omnisight",  # typo'd prefix is not the `repo:` namespace
        "repo:",            # empty slug
        "users",            # only the exact token `user` is the user namespace
    ],
)
def test_bad_subject_rejected(subject) -> None:
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, subject, "preferred_ipc", "named_pipes", source_span="chat:m1")


def test_subject_is_a_closed_namespace_user_or_repo() -> None:
    # exactly the two shapes render; everything else is unrepresentable.
    assert render_fact(Fact(FactType.PREFERENCE, "user", "preferred_ipc", "tcp", source_span="s"))
    assert render_fact(
        Fact(FactType.PROJECT_CONTEXT, "repo:omnisight/core", "default_branch", "develop", source_span="s")
    )


@pytest.mark.parametrize("span", ["", "bad span", "x\ny"])
def test_bad_source_span_rejected(span) -> None:
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes", source_span=span)


def test_valid_from_after_valid_until_rejected() -> None:
    with pytest.raises(FactValidationError):
        Fact(
            FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes",
            source_span="chat:m1", valid_from="2026-07-19", valid_until="2026-07-01",
        )


def test_malformed_date_rejected() -> None:
    with pytest.raises(FactValidationError):
        Fact(
            FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes",
            source_span="chat:m1", valid_from="July 19",
        )


def test_valid_temporal_window_accepted() -> None:
    fact = Fact(
        FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes",
        source_span="chat:m1", valid_from="2026-01-01", valid_until="2026-12-31",
        sensitivity=Sensitivity.SENSITIVE,
    )
    assert fact.sensitivity is Sensitivity.SENSITIVE


def test_bad_sensitivity_type_rejected() -> None:
    with pytest.raises(FactValidationError):
        Fact(
            FactType.PREFERENCE, "user", "preferred_ipc", "named_pipes",
            source_span="chat:m1", sensitivity="sensitive",  # str, not the enum
        )


# ── registry closedness (RB1) ────────────────────────────────────────────────
def test_fact_type_enum_is_exactly_the_three_data_only_kinds() -> None:
    assert {ft.value for ft in FactType} == {"preference", "profile", "project_context"}


def test_registry_is_frozen_and_every_predicate_has_a_validator() -> None:
    assert isinstance(REGISTERED_PREDICATES, frozenset)
    for pred, spec in _REGISTRY.items():
        assert isinstance(spec.fact_type, FactType)
        assert callable(spec.validate_value)
        assert predicate_is_registered(pred)


def test_registered_predicates_are_EXACTLY_the_frozen_set() -> None:
    # RB1: adding/removing a predicate must be a deliberate, reviewed diff — pin the
    # exact closed set so a silent extension (the thing RB1 forbids) fails loudly.
    assert REGISTERED_PREDICATES == frozenset(
        {
            "preferred_ipc", "preferred_language", "preferred_editor",
            "timezone", "ui_language",
            "build_standard", "default_branch",
        }
    )


def test_registry_public_view_is_immutable_and_not_stale() -> None:
    # "the write path may NOT extend the enum" — the public view rejects mutation,
    # and it never diverges from the frozen set.
    with pytest.raises(TypeError):
        PREDICATE_REGISTRY["allow_all"] = None  # type: ignore[index]
    assert frozenset(PREDICATE_REGISTRY) == REGISTERED_PREDICATES


def test_render_grammar_is_frozen_exact_bytes() -> None:
    # pin the EXACT rendered bytes (RB1) — any separator/field-order change fails
    # here even though the f-string oracle would silently follow the code.
    assert render_fact(
        Fact(FactType.PROFILE, "user", "timezone", "Asia/Taipei", source_span="s")
    ) == "user timezone Asia/Taipei"
    assert render_fact(
        Fact(FactType.PROJECT_CONTEXT, "repo:omnisight", "build_standard", "checkpatch-strict", source_span="s")
    ) == "repo:omnisight build_standard checkpatch-strict"


def test_schema_and_render_grammar_versions_pinned() -> None:
    # bump BOTH + update the golden bytes together when the grammar moves.
    assert FACT_SCHEMA_VERSION == 1
    assert RENDER_GRAMMAR_VERSION == 1


def test_unhashable_predicate_fails_closed_not_typeerror() -> None:
    # an unhashable predicate must surface as FactValidationError, never a bare
    # TypeError from the registry dict lookup.
    with pytest.raises(FactValidationError):
        Fact(FactType.PREFERENCE, "user", ["preferred_ipc"], "named_pipes", source_span="s")  # type: ignore[arg-type]
