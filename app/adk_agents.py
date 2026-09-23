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

from app.agents import (
    JUDGE_INSTRUCTION,
    JUDGE_SCHEMA,
    LAWYER_INSTRUCTION,
    LAWYER_SCHEMA,
    MAX_FOLLOW_UP_ROUNDS,
    VIDEO_INSTRUCTION,
    VIDEO_SCHEMA,
)

STATE_VIDEO = "video_report"
STATE_LEGAL = "legal_report"
STATE_VERDICT = "verdict"


def _model() -> str:
    return os.getenv("MODEL", "gemini-3.1-flash-lite")


def use_vertex_ai() -> None:
    """Point ADK at Vertex AI using this app's own settings.

    Agent Runtime reserves the GOOGLE_CLOUD_* names, so the deployment cannot set
    them; ADK reads them from the environment, so they are derived here instead.
    """
    project = os.getenv("APP_PROJECT_ID")
    if project:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", os.getenv("MODEL_REGION", "global"))
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
        callback_context.actions.escalate = True
        return
    if not verdict.get("needs_more"):
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
        model=_model(),
        description="Describes dashcam footage per time window and per vehicle.",
        instruction=(
            VIDEO_INSTRUCTION + "\n\nAnalyse the footage supplied with this request. If a previous "
            "round left questions in state, re-examine those points and keep the "
            "same vehicle_id values.\n"
            "Questions raised on review (may be empty): {judge_video_questions?}"
        ),
        output_schema=VIDEO_SCHEMA,
        output_key=STATE_VIDEO,
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
    )


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
        output_schema=LAWYER_SCHEMA,
        output_key=STATE_LEGAL,
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


# ADK tooling discovers a module-level `root_agent`.
root_agent = build_review_agent
