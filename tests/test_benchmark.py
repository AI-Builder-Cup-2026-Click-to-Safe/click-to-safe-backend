import json

from app import benchmark


def test_benchmark_events_are_opt_in_and_correlated(capsys):
    benchmark.emit("ignored")
    assert capsys.readouterr().out == ""

    token = benchmark.begin_request("run-123")
    try:
        benchmark.emit("request", status_code=200)
        event = json.loads(capsys.readouterr().out)
        assert event == {
            "severity": "INFO",
            "benchmark_id": "run-123",
            "benchmark_event": "request",
            "status_code": 200,
        }
    finally:
        benchmark.end_request(token)


def test_model_usage_records_token_counts_without_model_content(capsys):
    class Usage:
        prompt_token_count = 1200
        cached_content_token_count = 200
        candidates_token_count = 300
        thoughts_token_count = 40
        tool_use_prompt_token_count = 12

    token = benchmark.begin_request("run-456")
    try:
        benchmark.record_usage("adk:judge", "gemini-3.8-flash", Usage())
        event = json.loads(capsys.readouterr().out)
        assert event["benchmark_event"] == "model_usage"
        assert event["prompt_tokens"] == 1200
        assert event["cached_prompt_tokens"] == 200
        assert event["output_tokens"] == 300
        assert event["reasoning_tokens"] == 40
        assert not any(key in event for key in ("content", "text", "generated_text"))
    finally:
        benchmark.end_request(token)
