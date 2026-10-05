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

logger = logging.getLogger(__name__)

SUMMARIES = "review_summaries"
RECORDS = "review_records"
DEFAULT_PAGE = 20
MAX_PAGE = 100


@lru_cache
def client() -> AsyncClient:
    return AsyncClient(project=os.environ["APP_PROJECT_ID"])


def summarise(review_id: str, result: dict, created_at: datetime) -> dict:
    """The fields a list view shows, and nothing else."""
    reportable = result.get("reportable", []) or []
    return {
        "review_id": review_id,
        "created_at": created_at,
        "video_uri": result.get("video_uri", ""),
        "annotated_video_uri": result.get("annotated_video_uri"),
        "reportable_count": result.get("reportable_count", 0),
        "verdict": result.get("verdict", "")[:500],
        # The provisions cited, so a list can be scanned without opening records.
        "provisions": sorted(
            {r.get("legal_basis", "") for r in reportable if r.get("legal_basis")}
        ),
        "duration_seconds": (result.get("coverage") or {}).get("duration_seconds"),
        "rounds_used": result.get("rounds_used", 0),
    }


async def save(result: dict) -> str:
    """Persist one finished review. Returns its id.

    A failure here must not cost the caller their review, which has already been
    paid for in model calls, so the error is logged and the id returned anyway.
    """
    review_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc)
    try:
        db = client()
        summary = summarise(review_id, result, created_at)
        await db.collection(SUMMARIES).document(review_id).set(summary)
        await (
            db.collection(RECORDS)
            .document(review_id)
            .set({"review_id": review_id, "created_at": created_at, **result})
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
    """Add the URLs a player needs, leaving the stored gs:// URIs in place."""
    return {
        **record,
        "original_video_url": playable(record.get("video_uri")),
        "annotated_video_url": playable(record.get("annotated_video_uri")),
    }
