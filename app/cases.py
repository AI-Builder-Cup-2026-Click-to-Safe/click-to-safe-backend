"""Shape a finished review as the Case the front end's contract describes.

The review answers in the vocabulary of the agents that produced it — a verdict,
reportable vehicles, a lawyer's findings. The dashboard reads something else: a
Case with a vehicle, an incident, offences carrying dashboard codes, evidence,
and provenance saying where each part came from.

This translates one into the other. Nothing is invented: a fact the review did
not establish is `null`, because the contract asks the UI to let a person fill
those in rather than show a guess as if it were read from the footage.
"""

import re
from datetime import datetime, timedelta, timezone

# The codes the dashboard charts. `emergency_lane` has no bar of its own but
# persists in the fixtures, so a case carrying it is still shown.
KNOWN_CODES = ("red_light", "queue_cut", "road_rage", "counter_flow", "emergency_lane")

# What a judge's prose has to say for a dashboard code to apply. Ordered: the
# first match wins, so the more specific phrasings come first.
CODE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("red_light", r"red light|red signal|stop line|traffic light"),
    ("counter_flow", r"counter.?flow|wrong way|oncoming lane|against the (flow|traffic)"),
    ("emergency_lane", r"emergency lane|hard shoulder|\bshoulder\b"),
    ("queue_cut", r"cut(ting)? (in|across|off)|queue|overtak|undertak|squeez|lane change"),
    ("road_rage", r"road rage|tailgat|brake check|aggressiv|harass|intimidat"),
)

# A code the dashboard does not know is still carried, so a case is never lost
# for want of a bar on a chart. Falls back to a readable slug of the conduct.
FALLBACK_CODE = "other"

# The Act the corpus is built from, as published by the Attorney General's
# Chambers. The contract requires a real URI on every law source, and this is
# the same document the design's own fixture cites.
ACT_333_URL = (
    "https://lom.agc.gov.my/ilims/upload/portal/akta/outputaktap/1871744_BI/"
    "Act%20333_Final_15%20Oct%202023%20(25.3.24).pdf"
)

TIME_ZONES = {"MY": "Asia/Kuala_Lumpur", "SG": "Asia/Singapore", "JP": "Asia/Tokyo"}
DEFAULT_REGION = "MY"


def offence_code(text: str) -> str:
    """The dashboard code for this conduct, or a readable slug when none fits."""
    lowered = (text or "").lower()
    for code, pattern in CODE_PATTERNS:
        if re.search(pattern, lowered):
            return code
    return FALLBACK_CODE


def offence_label(conduct: str) -> str:
    """A short human label. The judge's own words, trimmed to a phrase."""
    text = (conduct or "").strip()
    if not text:
        return "Traffic offence"
    first = re.split(r"(?<=[.;])\s", text)[0].strip().rstrip(".")
    return (first[:117] + "...") if len(first) > 120 else first


def plate(appearance: str) -> str | None:
    """A registration number if the analyst read one, else None.

    Malaysian plates are letters then digits, optionally a trailing letter. A
    description without one yields None rather than a guess: the contract wants
    the user to supply what was not actually seen.
    """
    match = re.search(r"\b([A-Z]{1,3}\s?\d{1,4}\s?[A-Z]?)\b", appearance or "")
    if not match:
        return None
    found = re.sub(r"\s+", " ", match.group(1)).strip()
    # Two bare letters or a lone number is not a plate.
    return found if re.search(r"\d", found) and len(found) >= 4 else None


def vehicle_type(appearance: str) -> str | None:
    """The body description, with any plate text removed."""
    text = re.sub(r"\(?\s*(licence|license) plate[^)]*\)?", "", appearance or "", flags=re.I)
    text = re.sub(r"\b[A-Z]{1,3}\s?\d{1,4}\s?[A-Z]?\b", "", text)
    text = re.sub(r"[,(]\s*\)?|\s{2,}", " ", text).strip(" ,-()")
    return text or None


def law_sources(findings: list[dict], vehicle_id: str, retrieved_at: str) -> list[dict]:
    """The provisions the lawyer actually cited for this vehicle."""
    sources = []
    for finding in findings:
        if finding.get("vehicle_id") and finding["vehicle_id"] != vehicle_id:
            continue
        statute = finding.get("statute") or ""
        if not statute:
            continue
        sources.append(
            {
                "title": statute,
                # The retrieved text is the Act itself; link the published PDF
                # rather than leaving a field the contract requires as a URI.
                "url": ACT_333_URL,
                "section": finding.get("section") or None,
                "retrievedAt": retrieved_at,
            }
        )
    return sources


def stamped_incident(overlay: dict | None, at_seconds: float) -> dict:
    """Date, time and place from the camera's burned-in stamp, where readable.

    The stamp is the dashcam's own clock, so it is offered as a prefill the user
    checks before reporting, and anything malformed is left for them to enter.
    """
    overlay = overlay if isinstance(overlay, dict) else {}
    found: dict = {"date": None, "time": None, "location": None}
    try:
        clock = datetime.strptime(f"{overlay['date']} {overlay['time']}", "%Y-%m-%d %H:%M:%S")
    except (KeyError, TypeError, ValueError):
        clock = None
    if clock is not None:
        # The stamp was read at one moment; the offence may come later.
        clock += timedelta(seconds=at_seconds - float(overlay.get("at_seconds") or 0))
        found["date"], found["time"] = clock.strftime("%Y-%m-%d"), clock.strftime("%H:%M")
    lat, lon = overlay.get("latitude"), overlay.get("longitude")
    if (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and (lat, lon) != (0, 0)
    ):
        found["location"] = f"{lat:.5f}, {lon:.5f}"
    return found


def build_case(
    review: dict, case_id: str, region: str = DEFAULT_REGION, created_at: datetime | None = None
) -> dict:
    """One review as one Case. Reported vehicles become its offences.

    A review names at most a handful of vehicles and the dashboard shows a case
    per incident, so the first reported vehicle is the case's subject and every
    reported finding becomes an offence on it.
    """
    at = created_at or datetime.now(timezone.utc)
    stamp = at.isoformat()
    reportable = review.get("reportable") or []
    evidence = review.get("evidence") or {}
    findings = ((evidence.get("legal_analysis") or {}) or {}).get("findings") or []
    subject = reportable[0] if reportable else {}

    offences = []
    for index, item in enumerate(reportable, start=1):
        conduct = item.get("conduct") or ""
        offences.append(
            {
                "id": f"{case_id}-offence-{index}",
                "code": offence_code(f"{conduct} {item.get('reasoning', '')}"),
                "label": offence_label(conduct),
                "atSeconds": float(item.get("start_seconds") or 0),
                "confidence": float(item.get("confidence") or 0),
                # Everything the judge reported starts included; the user may
                # drop one through CasePatch.
                "included": True,
                "lawSources": law_sources(findings, item.get("vehicle_id", ""), stamp),
            }
        )

    coverage = review.get("coverage") or {}
    appearance = subject.get("appearance") or ""
    return {
        "id": case_id,
        "version": 1,
        "region": region if region in TIME_ZONES else DEFAULT_REGION,
        "vehicle": {
            "plate": plate(appearance),
            "type": vehicle_type(appearance),
            # Nothing in the review establishes a model; the user supplies it.
            "model": None,
        },
        "incident": {
            **stamped_incident(
                evidence.get("overlay"), offences[0]["atSeconds"] if offences else 0
            ),
            "timeZone": TIME_ZONES.get(region, TIME_ZONES[DEFAULT_REGION]),
            "description": review.get("verdict") or None,
        },
        "offences": offences,
        "evidence": {
            "durationSeconds": float(coverage.get("duration_seconds") or 0),
            "originalVideoUrl": review.get("original_video_url"),
            "annotatedVideoUrl": review.get("annotated_video_url"),
            "thumbnailUrl": None,
            "tracks": review.get("tracks") or [],
            # Real footage, really analysed: never a fixture.
            "isSynthetic": False,
        },
        "reporting": {
            # The judge wanting more detail is exactly "needs_info"; no offence
            # at all is "clean"; otherwise it is ready for a person to send.
            "status": (
                "needs_info"
                if review.get("follow_up_exhausted")
                else ("ready" if offences else "clean")
            ),
            "confirmedByUser": False,
        },
        "authorityOutcome": {"status": "unknown", "source": None, "updatedAt": None},
        "provenance": {
            "isDemo": False,
            "factsSource": "gemini_video_analysis",
            "legalRetrieval": "rag_act_333" if findings else "not_performed",
        },
    }


def decision_trail(review: dict, case_id: str) -> dict:
    """Why the model decided what it did, for the "Why this may be an offence" view.

    The Case schema is closed — every object sets additionalProperties false and
    the front end's validator rejects an unknown key — so this travels beside a
    Case rather than inside it. The UI can ignore it and still validate.
    """
    evidence = review.get("evidence") or {}
    legal = (evidence.get("legal_analysis") or {}) or {}
    reportable = review.get("reportable") or []
    return {
        "caseId": case_id,
        "verdict": review.get("verdict") or "",
        # The Evidence schema is closed and has no field for an excerpt, so the
        # cut-down clip is offered here rather than being dropped.
        "excerptVideoUrl": review.get("excerpt_video_url"),
        # One entry per offence, in the same order, so the UI can line them up.
        "offences": [
            {
                "offenceId": f"{case_id}-offence-{index}",
                "vehicleId": item.get("vehicle_id") or "",
                "observed": item.get("conduct") or "",
                "reasoning": item.get("reasoning") or "",
                "legalBasis": item.get("legal_basis") or "",
                "penalty": item.get("penalty") or "",
                "confidence": float(item.get("confidence") or 0),
                "window": {
                    "startSeconds": float(item.get("start_seconds") or 0),
                    "endSeconds": float(item.get("end_seconds") or 0),
                },
            }
            for index, item in enumerate(reportable, start=1)
        ],
        # The statute text the lawyer actually retrieved, quoted.
        "citations": [
            {
                "vehicleId": f.get("vehicle_id") or "",
                "statute": f.get("statute") or "",
                "section": f.get("section") or "",
                "quote": f.get("quote") or "",
                "penalty": f.get("penalty") or "",
                "relevance": float(f.get("relevance") or 0),
            }
            for f in (legal.get("findings") or [])
        ],
        # What the lawyer said it could not ground, which is what keeps the UI
        # honest about the limits of the citation it is showing.
        "gaps": legal.get("gaps") or [],
        "roundsUsed": review.get("rounds_used", 0),
        "followUpExhausted": bool(review.get("follow_up_exhausted")),
        "outstandingQuestions": review.get("outstanding_questions") or {"video": [], "lawyer": []},
        "observedTimeline": evidence.get("timeline") or [],
    }
