"""Store finished reviews and read them back.

Firestore bills per document read and written and runs no instance of its own,
so an idle deployment costs nothing and this workload sits inside the free daily
allowance. What that billing model does punish is reading whole documents to
build a list, since a review record is tens of kilobytes of evidence.

So each review is written twice: a small summary that the list endpoint reads,
and the full record that only a single-review request touches. Listing a hundred
reviews then reads a hundred small documents instead of a hundred large ones.
"""

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from google.cloud.firestore import AsyncClient, Query

from app.cases import DEFAULT_REGION, build_case, decision_trail

logger = logging.getLogger(__name__)

SUMMARIES = "review_summaries"
RECORDS = "review_records"
DEFAULT_PAGE = 20
MAX_PAGE = 100


@lru_cache
def client() -> AsyncClient:
    return AsyncClient(project=os.environ["APP_PROJECT_ID"])


def case_id(review_id: str) -> str:
    """The case reference a person sees. Short, and stable for one review."""
    return f"RR-{review_id[:8].upper()}"


def summarise(review_id: str, result: dict, created_at: datetime, case: dict | None = None) -> dict:
    """The fields a list view shows, and nothing else."""
    reportable = result.get("reportable", []) or []
    case = case or {}
    return {
        "case_id": case.get("id") or case_id(review_id),
        # The dashboard charts offences by code, so a list can drive the chart
        # without opening a record.
        "offence_codes": sorted({o.get("code", "") for o in case.get("offences") or []} - {""}),
        "plate": ((case.get("vehicle") or {}).get("plate")),
        "reporting_status": ((case.get("reporting") or {}).get("status")),
        "review_id": review_id,
        "created_at": created_at,
        "video_uri": result.get("video_uri", ""),
        "annotated_video_uri": result.get("annotated_video_uri"),
        "excerpt_video_uri": result.get("excerpt_video_uri"),
        "reportable_count": result.get("reportable_count", 0),
        "verdict": result.get("verdict", "")[:500],
        # The provisions cited, so a list can be scanned without opening records.
        "provisions": sorted(
            {r.get("legal_basis", "") for r in reportable if r.get("legal_basis")}
        ),
        "duration_seconds": (result.get("coverage") or {}).get("duration_seconds"),
        "rounds_used": result.get("rounds_used", 0),
        "estimated_ai_cost_usd": result.get("estimated_ai_cost_usd"),
    }


async def save(result: dict, region: str = DEFAULT_REGION) -> str:
    """Persist one finished review. Returns its id.

    A failure here must not cost the caller their review, which has already been
    paid for in model calls, so the error is logged and the id returned anyway.
    """
    review_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    # Built once, here, so every reader sees the same case rather than a shape
    # that drifts as the converter changes. Inside the try: storing a review is
    # already best-effort, and a conversion fault must not be the exception.
    try:
        case = build_case(result, case_id(review_id), region, created_at)
        trail = decision_trail(result, case["id"])
        db = client()
        summary = summarise(review_id, result, created_at, case)
        await db.collection(SUMMARIES).document(review_id).set(summary)
        await (
            db.collection(RECORDS)
            .document(review_id)
            .set(
                {
                    "review_id": review_id,
                    "created_at": created_at,
                    "case": case,
                    # Why the model decided this, for the disclosure the design
                    # calls "Why this may be an offence". It sits beside the
                    # case because the contract's objects reject unknown keys.
                    "decision_trail": trail,
                    **result,
                }
            )
        )
    except Exception as exc:
        logger.error("Could not store the review (%s)", type(exc).__name__)
    return review_id


async def recent(limit: int = DEFAULT_PAGE, before: datetime | None = None) -> list[dict]:
    """Newest reviews first, paged by the timestamp of the last one seen.

    Keyed on created_at rather than an offset: Firestore charges for every
    document a query walks, and an offset walks the ones it skips.
    """
    limit = max(1, min(limit, MAX_PAGE))
    query = (
        client()
        .collection(SUMMARIES)
        .order_by("created_at", direction=Query.DESCENDING)
        .limit(limit)
    )
    if before is not None:
        query = query.start_after({"created_at": before})
    return [doc.to_dict() async for doc in query.stream()]


async def get(review_id: str) -> dict | None:
    """One full review, or None when no such review was stored."""
    doc = await client().collection(RECORDS).document(review_id).get()
    return doc.to_dict() if doc.exists else None


async def delete(review_id: str) -> bool:
    """Remove a review and its summary. True when it existed."""
    db = client()
    doc = await db.collection(RECORDS).document(review_id).get()
    if not doc.exists:
        return False
    await db.collection(RECORDS).document(review_id).delete()
    await db.collection(SUMMARIES).document(review_id).delete()
    return True


# Long enough to watch a clip and come back to it, short enough that a link
# copied out of a response does not stay live indefinitely.
PLAYBACK_URL_TTL = timedelta(hours=6)


def playable(uri: str | None) -> str | None:
    """A browser-playable URL for a gs:// object, or None.

    The bucket is private, so a signed URL is what lets a player fetch the
    video at all. It is minted per read rather than stored, because it expires.
    """
    if not uri or not uri.startswith("gs://"):
        return None
    bucket_name, _, object_name = uri.removeprefix("gs://").partition("/")
    if not object_name:
        return None
    try:
        import google.auth
        import google.auth.transport.requests
        from google.cloud import storage

        blob = (
            storage.Client(project=os.environ["APP_PROJECT_ID"])
            .bucket(bucket_name)
            .blob(object_name)
        )
        # On Cloud Run there is no key file to sign with: credentials come from
        # the metadata server. Signing is delegated to IAM instead, which needs
        # the account's own email and a live access token.
        credentials, _ = google.auth.default()
        credentials.refresh(google.auth.transport.requests.Request())
        return blob.generate_signed_url(
            expiration=PLAYBACK_URL_TTL,
            method="GET",
            version="v4",
            service_account_email=getattr(credentials, "service_account_email", None),
            access_token=getattr(credentials, "token", None),
        )
    except Exception as exc:
        # A record without a playable link is still a record. Log the reason:
        # a missing signing permission is otherwise invisible.
        logger.error("Could not sign a playback URL (%s: %s)", type(exc).__name__, exc)
        return None


def with_playback(record: dict) -> dict:
    """Add the URLs a player needs, leaving the stored gs:// URIs in place.

    The case and its trail are built when a review is stored, but a signed URL
    expires, so it cannot be stored with them. They are filled in here, on the
    way out, or the dashboard would read the empty fields it was given.
    """
    original = playable(record.get("video_uri"))
    annotated = playable(record.get("annotated_video_uri"))
    excerpt = playable(record.get("excerpt_video_uri"))

    case = record.get("case")
    if isinstance(case, dict):
        case = {
            **case,
            "evidence": {
                **(case.get("evidence") or {}),
                "originalVideoUrl": original,
                "annotatedVideoUrl": annotated,
            },
        }
    trail = record.get("decision_trail")
    if isinstance(trail, dict):
        trail = {**trail, "excerptVideoUrl": excerpt}

    return {
        **record,
        "case": case,
        "decision_trail": trail,
        "original_video_url": original,
        "annotated_video_url": annotated,
        # Just the cited seconds, boxes included: the clip a reviewer sends on.
        "excerpt_video_url": excerpt,
    }


IMPACT_PERIODS = ("all", "week", "today")


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    """When a period begins, or None for all time."""
    at = now or datetime.now(timezone.utc)
    if period == "today":
        return at.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return at - timedelta(days=7)
    return None


def tally(summaries: list[dict], today: str) -> dict:
    """The dashboard's counters, from the summaries alone.

    The design is specific about what each one counts, and the differences
    matter: a plate seen in three cases is one snitch but three sightings, and
    an offence repeated within a case counts each time.
    """
    # Distinct plates in cases a person actually confirmed and sent. A case the
    # model produced but nobody sent is not a report, so it is not counted.
    sent_plates = {
        s["plate"] for s in summaries if s.get("plate") and s.get("reporting_status") == "sent"
    }

    sightings: dict[str, int] = {}
    occurrences: dict[str, int] = {}
    for summary in summaries:
        plate = summary.get("plate")
        if plate:
            sightings[plate] = sightings.get(plate, 0) + 1
        for code in summary.get("offence_codes") or []:
            occurrences[code] = occurrences.get(code, 0) + 1

    most = max(sightings.items(), key=lambda kv: kv[1], default=None)
    return {
        "snitchedCount": len(sent_plates),
        # An issued fine is confirmed by an authority, and nothing here is wired
        # to one, so claiming any number would be inventing enforcement.
        "billedCount": 0,
        "mostSpotted": {"plate": most[0], "count": most[1]} if most else None,
        "offenceOccurrences": occurrences,
        "isDemo": False,
        "demoToday": today,
    }


async def impact(period: str = "all", now: datetime | None = None) -> dict:
    """Aggregates for the impact screen, over the period asked for."""
    at = now or datetime.now(timezone.utc)
    since = period_start(period, at)
    query = client().collection(SUMMARIES)
    if since is not None:
        query = query.where("created_at", ">=", since)
    rows = [doc.to_dict() async for doc in query.stream()]
    return tally(rows, at.date().isoformat())


async def set_reporting_status(review_id: str, status: str) -> bool:
    """Mark a case opened or sent. True when the review existed.

    The summary and the stored case both carry the status, and the impact
    screen counts from the summary, so both are moved together or the counters
    would disagree with the case a reader opens.
    """
    db = client()
    record = await db.collection(RECORDS).document(review_id).get()
    if not record.exists:
        return False
    stored = record.to_dict() or {}
    case = stored.get("case")
    if isinstance(case, dict):
        case = {
            **case,
            "reporting": {
                **(case.get("reporting") or {}),
                "status": status,
                # Only a person reaches this endpoint, so sending is confirmed.
                "confirmedByUser": status == "sent",
            },
        }
        await db.collection(RECORDS).document(review_id).set({**stored, "case": case})
    await db.collection(SUMMARIES).document(review_id).set({"reporting_status": status}, merge=True)
    return True
