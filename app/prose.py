"""Wording guidance shared by the video and legal review prompts."""

REPORT_PROSE_STYLE = (
    "User-facing prose style:\n"
    "- Use concise, plain descriptions of observable facts and cited law. Avoid dramatic, "
    "emotional or moral judgments, speculative intent, and unsupported severity. State "
    "uncertainty where the evidence warrants it. Do not minimize observed hazards or "
    "supported violations.\n"
    "- In your own prose, use 'camera vehicle' instead of 'ego'. Mention it only when "
    "needed to explain an action or spatial relationship, not in every sentence.\n"
    "- These are wording rules, not changes to detection or legal decision criteria. "
    "Preserve observations, timestamps, confidence, exact statutory quotations and penalty "
    "amounts. Keep fixed enums and machine fields unchanged, including vehicle_id, "
    "actor_id='ego', and is_ego."
)
