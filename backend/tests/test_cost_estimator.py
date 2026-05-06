"""MP.W2.5 -- cost estimator prediction and tenant calibration tests."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from backend import pricing
from backend.agents import cost_estimator as ce


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    pricing.reset_cache_for_tests()
    yield
    pricing.reset_cache_for_tests()


@dataclass(frozen=True)
class TaskDataclass:
    prompt: str
    agent_class: str = "api-anthropic"
    tier: str = "M"
    area: str = "backend"


class ProviderObject:
    def provider_id(self) -> str:
        return "anthropic-subscription"


class PromptObject:
    prompt = "Implement the MP estimator endpoint."
    agent_class = "api-anthropic"
    tier = "M"
    area = "backend"
    correlation_id = "OP-29"


def test_predict_token_count_empty_is_zero():
    assert ce.predict_token_count("") == 0
    assert ce.predict_token_count(None) == 0


def test_predict_token_count_uses_char_ceiling_plus_envelope():
    assert ce.predict_token_count("abcd") == 18
    assert ce.predict_token_count("a" * 401) == 117


def test_predict_token_count_uses_word_floor_when_larger():
    text = "word " * 100
    assert ce.predict_token_count(text) == 151


def test_predict_token_count_decodes_bytes():
    assert ce.predict_token_count(b"abcd") == 18


def test_dict_task_serialization_is_key_order_stable():
    left = {"prompt": "build API", "tier": "M", "agent_class": "api-openai"}
    right = {"agent_class": "api-openai", "tier": "M", "prompt": "build API"}
    assert ce.predict_token_count(left) == ce.predict_token_count(right)


def test_dataclass_task_serialization_is_stable():
    task = TaskDataclass(prompt="Summarize MP.W2 drift reports.")
    assert ce.predict_token_count(task) == ce.predict_token_count(task.__dict__)


def test_prompt_object_includes_routing_envelope():
    with_envelope = ce.predict_token_count(PromptObject())
    prompt_only = ce.predict_token_count(PromptObject.prompt)
    assert with_envelope > prompt_only


def test_explicit_output_tokens_override_default():
    assert ce._predict_output_tokens({"prompt": "tiny", "max_tokens": "512"}) == 512


def test_output_tokens_default_has_minimum_floor():
    assert ce._predict_output_tokens("tiny") == ce.MIN_OUTPUT_TOKENS


def test_output_tokens_default_has_maximum_ceiling():
    assert ce._predict_output_tokens("a" * 200_000) == ce.MAX_OUTPUT_TOKENS


def test_predict_wall_time_anthropic_formula():
    task = {"prompt": "a" * 400, "max_output_tokens": 400}
    total_tokens = ce.predict_token_count(task) + ce._predict_output_tokens(task)
    expected = 8.0 + (total_tokens / 1_000.0) * 2.4
    assert ce.predict_wall_time(task, "anthropic") == pytest.approx(expected, abs=0.001)


def test_predict_wall_time_openai_subscription_is_faster_than_anthropic():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    assert ce.predict_wall_time(task, "openai-subscription") < ce.predict_wall_time(
        task, "anthropic-subscription"
    )


def test_predict_wall_time_unknown_provider_uses_default_formula():
    task = {"prompt": "a" * 400, "max_output_tokens": 400}
    total_tokens = ce.predict_token_count(task) + ce._predict_output_tokens(task)
    expected = 8.0 + (total_tokens / 1_000.0) * 2.5
    assert ce.predict_wall_time(task, "vendor-x") == pytest.approx(expected, abs=0.001)


def test_predict_cost_anthropic_uses_pricing_table():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    input_tokens = ce.predict_token_count(task)
    expected = input_tokens * 3.0 / 1_000_000 + 1_000 * 15.0 / 1_000_000
    assert ce.predict_cost(task, "anthropic") == pytest.approx(expected, abs=0.000001)


def test_predict_cost_openai_uses_openai_default_model():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    input_tokens = ce.predict_token_count(task)
    expected = input_tokens * 5.0 / 1_000_000 + 1_000 * 15.0 / 1_000_000
    assert ce.predict_cost(task, "openai") == pytest.approx(expected, abs=0.000001)


def test_subscription_provider_uses_base_provider_pricing():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    assert ce.predict_cost(task, "anthropic-subscription") == ce.predict_cost(
        task, "anthropic"
    )


def test_task_model_overrides_provider_default_model():
    task = {
        "prompt": "a" * 4_000,
        "max_output_tokens": 1_000,
        "model": "claude-opus-4-7",
    }
    input_tokens = ce.predict_token_count(task)
    expected = input_tokens * 5.0 / 1_000_000 + 1_000 * 25.0 / 1_000_000
    assert ce.predict_cost(task, "anthropic") == pytest.approx(expected, abs=0.000001)


def test_provider_object_id_is_supported():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    assert ce.predict(ProviderObject(), "anthropic").provider_id == "anthropic"
    assert ce.predict(task, ProviderObject()).provider_id == "anthropic-subscription"


def test_tenant_calibration_multiplies_token_prediction():
    calibration = ce.TenantCalibration(tenant_id="t-acme", token_multiplier=1.5)
    base = ce.predict_token_count("a" * 400)
    assert ce.predict_token_count("a" * 400, calibration) == pytest.approx(
        base * 1.5, abs=1
    )


def test_tenant_calibration_multiplies_wall_time_and_cost():
    task = {"prompt": "a" * 4_000, "max_output_tokens": 1_000}
    calibration = ce.TenantCalibration(
        tenant_id="t-acme",
        wall_time_multiplier=1.25,
        cost_multiplier=1.4,
    )
    assert ce.predict_wall_time(task, "anthropic", calibration) == pytest.approx(
        ce.predict_wall_time(task, "anthropic") * 1.25,
        abs=0.002,
    )
    assert ce.predict_cost(task, "anthropic", calibration) == pytest.approx(
        ce.predict_cost(task, "anthropic") * 1.4,
        abs=0.000002,
    )


def test_update_tenant_calibration_records_actual_drift_without_warning(caplog):
    prediction = ce.CostPrediction(
        tenant_id="t-acme",
        provider_id="anthropic",
        input_tokens=1_000,
        output_tokens=500,
        wall_time_seconds=10.0,
        cost_usd=0.10,
    )
    actual = ce.CostActual(
        input_tokens=1_100,
        output_tokens=550,
        wall_time_seconds=11.0,
        cost_usd=0.11,
    )
    caplog.set_level(logging.WARNING, logger="backend.agents.cost_estimator")
    updated = ce.update_tenant_calibration(prediction, actual)
    assert updated.sample_count == 1
    assert updated.token_multiplier == pytest.approx(1.1)
    assert updated.wall_time_multiplier == pytest.approx(1.1)
    assert updated.cost_multiplier == pytest.approx(1.1)
    assert "cost estimator drift exceeded threshold" not in caplog.text


def test_update_tenant_calibration_warns_when_drift_exceeds_50_percent(caplog):
    prediction = ce.CostPrediction(
        tenant_id="t-acme",
        provider_id="anthropic",
        input_tokens=1_000,
        output_tokens=500,
        wall_time_seconds=10.0,
        cost_usd=0.10,
    )
    actual = ce.CostActual(
        input_tokens=2_000,
        output_tokens=1_000,
        wall_time_seconds=22.0,
        cost_usd=0.25,
    )
    caplog.set_level(logging.WARNING, logger="backend.agents.cost_estimator")
    updated = ce.update_tenant_calibration(prediction, actual)
    assert updated.last_cost_drift == pytest.approx(1.5)
    assert "tenant=t-acme" in caplog.text
    assert "drift_pct=1.500" in caplog.text


def test_update_tenant_calibration_blends_existing_samples():
    current = ce.TenantCalibration(
        tenant_id="t-acme",
        token_multiplier=2.0,
        wall_time_multiplier=2.0,
        cost_multiplier=2.0,
        sample_count=3,
    )
    prediction = ce.CostPrediction(
        tenant_id="t-acme",
        provider_id="anthropic",
        input_tokens=1_000,
        output_tokens=0,
        wall_time_seconds=10.0,
        cost_usd=0.10,
    )
    actual = ce.CostActual(
        input_tokens=1_500,
        output_tokens=0,
        wall_time_seconds=15.0,
        cost_usd=0.15,
    )
    updated = ce.update_tenant_calibration(prediction, actual, current)
    assert updated.sample_count == 4
    assert updated.token_multiplier == pytest.approx(2.3)
    assert updated.wall_time_multiplier == pytest.approx(2.3)
    assert updated.cost_multiplier == pytest.approx(2.3)
