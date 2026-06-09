"""
Output format specs shared across AI prompts.

Do not change these strings without updating the template filters that parse
the structured output: format_day_analysis, format_body_commentary,
format_next_workout.
"""

HEADLINE_BULLETS_FORMAT = """\
Respond in exactly this format with no extra text before or after:

HEADLINE: <one punchy sentence>
• <specific observation referencing actual numbers>
• <specific observation referencing actual numbers>
• <specific observation referencing actual numbers>

Max 3 bullets. Each bullet should cite a specific metric."""

INTENSITY_ACTIVITY_REASON_FORMAT = """\
You MUST respond in exactly this format with no extra text before or after:

INTENSITY: <one of: GO HARD / GO MODERATE / GO EASY / REST>
ACTIVITY: <specific workout — choose the best modality (running, cycling, walking, hiking, strength) and give duration and intensity guidance in one line>
REASON: <2–3 sentences explaining why, referencing the actual data. Be specific and direct — say which muscle groups are recovered vs fatigued, and why this modality (run vs ride vs lift vs walk) fits better than the alternatives today.>"""
