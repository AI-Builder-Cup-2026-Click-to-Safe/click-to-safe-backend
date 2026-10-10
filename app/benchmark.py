"""Opt-in structured measurements for a single explicitly tagged request."""

from __future__ import annotations

import contextvars
import json
import time

_BENCHMARK_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "benchmark_id", default=None
)


def begin_request(benchmark_id: str | None):
    """Set the request's correlation ID; ordinary requests emit no benchmark logs."""
    return _BENCHMARK_ID.set(benchmark_id)


def end_request(token) -> None:
    _BENCHMARK_ID.reset(token)


def benchmark_id() -> str | None:
    return _BENCHMARK_ID.get()


def emit(event: str, **fields) -> None:
    """Emit JSON for Cloud Run's structured stdout log ingestion."""
    run_id = benchmark_id()
    if not run_id:
        return
    print(
        json.dumps(
            {"severity": "INFO", "benchmark_id": run_id, "benchmark_event": event, **fields},
            separators=(",", ":"),
            ensure_ascii=True,
        ),
        flush=True,
    )


def stage(name: str, started: float) -> None:
    emit("stage", stage=name, duration_ms=round((time.monotonic() - started) * 1000))


def _count(usage, name: str) -> int | None:
    value = getattr(usage, name, None)
    return int(value) if value is not None else None


def record_usage(source: str, model: str, usage) -> None:
    """Record token counts only; never log prompts or generated content."""
    if not benchmark_id():
        return
    if usage is None:
        emit("model_usage_missing", source=source, model=model)
        return
    emit(
        "model_usage",
        source=source,
        model=model,
        prompt_tokens=_count(usage, "prompt_token_count"),
        cached_prompt_tokens=_count(usage, "cached_content_token_count"),
        output_tokens=_count(usage, "candidates_token_count"),
        reasoning_tokens=_count(usage, "thoughts_token_count"),
        tool_use_prompt_tokens=_count(usage, "tool_use_prompt_token_count"),
    )


def record_adk_usage(callback_context, llm_response) -> None:
    agent = getattr(callback_context, "agent", None)
    model = str(getattr(agent, "model", "unknown"))
    record_usage(
        f"adk:{getattr(agent, 'name', 'unknown')}",
        model,
        getattr(llm_response, "usage_metadata", None),
    )
