"""WP.1.5 -- Block share redaction-mask contracts."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from backend.block_redaction import (
    REDACTION_CUSTOMER_IP,
    REDACTION_KS_ENVELOPE,
    REDACTION_PII,
    REDACTION_SECRET,
    normalise_share_regions,
    redact_block_for_share,
)
from backend.models import Block


@dataclass(frozen=True)
class FakeRecognizerResult:
    entity_type: str
    start: int
    end: int
    score: float


class TokenAnalyzer:
    def analyze(
        self,
        *,
        text: str,
        language: str,
        entities: list[str],
        score_threshold: float,
    ) -> list[FakeRecognizerResult]:
        del language, entities, score_threshold
        results: list[FakeRecognizerResult] = []
        for token, entity_type in (
            ("alice@example.com", "EMAIL_ADDRESS"),
            ("555-0100", "PHONE_NUMBER"),
        ):
            start = text.find(token)
            if start >= 0:
                results.append(
                    FakeRecognizerResult(
                        entity_type=entity_type,
                        start=start,
                        end=start + len(token),
                        score=0.91,
                    )
                )
        return results


class FakeAnonymizer:
    def anonymize(
        self,
        *,
        text: str,
        analyzer_results: list[FakeRecognizerResult],
    ) -> SimpleNamespace:
        out = text
        for item in sorted(analyzer_results, key=lambda result: result.start, reverse=True):
            out = out[: item.start] + f"<{item.entity_type}>" + out[item.end :]
        return SimpleNamespace(text=out)


def _block(**kwargs: object) -> Block:
    return Block(
        block_id="b-1",
        tenant_id="t-1",
        kind="turn.command",
        status="completed",
        **kwargs,
    )


def test_normalise_share_regions_dedupes_and_filters_unknown_values() -> None:
    assert normalise_share_regions(["output", "unknown", "output", "metadata"]) == (
        "output",
        "metadata",
    )
    assert normalise_share_regions(None) == (
        "command",
        "output",
        "metadata",
        "screenshots",
    )


def test_explicit_redaction_mask_is_authoritative_for_selected_regions() -> None:
    block = _block(
        payload={
            "command": "curl https://example.test",
            "stdout": "public output",
        },
        redaction_mask={"payload.command": REDACTION_SECRET},
    )

    result = redact_block_for_share(
        block,
        regions=["command"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.changed is True
    assert result.redaction_mask == {"payload.command": REDACTION_SECRET}
    assert result.block["payload"]["command"] == "[REDACTED:secret]"
    assert result.block["payload"]["stdout"] == "public output"
    assert block.payload["command"] == "curl https://example.test"


def test_explicit_none_mask_preserves_wp_1_1_no_redaction_shape() -> None:
    block = _block(
        payload={"stdout": "public output"},
        redaction_mask={"payload.stdout": "none"},
    )

    result = redact_block_for_share(
        block,
        regions=["output"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.changed is False
    assert result.redaction_mask == {}
    assert result.block["payload"]["stdout"] == "public output"


def test_auto_mask_marks_secret_pii_and_customer_ip_paths() -> None:
    block = _block(
        payload={
            "stdout": (
                "token sk-ant-abcdefghijklmnopqrstuv "
                "ip 203.0.113.9 email alice@example.com"
            ),
            "stderr": "call 555-0100",
        },
    )

    result = redact_block_for_share(
        block,
        regions=["output"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.redaction_mask == {
        "payload.stdout": [
            REDACTION_SECRET,
            REDACTION_CUSTOMER_IP,
            REDACTION_PII,
        ],
        "payload.stderr": REDACTION_PII,
    }
    assert "[REDACTED:anthropic_key]" in result.block["payload"]["stdout"]
    assert "[REDACTED:customer_ip]" in result.block["payload"]["stdout"]
    assert "<EMAIL_ADDRESS>" in result.block["payload"]["stdout"]
    assert result.block["payload"]["stderr"] == "call <PHONE_NUMBER>"


def test_metadata_region_masks_nested_pii_without_mutating_source() -> None:
    block = _block(
        metadata={
            "reviewer": {"email": "alice@example.com"},
            "customer_ip": "198.51.100.44",
        },
    )

    result = redact_block_for_share(
        block,
        regions=["metadata"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.redaction_mask == {
        "metadata.reviewer.email": REDACTION_PII,
        "metadata.customer_ip": REDACTION_CUSTOMER_IP,
    }
    assert result.block["metadata"]["reviewer"]["email"] == "<EMAIL_ADDRESS>"
    assert result.block["metadata"]["customer_ip"] == "[REDACTED:customer_ip]"
    assert block.metadata["reviewer"]["email"] == "alice@example.com"


def test_ks_envelope_boundary_is_marked_without_decrypting() -> None:
    envelope = {
        "fmt": 1,
        "alg": "AES-256-GCM",
        "dek": "dek-1",
        "tid": "tenant-1",
        "nonce_b64": "bm9uY2U=",
        "ciphertext_b64": "Y2lwaGVydGV4dA==",
    }
    block = _block(payload={"stdout": [{"secret": envelope}]})

    result = redact_block_for_share(
        block,
        regions=["output"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.redaction_mask == {
        "payload.stdout.0.secret": REDACTION_KS_ENVELOPE,
    }
    assert result.block["payload"]["stdout"][0]["secret"] == {
        "redacted": True,
        "reason": REDACTION_KS_ENVELOPE,
    }
    assert block.payload["stdout"][0]["secret"] == envelope


def test_unselected_regions_are_not_scanned_or_marked() -> None:
    block = _block(
        payload={
            "command": "run with sk-ant-abcdefghijklmnopqrstuv",
            "stdout": "ok",
        },
    )

    result = redact_block_for_share(
        block,
        regions=["output"],
        pii_analyzer=TokenAnalyzer(),
        pii_anonymizer=FakeAnonymizer(),
    )

    assert result.changed is False
    assert result.redaction_mask == {}
    assert result.block["payload"]["command"] == "run with sk-ant-abcdefghijklmnopqrstuv"
