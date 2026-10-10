import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from app import costs
from app.adk_agents import (
    build_judge_agent,
    build_lawyer_agent,
    build_lawyer_step,
    build_video_agent,
)

MODEL = "gemini-3.8-flash"


@pytest.fixture(autouse=True)
def price_date(monkeypatch):
    monkeypatch.setenv("MODEL_REGION", "global")
    monkeypatch.setattr(
        costs,
        "datetime",
        SimpleNamespace(now=lambda tz: datetime(2026, 10, 11, tzinfo=timezone.utc)),
    )


def usage(prompt=1200):
    return types.GenerateContentResponseUsageMetadata(
        prompt_token_count=prompt,
        cached_content_token_count=200,
        candidates_token_count=300,
        thoughts_token_count=40,
        tool_use_prompt_token_count=12,
    )


def test_counts_cached_input_once_and_includes_reasoning_and_tool_tokens():
    token = costs.begin_review()
    try:
        costs.record_usage(MODEL, usage())
        assert costs.estimate() == pytest.approx(0.002049)
        costs.record_usage(MODEL, usage())
        assert costs.estimate() == pytest.approx(0.004098)
    finally:
        costs.end_review(token)
    assert costs.estimate() is None


@pytest.mark.parametrize(
    "model,metadata",
    [
        ("unknown", usage()),
        (MODEL, None),
        (MODEL, types.GenerateContentResponseUsageMetadata(prompt_token_count=10)),
        (MODEL, usage(prompt=100)),
    ],
)
def test_incomplete_estimate_is_null_instead_of_a_partial_total(model, metadata):
    token = costs.begin_review()
    try:
        costs.record_usage(MODEL, usage())
        costs.record_usage(model, metadata)
        assert costs.estimate() is None
    finally:
        costs.end_review(token)


def test_concurrent_reviews_and_child_tasks_are_isolated():
    async def run(count):
        token = costs.begin_review()
        try:

            async def call():
                await asyncio.sleep(0)
                costs.record_usage(MODEL, usage())

            await asyncio.gather(*(call() for _ in range(count)))
            return costs.estimate()
        finally:
            costs.end_review(token)

    async def both():
        return await asyncio.gather(run(2), run(3))

    assert asyncio.run(both()) == pytest.approx([0.004098, 0.006147])


def test_rates_follow_region_and_introductory_end_date(monkeypatch):
    monkeypatch.setenv("MODEL_REGION", "us-central1")
    monkeypatch.setattr(
        costs, "datetime", SimpleNamespace(now=lambda tz: datetime(2027, 1, 1, tzinfo=timezone.utc))
    )
    token = costs.begin_review()
    try:
        costs.record_usage(MODEL, usage())
        assert costs.estimate() == pytest.approx(0.002049 * 2 * 1.1)
    finally:
        costs.end_review(token)


def test_every_adk_agent_including_nested_lawyer_reports_non_partial_usage(monkeypatch):
    monkeypatch.setenv("MODEL", MODEL)
    monkeypatch.setenv("VISION_MODEL", MODEL)
    monkeypatch.delenv("LAW_CORPUS", raising=False)
    token = costs.begin_review()
    try:
        for builder in [
            build_video_agent,
            build_lawyer_step,
            build_lawyer_agent,
            build_judge_agent,
        ]:
            callback = builder().after_model_callback
            callbacks = callback if isinstance(callback, list) else [callback]
            for cb in callbacks:
                cb(
                    callback_context=SimpleNamespace(),
                    llm_response=LlmResponse(partial=True, usage_metadata=usage()),
                )
                cb(
                    callback_context=SimpleNamespace(),
                    llm_response=LlmResponse(partial=False, usage_metadata=usage()),
                )
        assert costs.estimate() == pytest.approx(0.008196)
    finally:
        costs.end_review(token)
