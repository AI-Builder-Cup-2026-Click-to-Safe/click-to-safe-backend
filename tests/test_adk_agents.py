import json

import pytest

from app import adk_agents
from app.agents import MAX_FOLLOW_UP_ROUNDS


class FakeActions:
    def __init__(self):
        self.escalate = False


class FakeContext:
    def __init__(self, state):
        self.state = state
        self.actions = FakeActions()


@pytest.fixture
def corpus(monkeypatch):
    monkeypatch.setenv("LAW_CORPUS", "projects/p/locations/europe-west4/ragCorpora/123")


def test_loop_is_bounded_to_the_allowed_follow_up_rounds(corpus):
    agent = adk_agents.build_review_agent()
    assert agent.max_iterations == MAX_FOLLOW_UP_ROUNDS + 1


def test_pipeline_runs_analyst_then_lawyer_then_judge(corpus):
    names = [s.name for s in adk_agents.build_review_agent().sub_agents]
    assert names == ["video_analyst", "lawyer_step", "judge"]


def test_lawyer_is_called_as_a_tool_so_retrieval_never_sees_the_video(corpus):
    """Vertex rejects grounding alongside non-text input, so the lawyer needs
    its own session rather than sharing the loop's video history."""
    step = adk_agents.build_review_agent().sub_agents[1]
    assert [t.name for t in step.tools] == ["traffic_lawyer"]
    inner = step.tools[0].agent
    assert [t.name for t in inner.tools] == ["search_traffic_law"]


def test_without_a_corpus_the_lawyer_has_no_retrieval_tool(monkeypatch):
    monkeypatch.delenv("LAW_CORPUS", raising=False)
    inner = adk_agents.build_review_agent().sub_agents[1].tools[0].agent
    assert inner.tools == []


def test_judge_stops_the_loop_once_it_has_decided():
    ctx = FakeContext({adk_agents.STATE_VERDICT: json.dumps({"needs_more": False})})
    adk_agents.stop_when_decided(ctx)
    assert ctx.actions.escalate is True


def test_judge_keeps_the_loop_open_when_it_needs_more():
    ctx = FakeContext({adk_agents.STATE_VERDICT: json.dumps({"needs_more": True})})
    adk_agents.stop_when_decided(ctx)
    assert ctx.actions.escalate is False


def test_an_unreadable_verdict_does_not_burn_further_rounds():
    ctx = FakeContext({adk_agents.STATE_VERDICT: "not json"})
    adk_agents.stop_when_decided(ctx)
    assert ctx.actions.escalate is True


def test_a_missing_verdict_leaves_the_decision_to_the_next_round():
    ctx = FakeContext({})
    adk_agents.stop_when_decided(ctx)
    assert ctx.actions.escalate is False


def test_judge_carries_a_schema_and_no_tools(corpus):
    """A tool call would make the reply a function_call, leaving no text to
    store under output_key and losing the verdict."""
    judge = adk_agents.build_review_agent().sub_agents[2]
    assert judge.tools == []
    assert judge.output_schema is not None
    assert judge.after_agent_callback


def test_vertex_settings_come_from_the_apps_own_variables(monkeypatch):
    """Agent Runtime reserves the GOOGLE_CLOUD_* names, so they cannot be set by
    the deployment and are derived from APP_PROJECT_ID and MODEL_REGION."""
    for key in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_PROJECT_ID", "proj-x")
    monkeypatch.setenv("MODEL_REGION", "global")

    adk_agents.use_vertex_ai()

    import os

    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "proj-x"
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"
    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "TRUE"


def test_model_region_overrides_the_location_the_runtime_injects(monkeypatch):
    """Agent Runtime injects its own region, where the model may not be served;
    deploying with MODEL_REGION=global must win or the runtime answers 404."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.setenv("MODEL_REGION", "global")
    adk_agents.use_vertex_ai()

    import os

    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_retrieval_attaches_as_gemini_grounding_not_a_function_call(corpus):
    """On Gemini, ADK attaches this tool as built-in retrieval rather than a
    callable function, so a review emits no function_call for it; evidence that
    it ran is grounding_metadata on the response, not a tool-call event."""
    from google.adk.models.llm_request import LlmRequest

    tool = adk_agents.law_retrieval_tool()
    request = LlmRequest(model="gemini-3.1-flash-lite")

    import asyncio

    asyncio.run(tool.process_llm_request(tool_context=None, llm_request=request))

    attached = request.config.tools
    assert len(attached) == 1
    assert attached[0].retrieval is not None
    assert attached[0].function_declarations is None
