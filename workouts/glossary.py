"""Plain-language definitions for metric help popovers ({% metric_help "slug" %}).

Each entry says what the metric *is*: one or two sentences, no health advice, no
diagnosis language. Describe what FitPulse actually shows (see CLAUDE.md), and don't
invent formulas the app doesn't use.
"""

METRIC_HELP = {
    "hrv": (
        "HRV",
        "Heart rate variability: how much the time between heartbeats varies overnight. "
        "Compared with your own baseline, higher usually means better recovered.",
    ),
    "body_battery": (
        "Body Battery",
        "Garmin's 0–100 energy estimate. Sleep and rest charge it; activity and stress drain it.",
    ),
    "readiness_estimated": (
        "Estimated readiness",
        "Calculated by FitPulse from last night's HRV, sleep and resting heart rate against "
        "your own 30-day baseline, when Garmin's score isn't available.",
    ),
    "training_readiness": (
        "Training readiness",
        "Garmin's 0–100 score for how ready you are to train, based on your sleep, recovery "
        "time, HRV, recent training load and stress.",
    ),
    "sleep_score": (
        "Sleep score",
        "Your watch's 0–100 rating of last night's sleep, based on how long you slept and how "
        "that time divided into sleep stages.",
    ),
    "resting_hr": (
        "Resting heart rate",
        "Your heart rate at rest, measured by your watch. The estimated readiness score counts "
        "a value above your own 30-day baseline as less recovered.",
    ),
    "stress": (
        "Stress",
        "Garmin's 0–100 stress level, estimated from your heart rate and its variability through "
        "the day. This is the day's average; lower means calmer.",
    ),
    "azm": (
        "Active Zone Minutes",
        "Google's count of minutes you spent in your fat-burn, cardio or peak heart rate zones. "
        "Cardio and peak minutes count double.",
    ),
    "training_load": (
        "Training load",
        "Garmin's acute training load: the combined effort of your workouts over about the "
        "last seven days.",
    ),
    "vo2_max": (
        "VO2 max",
        "Garmin's estimate of the most oxygen your body can use during hard exercise, in "
        "mL/kg/min, from runs or rides recorded with heart rate.",
    ),
    "vertical_oscillation": (
        "Vertical oscillation",
        "How far your torso moves up and down with each running step, in centimetres, from "
        "Garmin running dynamics.",
    ),
    "vertical_ratio": (
        "Vertical ratio",
        "Vertical oscillation divided by stride length. Lower means more of each step moves "
        "you forward rather than up.",
    ),
    "ground_contact_time": (
        "Ground contact time",
        "How long each foot stays on the ground per running step, in milliseconds, from Garmin "
        "running dynamics.",
    ),
    "run_cadence": (
        "Run cadence",
        "Steps per minute while running. FitPulse leaves out walking stretches (under 140 "
        "steps per minute), so it can be higher than Garmin's own average.",
    ),
    "stride_length": (
        "Stride length",
        "The distance covered by each running step, in centimetres, from Garmin running dynamics.",
    ),
    "effort_points": (
        "Effort points",
        "Peloton's effort score for a class, from the time you spent in each heart rate zone. "
        "Minutes in higher zones earn more points.",
    ),
    "ftp": (
        "FTP",
        "Functional threshold power: the watts you could hold for about an hour. FitPulse uses "
        "it to draw power zones on cycling workouts.",
    ),
    "tdee": (
        "TDEE",
        "Total daily energy expenditure: your estimated calories burned per day. FitPulse "
        "estimates resting burn with the Mifflin-St Jeor formula and multiplies it by your "
        "activity level.",
    ),
    "fat_ratio": (
        "Body fat %",
        "Body fat as a percentage of your weight, measured by your Withings scale.",
    ),
}
