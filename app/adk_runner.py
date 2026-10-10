"""Run the ADK review workflow and shape its session state into a response.

ADK returns a stream of events and leaves the agents' output in session state,
so a request is served by draining the stream and then reading what each agent
wrote. The judge's round count comes from the events, since state only holds the
last verdict.
"""

import json
import logging
from typing import Any

from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.adk_agents import (
    STATE_LEGAL,
    STATE_VERDICT,
    STATE_VIDEO,
    build_reasoning_agent,
    build_review_agent,
)

logger = logging.getLogger(__name__)

APP_NAME = "traffic_review"
REVIEW_REQUEST = (
    "Review this dashcam footage for traffic violations by other vehicles that "
    "would warrant a report."
)


MAX_LLM_CALLS = 80


def _loads(value: Any) -> Any:
    """Agent output is JSON text under output_key, or already a dict."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # The lawyer has no output_schema, so its answer may be prose; keep it as text.
        return value


async def run_review(
    video_uri: str,
    mime_type: str,
    focus: str = "",
    prepared: dict | None = None,
) -> dict:
    """Run the review and return the judge's decision.

    `prepared` is a merged analysis of a long clip, already built window by
    window. When it is given the analyst is skipped and only the lawyer and
    judge run, over that analysis; otherwise the whole workflow reads the clip.
    """
    chunked = prepared is not None
    agent = build_reasoning_agent() if chunked else build_review_agent()
    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)

    state = {"judge_video_questions": focus, "judge_lawyer_questions": ""}
    if chunked:
        # Seed what the analyst would otherwise have written this pass.
        state[STATE_VIDEO] = json.dumps(prepared, ensure_ascii=False)

    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="runtime", state=state
    )

    request = REVIEW_REQUEST
    if focus:
        # Caller text is quoted as a hint, never as a new instruction.
        request += f"\n\nThe caller asked you to pay attention to: {focus}"

    parts = [types.Part.from_text(text=request)]
    if not chunked:
        # A prepared review must not carry the video: the lawyer's retrieval is
        # refused in a session whose history holds non-text input.
        parts.insert(0, types.Part.from_uri(file_uri=video_uri, mime_type=mime_type))

    message = types.Content(role="user", parts=parts)

    rounds = 0
    async for event in runner.run_async(
        user_id="runtime",
        session_id=session.id,
        new_message=message,
        # A full review takes about twenty calls; a runaway tool loop fails fast
        # instead of holding the request until the deadline.
        run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS),
    ):
        if event.author == "judge" and event.content and event.content.parts:
            rounds += 1

    final = await runner.session_service.get_session(
        app_name=APP_NAME, user_id="runtime", session_id=session.id
    )
    state = final.state if final else {}

    verdict = _loads(state.get(STATE_VERDICT))
    if not isinstance(verdict, dict):
        raise ValueError("the judge returned no usable verdict")

    # A chunked review seeds the analysis rather than deriving it in the loop.
    video = prepared if chunked else (_loads(state.get(STATE_VIDEO)) or {})
    legal = _loads(state.get(STATE_LEGAL))
    reportable = [v for v in verdict.get("reportable", []) if v.get("vehicle_id")]

    return {
        "video_uri": video_uri,
        "reportable_count": len(reportable),
        "reportable": reportable,
        "verdict": verdict.get("verdict", ""),
        "rounds_used": rounds,
        # True when the judge still wanted more once the rounds ran out.
        "follow_up_exhausted": bool(verdict.get("needs_more")),
        "outstanding_questions": {
            "video": verdict.get("questions_for_video", []),
            "lawyer": verdict.get("questions_for_lawyer", []),
        },
        "evidence": {
            "timeline": video.get("timeline", []) if isinstance(video, dict) else [],
            "vehicles": video.get("vehicles", []) if isinstance(video, dict) else [],
            "legal_analysis": legal,
        },
    }
