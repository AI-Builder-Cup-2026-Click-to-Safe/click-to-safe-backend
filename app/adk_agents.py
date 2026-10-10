"""The review workflow as native ADK agents.

A LoopAgent wraps the analyst -> lawyer -> judge sequence. ADK does not stop a
loop on its own, so an after_agent_callback ends it once the judge reports that
it needs nothing further; max_iterations is the backstop that bounds cost and
latency when the judge keeps asking for more.

The lawyer runs behind an AgentTool rather than directly in the loop. Sub-agents
of a workflow agent share one conversation, so the video part stays in history
and Vertex rejects the pairing ("grounding is not supported non-text input").
AgentTool runs its agent in a fresh session seeded with the parent state, so the
lawyer sees the analyst's written findings as text and retrieval is allowed.

Agents exchange data through session state: each writes under its output_key and
the next reads it with a {key} placeholder in its instruction.
"""

import json
import os

from google.adk.agents import LlmAgent, LoopAgent
from google.adk.dependencies.vertexai import rag
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.retrieval.vertex_ai_rag_retrieval import VertexAiRagRetrieval

from app import costs
from app.agents import (
    JUDGE_INSTRUCTION,
    JUDGE_SCHEMA,
    LAWYER_INSTRUCTION,
    LAWYER_SCHEMA,
    MAX_FOLLOW_UP_ROUNDS,
    VIDEO_INSTRUCTION,
    VIDEO_SCHEMA,
)
from app.benchmark import record_adk_usage

STATE_VIDEO = "video_report"
STATE_LEGAL = "legal_report"
STATE_VERDICT = "verdict"


def _model() -> str:
    return os.getenv("MODEL", "gemini-3.8-flash")


def _vision_model() -> str:
    """Model for reading the footage.

    Watching the road is the hardest step and the one everything else rests on:
    misread the lane layout and the lawyer cites the wrong provision. Measured on
    the sample clip, asking each model how many lanes ran in the direction of
    travel (the road is single-lane), over three runs each:

        gemini-3.8-flash        3/3   <- chosen
        gemini-3.7-flash        3/3
        gemini-2.5-pro          3/3
        gemini-3.1-pro-preview  1/3
        gemini-3.1-flash-lite   0/3

    Newer did not mean better: the preview Pro was the least reliable of these.
    Kept separate from MODEL so the analyst can be moved without touching the
    text agents, even though both now default to the same model.
    """
    return os.getenv("VISION_MODEL", "gemini-3.8-flash")


def use_vertex_ai() -> None:
    """Point ADK at Vertex AI using this app's own settings.

    Agent Runtime reserves the GOOGLE_CLOUD_* names because it injects them
    itself, and it sets the location to its own region. The model may not be
    served there -- ours is global -- so MODEL_REGION overrides that value rather
    than defaulting behind it. Without this the runtime answers 404 for a model
    that works locally.
    """
    project = os.getenv("APP_PROJECT_ID")
    if project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
    region = os.getenv("MODEL_REGION")
    if region:
        os.environ["GOOGLE_CLOUD_LOCATION"] = region
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")


def stop_when_decided(callback_context) -> None:
    """End the loop once the judge says it needs nothing further.

    A tool cannot do this job here: calling one makes the model's reply a
    function_call, and ADK then has no text to store under output_key, so the
    verdict would be lost. Reading the verdict the judge already wrote keeps the
    decision and the stop signal in one place.
    """
    raw = callback_context.state.get(STATE_VERDICT)
    if raw is None:
        return
    try:
        verdict = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError):
        # An unreadable verdict is not a reason to keep spending rounds.
        _stop(callback_context)
        return
    if not verdict.get("needs_more"):
        _stop(callback_context)


def _stop(callback_context) -> None:
    # ADK only emits an after-agent event, the one LoopAgent checks for
    # escalate, when the callback also changes state.
    callback_context.state[STATE_STOPPED] = True
    callback_context.actions.escalate = True


def law_retrieval_tool() -> VertexAiRagRetrieval | None:
    """Retrieval over the statute corpus, or None when no corpus is configured."""
    corpus = os.getenv("LAW_CORPUS", "")
    if not corpus:
        return None
    return VertexAiRagRetrieval(
        name="search_traffic_law",
        description=(
            "Search the Malaysian Road Transport Act 1987 for provisions that match "
            "described driving conduct. Returns whole sections with their headings."
        ),
        rag_resources=[rag.RagResource(rag_corpus=corpus)],
        similarity_top_k=6,
        # RAG Engine returns a distance, so this bounds how far a provision may be
        # from the query before it is dropped.
        vector_distance_threshold=0.75,
    )


def build_video_agent() -> LlmAgent:
    return LlmAgent(
        name="video_analyst",
        model=_vision_model(),
        description="Describes dashcam footage per time window and per vehicle.",
        instruction=(
            VIDEO_INSTRUCTION + "\n\nAnalyse the footage supplied with this request. If a previous "
            "round left questions in state, re-examine those points and keep the "
            "same vehicle_id values.\n"
            "Questions raised on review (may be empty): {judge_video_questions?}"
        ),
        output_schema=VIDEO_SCHEMA,
        output_key=STATE_VIDEO,
        after_model_callback=[record_adk_usage, costs.adk_callback(_vision_model())],
    )


def build_lawyer_agent() -> LlmAgent:
    """The lawyer itself. Runs in its own session so retrieval sees only text."""
    tool = law_retrieval_tool()
    sourcing = (
        "Call search_traffic_law for each distinct manoeuvre before answering, and "
        "cite only what it returns."
        if tool
        else "No statute corpus is configured, so return no findings and say so in "
        "summary rather than citing provisions from memory."
    )
    return LlmAgent(
        name="traffic_lawyer",
        model=_model(),
        description="Grounds observed conduct in retrieved Malaysian statute text.",
        instruction=(
            LAWYER_INSTRUCTION + f"\n\n{sourcing}\n\n"
            "Observed facts from the footage:\n{" + STATE_VIDEO + "}\n\n"
            "Questions raised on review (may be empty): {judge_lawyer_questions?}"
        ),
        # A retrieval tool must be the only tool on its agent.
        tools=[tool] if tool else [],
        after_model_callback=[record_adk_usage, costs.adk_callback(_model())],
    )


LAWYER_CALLS = "lawyer_step_calls"
STATE_STOPPED = "review_stopped"


def reset_lawyer_calls(callback_context) -> None:
    """Each round may consult the lawyer afresh."""
    callback_context.state[LAWYER_CALLS] = 0


def call_lawyer_once(tool, args, tool_context) -> dict | None:
    """Refuse repeat calls within a round.

    The model sometimes ignores "exactly once" and re-asks the lawyer until the
    review deadline; answering the repeat locally ends that loop.
    """
    calls = tool_context.state.get(LAWYER_CALLS, 0) + 1
    tool_context.state[LAWYER_CALLS] = calls
    if calls > 1:
        return {
            "error": "traffic_lawyer has already answered this round. Record its "
            "earlier answer now; if it cited nothing, return empty findings."
        }
    return None


def hide_lawyer_once_answered(callback_context, llm_request) -> None:
    """Leave the model nothing to call but its answer once the lawyer replied."""
    if callback_context.state.get(LAWYER_CALLS, 0) >= 1:
        llm_request.config.tools = None
        llm_request.tools_dict.clear()


def build_lawyer_step() -> LlmAgent:
    """Loop step that delegates to the lawyer through an AgentTool.

    Calling the lawyer as a tool gives it a fresh session, which keeps the video
    part out of its request; a retrieval tool cannot be used alongside non-text
    input. State written by the lawyer is forwarded back to this session.
    """
    return LlmAgent(
        name="lawyer_step",
        model=_model(),
        description="Records the traffic lawyer's citations as structured findings.",
        instruction=(
            "Call traffic_lawyer exactly once to have the observed driving conduct "
            "matched against Malaysian statute provisions. Then record what it "
            "returned in the required JSON shape, changing nothing: keep each "
            "section number, quote and penalty exactly as the lawyer gave them, and "
            "carry its gaps across. Never analyse the footage or name a provision "
            "the lawyer did not cite. If it cited nothing, return empty findings."
        ),
        tools=[AgentTool(agent=build_lawyer_agent())],
        before_agent_callback=reset_lawyer_calls,
        before_model_callback=hide_lawyer_once_answered,
        before_tool_callback=call_lawyer_once,
        output_schema=LAWYER_SCHEMA,
        output_key=STATE_LEGAL,
        after_model_callback=[record_adk_usage, costs.adk_callback(_model())],
    )


def build_judge_agent() -> LlmAgent:
    return LlmAgent(
        name="judge",
        model=_model(),
        description="Decides whether any vehicle should be reported.",
        instruction=(
            JUDGE_INSTRUCTION + "\n\nVideo observations:\n{" + STATE_VIDEO + "}\n\n"
            "Lawyer's analysis:\n{" + STATE_LEGAL + "}\n\n"
            "Set needs_more false once you can decide; the review stops there. Set it "
            "true only when a specific missing fact would change the outcome, and list "
            "the questions that would settle it."
        ),
        output_schema=JUDGE_SCHEMA,
        output_key=STATE_VERDICT,
        after_agent_callback=stop_when_decided,
        after_model_callback=[record_adk_usage, costs.adk_callback(_model())],
    )


def build_review_agent() -> LoopAgent:
    """The master agent: analyst -> lawyer -> judge, repeated until the judge exits."""
    use_vertex_ai()
    # ADK marks LoopAgent deprecated in favour of Workflow, but that type is not
    # exported by this release and its own notice says it cannot yet be an
    # LlmAgent sub-agent, so LoopAgent remains the supported choice here.
    return LoopAgent(
        name="review_master",
        description=(
            "Reviews dashcam footage for reportable traffic violations under Malaysian law."
        ),
        sub_agents=[build_video_agent(), build_lawyer_step(), build_judge_agent()],
        # One initial pass plus the allowed follow-up rounds.
        max_iterations=MAX_FOLLOW_UP_ROUNDS + 1,
    )


def build_reasoning_agent() -> LoopAgent:
    """Lawyer and judge only, for a clip whose analysis is already in state.

    A long clip is analysed window by window before the loop starts, so the
    timeline is seeded under STATE_VIDEO. Running the analyst again here would
    put it back in front of the whole video — the case the windows exist to
    avoid — and its answer would overwrite the merged one.
    """
    use_vertex_ai()
    return LoopAgent(
        name="review_master",
        description=(
            "Reviews an existing dashcam analysis for reportable violations under Malaysian law."
        ),
        sub_agents=[build_lawyer_step(), build_judge_agent()],
        max_iterations=MAX_FOLLOW_UP_ROUNDS + 1,
    )


# ADK tooling discovers a module-level `root_agent`.
root_agent = build_review_agent
