"""Property-based tests for ``backend.routers.invoke`` public contracts."""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.models import TaskPriority
from backend.routers import invoke as inv


SAFE_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=2048,
)
URL_HOST = st.lists(
    st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=12),
    min_size=2,
    max_size=4,
).map(".".join)
URL_PATH_BODY = st.text(
    alphabet=string.ascii_letters + string.digits + "-_/%?=&",
    max_size=79,
)
URL_PATH = st.builds(
    lambda body, tail: f"{body}{tail}",
    URL_PATH_BODY,
    st.text(alphabet=string.ascii_letters + string.digits + "-_/", min_size=1, max_size=1),
)
HTTP_URLS = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}/{path}",
    st.sampled_from(["http", "https", "HTTP", "HTTPS"]),
    URL_HOST,
    URL_PATH,
)
TRIGGER_PAYLOAD = st.text(
    alphabet=string.ascii_letters + string.digits + "-_./:",
    min_size=1,
    max_size=80,
)
DECOMPOSE_PART = st.text(
    alphabet=string.ascii_letters + string.digits + "-_./:",
    min_size=2,
    max_size=80,
)


@settings(max_examples=75, deadline=None)
@given(urls=st.lists(HTTP_URLS, max_size=12))
def test_detect_urls_in_text_property_dedupes_preserves_order_and_caps(
    urls: list[str],
) -> None:
    command = " ".join(f"see {url}, then {url}." for url in urls)

    detected = inv._detect_urls_in_text(command)

    expected = list(dict.fromkeys(urls))[: inv._MAX_URL_TRIGGERS]
    assert detected == expected
    assert detected == inv._detect_urls_in_text(command)


@settings(max_examples=75, deadline=None)
@given(text=SAFE_TEXT)
def test_detect_urls_in_text_property_returns_only_http_urls(text: str) -> None:
    detected = inv._detect_urls_in_text(text)

    assert len(detected) <= inv._MAX_URL_TRIGGERS
    assert all(url.lower().startswith(("http://", "https://")) for url in detected)
    assert len(detected) == len(set(detected))


@settings(max_examples=75, deadline=None)
@given(url=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=512))
def test_truncate_url_for_display_property_is_bounded_and_idempotent(url: str) -> None:
    display = inv._truncate_url_for_display(url)

    assert len(display) <= inv._MAX_URL_DISPLAY_CHARS
    if len(url) <= inv._MAX_URL_DISPLAY_CHARS:
        assert display == url
    else:
        assert display != url
    assert inv._truncate_url_for_display(display) == display


@settings(max_examples=75, deadline=None)
@given(parts=st.lists(DECOMPOSE_PART, min_size=1, max_size=8))
def test_regex_decompose_property_splits_known_sequential_conjunctions(
    parts: list[str],
) -> None:
    text = " then ".join(parts)

    expected = [part.strip().rstrip(".").strip() for part in parts]
    assert inv._regex_decompose(text) == expected


@settings(max_examples=75, deadline=None)
@given(
    payloads=st.lists(
        st.one_of(st.none(), st.just(""), TRIGGER_PAYLOAD),
        max_size=30,
    ),
)
def test_trigger_extractors_property_preserve_payload_order(
    payloads: list[str | None],
) -> None:
    triggers: list[str] = []
    expected_toolchains: list[str] = []
    expected_urls: list[str] = []
    expected_images: list[str] = []
    for idx, payload in enumerate(payloads):
        value = payload or ""
        triggers.append(f"noise:{idx}")
        triggers.append(f"missing_toolchain:{value}")
        triggers.append(f"url_in_message:{value}")
        triggers.append(f"image_in_message:{value}")
        if value:
            expected_toolchains.append(value)
            expected_urls.append(value)
            expected_images.append(value)

    assert inv._missing_toolchain_slugs(triggers) == expected_toolchains
    assert inv._url_in_message_urls(triggers) == expected_urls
    assert inv._image_in_message_hashes(triggers) == expected_images


@settings(max_examples=75, deadline=None)
@given(errors=st.lists(TRIGGER_PAYLOAD, max_size=40))
def test_agent_error_history_property_is_bounded_and_clearable(
    errors: list[str],
) -> None:
    agent_id = "property-agent"
    inv.clear_agent_error_history(agent_id)
    try:
        for error_key in errors:
            inv.record_agent_error(agent_id, error_key)

        assert inv._snapshot_agent_errors(agent_id) == errors[-inv._AGENT_ERR_HIST_MAX:]
    finally:
        inv.clear_agent_error_history(agent_id)
    assert inv._snapshot_agent_errors(agent_id) == []


@settings(max_examples=75, deadline=None)
@given(priority=st.one_of(st.sampled_from(list(TaskPriority)), SAFE_TEXT))
def test_priority_rank_property_returns_known_order_or_fallback(priority) -> None:
    rank = inv._priority_rank(priority)

    assert isinstance(rank, int)
    assert 0 <= rank <= 4
    priority_value = priority.value if hasattr(priority, "value") else str(priority)
    if priority_value not in {"critical", "high", "medium", "low"}:
        assert rank == 4


@settings(max_examples=75, deadline=None)
@given(titles=st.lists(SAFE_TEXT, max_size=20))
def test_build_report_property_is_deterministic_and_includes_titles(
    titles: list[str],
) -> None:
    action = {"completed_count": len(titles), "tasks": titles}

    report = inv._build_report(action)

    assert report == inv._build_report(action)
    assert f"{len(titles)} task(s)" in report
    for title in titles:
        assert title in report
