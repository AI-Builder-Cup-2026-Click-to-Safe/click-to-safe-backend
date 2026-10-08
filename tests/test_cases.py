from datetime import datetime, timezone

import pytest

from app.cases import (
    ACT_333_URL,
    build_case,
    decision_trail,
    offence_code,
    offence_label,
    plate,
    vehicle_type,
)

REVIEW = {
    "video_uri": "gs://b/uploads/x",
    "verdict": "Report the white MPV for careless driving under Section 43(1).",
    "reportable_count": 1,
    "reportable": [
        {
            "vehicle_id": "white_mpv",
            "appearance": "White MPV with body damage (licence plate WA 4978 R)",
            "conduct": "Abruptly cut into the middle lane without indicating at 90 km/h.",
            "start_seconds": 10.0,
            "end_seconds": 14.0,
            "legal_basis": "Road Transport Act 1987, Section 43(1)",
            "penalty": "Fine of not less than four thousand ringgit",
            "reasoning": "Cutting in at highway speed without signalling creates danger.",
            "confidence": 0.95,
        }
    ],
    "rounds_used": 3,
    "follow_up_exhausted": False,
    "outstanding_questions": {"video": [], "lawyer": []},
    "coverage": {"duration_seconds": 18.3},
    "tracks": [{"vehicleTrackId": "white_mpv", "frames": [{"atSeconds": 10.0}]}],
    "original_video_url": "https://storage.googleapis.com/x",
    "annotated_video_url": "https://storage.googleapis.com/y",
    "evidence": {
        "timeline": [],
        "vehicles": [],
        "legal_analysis": {
            "findings": [
                {
                    "vehicle_id": "white_mpv",
                    "statute": "Road Transport Act 1987 (Act 333)",
                    "section": "Section 43(1)",
                    "quote": "A person who drives a motor vehicle...",
                    "penalty": "RM4,000-10,000",
                    "relevance": 0.9,
                }
            ],
            "gaps": ["Signalling specifics sit in the Road Traffic Rules 1959."],
        },
    },
}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ran the red light at the stop line", "red_light"),
        ("Drove against the flow of traffic", "counter_flow"),
        ("Drove along the emergency lane", "emergency_lane"),
        ("Cut in front of the ego vehicle", "queue_cut"),
        ("Tailgated aggressively for 20 seconds", "road_rage"),
        ("Parked on a hill facing downwards", "other"),
    ],
)
def test_conduct_maps_to_a_dashboard_code(text, expected):
    assert offence_code(text) == expected


def test_an_unfamiliar_offence_is_kept_rather_than_dropped():
    # The contract says not to discard a case for an unknown code.
    assert offence_code("Something the dashboard has never charted") == "other"


def test_a_plate_is_read_only_when_one_was_seen():
    assert plate("Silver sedan (licence plate BGD 6220)") == "BGD 6220"
    assert plate("White MPV, plate WA 4978 R") == "WA 4978 R"
    # No plate in the description means null, never a guess.
    assert plate("White modern sedan with a full-width light bar") is None


def test_the_vehicle_type_drops_the_plate_text():
    got = vehicle_type("White MPV with body damage (licence plate WA 4978 R)")
    assert "WA 4978" not in got and "MPV" in got


def test_a_label_is_one_readable_phrase():
    label = offence_label("Cut in sharply. Then braked hard in front of the ego vehicle.")
    assert label == "Cut in sharply"
    assert offence_label("") == "Traffic offence"


def test_a_case_carries_what_the_dashboard_reads():
    case = build_case(REVIEW, "RR-TEST01", "MY", datetime.now(timezone.utc))
    assert case["id"] == "RR-TEST01" and case["region"] == "MY"
    assert case["vehicle"]["plate"] == "WA 4978 R"
    assert case["offences"][0]["code"] == "queue_cut"
    assert case["offences"][0]["lawSources"][0]["url"] == ACT_333_URL
    assert case["evidence"]["annotatedVideoUrl"] == "https://storage.googleapis.com/y"
    assert case["reporting"]["status"] == "ready"
    # Real footage, really analysed.
    assert case["evidence"]["isSynthetic"] is False
    assert case["provenance"]["isDemo"] is False


def test_facts_the_footage_cannot_establish_stay_null():
    case = build_case(REVIEW, "RR-1", "MY")
    # The contract asks the UI to collect these, not to show a guess.
    assert case["incident"]["date"] is None
    assert case["incident"]["location"] is None
    assert case["vehicle"]["model"] is None


def test_a_clean_review_reports_clean():
    case = build_case({"reportable": [], "verdict": "Nothing reportable."}, "RR-2")
    assert case["reporting"]["status"] == "clean" and case["offences"] == []


def test_an_unfinished_review_asks_for_more_information():
    case = build_case({**REVIEW, "follow_up_exhausted": True}, "RR-3")
    assert case["reporting"]["status"] == "needs_info"


def test_the_region_sets_the_time_zone():
    assert build_case(REVIEW, "RR-4", "JP")["incident"]["timeZone"] == "Asia/Tokyo"
    # An unknown region falls back rather than emitting an invalid enum.
    assert build_case(REVIEW, "RR-5", "XX")["region"] == "MY"


def test_the_decision_trail_explains_why():
    trail = decision_trail(REVIEW, "RR-TEST01")
    offence = trail["offences"][0]
    assert offence["reasoning"].startswith("Cutting in at highway speed")
    assert offence["offenceId"] == "RR-TEST01-offence-1"
    # The statute text the lawyer actually retrieved, and what it could not ground.
    assert trail["citations"][0]["quote"].startswith("A person who drives")
    assert trail["gaps"]


def test_the_trail_survives_a_review_with_nothing_in_it():
    trail = decision_trail({}, "RR-6")
    assert trail["offences"] == [] and trail["citations"] == []
