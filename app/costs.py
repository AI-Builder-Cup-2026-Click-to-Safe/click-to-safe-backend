"""Per-review Gemini token estimate, not a Cloud Billing invoice.

Standard Vertex Gemini 3.8 Flash prices (USD / million tokens), checked 2026-10-11:
https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
Includes the advertised introductory credit through 2026-12-31. Excludes storage,
compute, retrieval, explicit cache storage and account-specific discounts.
"""

import contextvars
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class ReviewCost:
    usd: float = 0.0
    calls: int = 0
    complete: bool = True


_current: contextvars.ContextVar[ReviewCost | None] = contextvars.ContextVar(
    "review_cost", default=None
)


def begin_review():
    # Child tasks share this accumulator; concurrent requests get separate ones.
    return _current.set(ReviewCost())


def end_review(token):
    _current.reset(token)


def record_usage(model, usage):
    cost = _current.get()
    if cost is None:
        logger.warning("Gemini usage arrived outside a review cost context")
        return
    cost.calls += 1
    if model != "gemini-3.8-flash" or usage is None:
        logger.warning("AI cost unavailable: model=%s, has_usage=%s", model, usage is not None)
        cost.complete = False
        return
    prompt = getattr(usage, "prompt_token_count", None)
    output = getattr(usage, "candidates_token_count", None)
    cached = getattr(usage, "cached_content_token_count", None) or 0
    thoughts = getattr(usage, "thoughts_token_count", None) or 0
    tool = getattr(usage, "tool_use_prompt_token_count", None) or 0
    counts = (prompt, output, cached, thoughts, tool)
    if any(type(n) is not int or n < 0 for n in counts) or cached > prompt:
        logger.warning("AI cost unavailable: invalid token counts %s", counts)
        cost.complete = False
        return
    multiplier = 1 if datetime.now(timezone.utc).year < 2027 else 2
    if os.getenv("MODEL_REGION", "global") != "global":
        multiplier *= 1.1
    # Cached tokens are already in prompt, while reasoning and tool-use tokens
    # are separate usage fields. Do not bill cached input twice.
    cost.usd += (
        ((prompt - cached + tool) * 0.75 + cached * 0.075 + (output + thoughts) * 3.75)
        * multiplier
        / 1_000_000
    )


def adk_callback(model):
    # Capture the actual configured model; ADK's public context has agent_name,
    # but no agent/model property. Called for each tool turn and nested agent.
    def collect(callback_context, llm_response):
        if not llm_response.partial:
            record_usage(model, llm_response.usage_metadata)

    return collect


def estimate():
    cost = _current.get()
    if cost is None or not cost.calls or not cost.complete:
        logger.warning("Incomplete review cost: %s", cost)
        return None
    return round(cost.usd, 8)
