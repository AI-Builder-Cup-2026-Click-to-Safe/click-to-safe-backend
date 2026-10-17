"""Run with APP_PROJECT_ID and ADC: python -m app.thumbnail_backfill [--apply]."""

import argparse
import asyncio
import json

from app import history, thumbnails


async def backfill(apply: bool = False) -> dict:
    db = history.client()
    records = [snapshot async for snapshot in db.collection(history.RECORDS).stream()]
    counts = {"records": len(records), "updated": 0, "existing": 0, "pending": 0, "failed": []}
    for snapshot in records:
        record = snapshot.to_dict() or {}
        if not apply:
            counts["pending"] += 1
            continue
        try:
            uri = await asyncio.to_thread(thumbnails.render, record)
            summary_ref = db.collection(history.SUMMARIES).document(snapshot.id)
            summary = await summary_ref.get()
            if (
                record.get("thumbnail_uri") == uri
                and (summary.to_dict() or {}).get("thumbnail_uri") == uri
            ):
                counts["existing"] += 1
                continue
            batch = db.batch()
            # Field updates preserve verdicts, reporting state and concurrent user edits.
            batch.update(snapshot.reference, {"thumbnail_uri": uri})
            if summary.exists:
                batch.update(summary_ref, {"thumbnail_uri": uri})
            else:
                batch.create(
                    summary_ref,
                    history.summarise(
                        snapshot.id,
                        {**record, "thumbnail_uri": uri},
                        record["created_at"],
                        record.get("case"),
                    ),
                )
            await batch.commit()
            counts["updated"] += 1
            print(json.dumps({"review_id": snapshot.id, "status": "updated"}), flush=True)
        except Exception as exc:
            failure = {"review_id": snapshot.id, "error": type(exc).__name__}
            counts["failed"].append(failure)
            print(json.dumps(failure), flush=True)
    print(json.dumps(counts), flush=True)
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Generate images and update URI fields"
    )
    args = parser.parse_args()
    outcome = asyncio.run(backfill(args.apply))
    raise SystemExit(1 if outcome["failed"] else 0)
