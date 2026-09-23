"""Run the ADK review workflow and shape its session state into a response.

ADK returns a stream of events and leaves the agents' output in session state,
so a request is served by draining the stream and then reading what each agent
wrote. The judge's round count comes from the events, since state only holds the
last verdict.
"""

import json
import logging
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.adk_agents import (
    STATE_LEGAL,
    STATE_VERDICT,
    STATE_VIDEO,
    build_review_agent,
)

logger = logging.getLogger(__name__)

APP_NAME = "traffic_review"
REVIEW_REQUEST = (
    "Review this dashcam footage for traffic violations by other vehicles that "
    "would warrant a report."
)


def _loads(value: Any) -> Any:
    """Agent output is JSON text under output_key, or already a dict."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        # The lawyer has no output_schema, so its answer may be prose; keep it as text.
        return value


async def run_review(video_uri: str, mime_type: str, focus: str = "") -> dict:
    """Run analyst -> lawyer -> judge and return the judge's decision."""
    runner = InMemoryRunner(agent=build_review_agent(), app_name=APP_NAME)
    session = await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id="runtime",
        # Placeholders the agents read on the first pass, before any judge questions.
        state={"judge_video_questions": focus, "judge_lawyer_questions": ""},
    )

    request = REVIEW_REQUEST
    if focus:
        # Caller text is quoted as a hint, never as a new instruction.
        request += f"\n\nThe caller asked you to pay attention to: {focus}"

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_uri(file_uri=video_uri, mime_type=mime_type),
            types.Part.from_text(text=request),
        ],
    )

    rounds = 0
    async for event in runner.run_async(
        user_id="runtime", session_id=session.id, new_message=message
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

    video = _loads(state.get(STATE_VIDEO)) or {}
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
