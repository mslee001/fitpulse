from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class DailyStats(models.Model):
    """Garmin wellness data for a single calendar day."""
    date = models.DateField(unique=True, db_index=True)

    # Body battery (time series + daily extremes)
    body_battery_json = models.JSONField(default=list, blank=True)
    body_battery_high = models.IntegerField(null=True, blank=True)
    body_battery_low = models.IntegerField(null=True, blank=True)

    # Sleep (the night leading into this date)
    sleep_score = models.IntegerField(null=True, blank=True)
    sleep_seconds = models.IntegerField(null=True, blank=True)
    sleep_deep_seconds = models.IntegerField(null=True, blank=True)
    sleep_light_seconds = models.IntegerField(null=True, blank=True)
    sleep_rem_seconds = models.IntegerField(null=True, blank=True)
    sleep_baseline_seconds = models.FloatField(null=True, blank=True)  # trailing 30-day avg, for readiness_score

    # HRV
    hrv_weekly_avg = models.FloatField(null=True, blank=True)   # ms
    hrv_last_night = models.FloatField(null=True, blank=True)   # ms
    hrv_status = models.CharField(max_length=32, blank=True)    # BALANCED / UNBALANCED / POOR

    # Resting HR & stress
    resting_hr = models.IntegerField(null=True, blank=True)
    resting_hr_baseline = models.FloatField(null=True, blank=True)  # trailing 30-day avg, for readiness_score
    stress_avg = models.IntegerField(null=True, blank=True)

    # Training status & load (from get_training_status)
    training_status = models.CharField(max_length=64, blank=True)
    training_load = models.FloatField(null=True, blank=True)         # acute load
    load_focus_anaerobic = models.FloatField(null=True, blank=True)
    load_focus_high_aerobic = models.FloatField(null=True, blank=True)
    load_focus_low_aerobic = models.FloatField(null=True, blank=True)

    # Training readiness (0–100 score + label) — real Garmin algorithm
    training_readiness_score = models.IntegerField(null=True, blank=True)
    training_readiness_label = models.CharField(max_length=64, blank=True)

    # Readiness proxy computed from Google Health signals (HRV/sleep/RHR vs.
    # personal baseline) for days with no real Garmin training_readiness_score.
    # Populated by the Google Health wellness sync once wellness_days_synced
    # clears the 7-day reliability floor. Use the readiness_score/
    # readiness_label properties, not these fields, directly.
    computed_readiness_score = models.IntegerField(null=True, blank=True)
    computed_readiness_label = models.CharField(max_length=16, blank=True)

    # Trailing 30-day count of days with any recovery signal (HRV, resting
    # HR, or sleep) — gates whether computed_readiness_score is reliable.
    wellness_days_synced = models.IntegerField(null=True, blank=True)

    # Activity volume
    steps = models.IntegerField(null=True, blank=True)
    floors_climbed = models.IntegerField(null=True, blank=True)
    active_calories = models.IntegerField(null=True, blank=True)
    total_calories = models.IntegerField(null=True, blank=True)
    bmr_calories = models.IntegerField(null=True, blank=True)
    moderate_intensity_minutes = models.IntegerField(null=True, blank=True)
    vigorous_intensity_minutes = models.IntegerField(null=True, blank=True)
    steps_goal = models.IntegerField(null=True, blank=True)
    floors_climbed_goal = models.IntegerField(null=True, blank=True)
    intensity_minutes_goal = models.IntegerField(null=True, blank=True)

    # Fitness markers
    vo2_max_running = models.FloatField(null=True, blank=True)
    vo2_max_cycling = models.FloatField(null=True, blank=True)
    fitness_age = models.IntegerField(null=True, blank=True)

    # Respiratory & oxygenation
    respiration_avg = models.FloatField(null=True, blank=True)
    respiration_waking_avg = models.FloatField(null=True, blank=True)
    respiration_sleep_avg = models.FloatField(null=True, blank=True)
    spo2_sleep_avg = models.FloatField(null=True, blank=True)
    spo2_sleep_low = models.IntegerField(null=True, blank=True)

    # Body battery refinement
    body_battery_start = models.IntegerField(null=True, blank=True)
    body_battery_end = models.IntegerField(null=True, blank=True)
    body_battery_charge = models.IntegerField(null=True, blank=True)
    body_battery_drain = models.IntegerField(null=True, blank=True)

    # Stress refinement
    stress_max = models.IntegerField(null=True, blank=True)
    stress_rest_minutes = models.IntegerField(null=True, blank=True)
    stress_low_minutes = models.IntegerField(null=True, blank=True)
    stress_medium_minutes = models.IntegerField(null=True, blank=True)
    stress_high_minutes = models.IntegerField(null=True, blank=True)

    # HRV refinement
    hrv_min = models.IntegerField(null=True, blank=True)
    hrv_max = models.IntegerField(null=True, blank=True)

    # AI analysis for this day
    ai_day_analysis = models.TextField(null=True, blank=True)
    ai_day_generated_at = models.DateTimeField(null=True, blank=True)

    # Next-workout recommendation (stored on "today" and refreshed daily)
    ai_next_workout = models.TextField(null=True, blank=True)
    ai_next_workout_generated_at = models.DateTimeField(null=True, blank=True)

    synced_at = models.DateTimeField(null=True, blank=True)

    # Body composition — daily aggregate from Withings
    weight_lb = models.FloatField(null=True, blank=True)
    fat_mass_lb = models.FloatField(null=True, blank=True)
    fat_free_mass_lb = models.FloatField(null=True, blank=True)
    muscle_mass_lb = models.FloatField(null=True, blank=True)
    hydration_lb = models.FloatField(null=True, blank=True)
    bone_mass_lb = models.FloatField(null=True, blank=True)
    fat_ratio_pct = models.FloatField(null=True, blank=True)
    weight_count = models.IntegerField(default=0)
    weight_synced_at = models.DateTimeField(null=True, blank=True)

    # Nutrition daily rollup (summed from FoodEntry rows)
    cal_total = models.FloatField(null=True, blank=True)
    protein_g_total = models.FloatField(null=True, blank=True)
    carbs_g_total = models.FloatField(null=True, blank=True)
    fat_g_total = models.FloatField(null=True, blank=True)
    fiber_g_total = models.FloatField(null=True, blank=True)

    # Provenance for wellness fields shared across sources — see CLAUDE.md
    # "No parallel Garmin/Google Health wellness sync" decision.
    wellness_source = models.CharField(
        max_length=16,
        choices=[("garmin", "Garmin"), ("google_health", "Google Health")],
        null=True, blank=True,
    )
    google_health_synced_at = models.DateTimeField(null=True, blank=True)

    # Active Zone Minutes (Fitbit/Google Health weighted zone-minutes score) — Google Health only
    active_zone_minutes = models.IntegerField(null=True, blank=True)

    # Skin temperature vs. 30-day baseline — illness/recovery signal, Google Health only
    skin_temp_c = models.FloatField(null=True, blank=True)
    skin_temp_deviation_c = models.FloatField(null=True, blank=True)

    # Time in heart rate zone (minutes/day) — Google Health only
    hr_zone_light_minutes = models.IntegerField(null=True, blank=True)
    hr_zone_moderate_minutes = models.IntegerField(null=True, blank=True)
    hr_zone_vigorous_minutes = models.IntegerField(null=True, blank=True)
    hr_zone_peak_minutes = models.IntegerField(null=True, blank=True)

    # Sedentary time (minutes/day) — Google Health only
    sedentary_minutes = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"DailyStats {self.date}"

    @property
    def sleep_minutes(self):
        return self.sleep_seconds // 60 if self.sleep_seconds else None

    @property
    def hrv_status_display(self):
        return (self.hrv_status or "").replace("_", " ").title()

    @property
    def has_wellness_data(self):
        """True if either a real Garmin wellness sync or a Google Health
        wellness sync has ever populated this row. Use this to gate wellness
        UI instead of checking synced_at directly — synced_at is stamped
        only by the Garmin path, so gating on it alone hides Google
        Health-only days entirely."""
        return bool(self.synced_at or self.google_health_synced_at)

    def compute_readiness_proxy(self):
        """Heuristic 0-100 readiness proxy from Google Health recovery
        signals, for days with no real Garmin training_readiness_score.
        Compares sleep, resting HR, and HRV each against your own trailing
        personal baseline (not an absolute number) and blends them at target
        weights HRV 50 / sleep 30 / resting HR 20 — HRV is the most direct
        autonomic recovery signal, resting HR corroborates it. Any missing
        input's weight is redistributed across the ones present.

        Requires at least 7 days of recent wellness history
        (wellness_days_synced) before returning a score at all — a personal
        baseline built from only a day or two isn't reliable. Returns
        (score, label) or (None, None). Labels: Low 1-29 (prioritize
        recovery — lower-intensity work like stretching/yoga), Moderate
        30-64 (HR and sleep are about usual — body is balancing training
        and stress with recovery), High 65-100 (well-rested and recovered).

        Called by the Google Health sync to populate computed_readiness_score
        /computed_readiness_label once the trailing baselines
        (hrv_weekly_avg, sleep_baseline_seconds, resting_hr_baseline) are
        current for this date — not meant to be called ad hoc elsewhere; use
        the readiness_score/readiness_label properties instead."""
        if (self.wellness_days_synced or 0) < 7:
            return None, None

        hrv_score = None
        if self.hrv_last_night and self.hrv_weekly_avg:
            ratio = self.hrv_last_night / self.hrv_weekly_avg
            hrv_score = max(0, min(100, 70 + (ratio - 1.0) * 100))

        sleep_score = None
        if self.sleep_seconds and self.sleep_baseline_seconds:
            ratio = self.sleep_seconds / self.sleep_baseline_seconds
            sleep_score = max(0, min(100, 70 + (ratio - 1.0) * 100))

        rhr_score = None
        if self.resting_hr and self.resting_hr_baseline:
            ratio = self.resting_hr / self.resting_hr_baseline
            # Higher-than-baseline resting HR is a fatigue/illness signal, so
            # this runs the opposite direction of the HRV/sleep ratios above.
            rhr_score = max(0, min(100, 70 - (ratio - 1.0) * 100))

        weighted = [(s, w) for s, w in
                    [(hrv_score, 0.5), (sleep_score, 0.3), (rhr_score, 0.2)] if s is not None]
        if not weighted:
            return None, None

        total_weight = sum(w for _, w in weighted)
        composite = round(sum(s * w for s, w in weighted) / total_weight)
        label = "High" if composite >= 65 else "Moderate" if composite >= 30 else "Low"
        return composite, label

    @property
    def readiness_score(self):
        """Garmin's own training_readiness_score when available (never
        overridden); otherwise the Google Health-derived computed_readiness_score.
        None if neither source has a value."""
        if self.training_readiness_score is not None:
            return self.training_readiness_score
        return self.computed_readiness_score

    @property
    def readiness_label(self):
        if self.training_readiness_score is not None:
            return self.training_readiness_label
        return self.computed_readiness_label or ""

    @property
    def readiness_is_computed(self):
        """True when readiness_score is the Google Health-derived estimate
        rather than Garmin's own algorithm — templates and AI prompts use
        this to label the number as an estimate."""
        return self.training_readiness_score is None and self.computed_readiness_score is not None

    @property
    def readiness_color(self):
        """green/yellow/red for UI, using the label bands that actually
        apply to whichever source produced the score — Garmin's own bands
        (>=70/>=40) for a real score, the proxy's bands (>=65/>=30) for a
        computed one, so color never contradicts readiness_label."""
        score = self.readiness_score
        if score is None:
            return ""
        if self.readiness_is_computed:
            if score >= 65:
                return "green"
            if score >= 30:
                return "yellow"
            return "red"
        if score >= 70:
            return "green"
        if score >= 40:
            return "yellow"
        return "red"


class BodyMeasurement(models.Model):
    """A single Withings scale measurement group (one weigh-in session)."""
    measured_at = models.DateTimeField(db_index=True)
    date = models.DateField(db_index=True)  # local date for fast daily queries
    source = models.CharField(max_length=20, default="withings")
    weight_lb = models.FloatField(null=True, blank=True)
    fat_mass_lb = models.FloatField(null=True, blank=True)
    fat_free_mass_lb = models.FloatField(null=True, blank=True)
    muscle_mass_lb = models.FloatField(null=True, blank=True)
    bone_mass_lb = models.FloatField(null=True, blank=True)
    hydration_lb = models.FloatField(null=True, blank=True)
    fat_ratio_pct = models.FloatField(null=True, blank=True)
    raw_data = models.JSONField(default=dict, blank=True)
    withings_grpid = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-measured_at"]
        constraints = [
            models.UniqueConstraint(fields=["source", "withings_grpid"], name="unique_withings_measurement"),
        ]

    def __str__(self):
        return f"BodyMeasurement {self.date} {self.weight_lb}lb"


class UserSettings(models.Model):
    """Singleton for user-level settings (FTP, preferences, etc.)."""
    ftp = models.IntegerField(null=True, blank=True, help_text="Functional Threshold Power in watts")
    updated_at = models.DateTimeField(auto_now=True)
    ai_insights = models.TextField(null=True, blank=True)
    ai_insights_generated_at = models.DateTimeField(null=True, blank=True)
    ai_insights_batch_id = models.CharField(max_length=128, null=True, blank=True)
    ai_body_commentary = models.TextField(null=True, blank=True)
    ai_body_commentary_generated_at = models.DateTimeField(null=True, blank=True)
    ai_nutrition_insights = models.TextField(null=True, blank=True)
    ai_nutrition_insights_generated_at = models.DateTimeField(null=True, blank=True)
    ai_nutrition_insights_range = models.IntegerField(null=True, blank=True)
    ai_nutrition_insights_batch_id = models.CharField(max_length=128, null=True, blank=True)
    ai_pattern_insights = models.TextField(null=True, blank=True)
    ai_pattern_insights_generated_at = models.DateTimeField(null=True, blank=True)
    ai_pattern_insights_batch_id = models.CharField(max_length=128, null=True, blank=True)
    last_daily_sync_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"UserSettings (FTP={self.ftp})"


class Intervention(models.Model):
    CATEGORY_CHOICES = [
        ("medication", "Medication"),
        ("supplement", "Supplement"),
        ("hormone", "Hormone"),
        ("lifestyle", "Lifestyle"),
        ("surgery", "Surgery"),
        ("other", "Other"),
    ]
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    expected_effects = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]

    @property
    def is_active(self):
        from datetime import date
        return self.end_date is None or self.end_date > date.today()

    @property
    def duration_days(self):
        from datetime import date
        end = self.end_date or date.today()
        return (end - self.start_date).days

    @property
    def current_dose(self):
        from datetime import date
        from django.db.models import Q
        today = date.today()
        return self.dose_changes.filter(
            start_date__lte=today
        ).filter(
            Q(end_date__gte=today) | Q(end_date__isnull=True)
        ).order_by("-start_date").first()

    @property
    def dose_summary(self):
        doses = list(self.dose_changes.order_by("start_date"))
        if not doses:
            return ""
        if len(doses) == 1:
            return doses[0].dose
        return " → ".join(d.dose for d in doses)

    def dose_at(self, target_date):
        from django.db.models import Q
        return self.dose_changes.filter(
            start_date__lte=target_date
        ).filter(
            Q(end_date__gte=target_date) | Q(end_date__isnull=True)
        ).order_by("-start_date").first()

    def __str__(self):
        return f"{self.name} ({self.start_date})"


class DoseChange(models.Model):
    intervention = models.ForeignKey(
        "Intervention", on_delete=models.CASCADE, related_name="dose_changes"
    )
    dose = models.CharField(max_length=100)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["intervention", "start_date"],
                name="unique_dose_change_per_intervention_date",
            ),
        ]

    @property
    def duration_days(self):
        from datetime import date
        end = self.end_date or date.today()
        return (end - self.start_date).days

    def __str__(self):
        end = self.end_date or "present"
        return f"{self.dose} ({self.start_date} → {end})"


class SavedAnalysis(models.Model):
    label = models.CharField(max_length=200)
    intervention = models.ForeignKey("Intervention", null=True, blank=True, on_delete=models.SET_NULL)
    before_start = models.DateField()
    before_end = models.DateField()
    after_start = models.DateField()
    after_end = models.DateField()
    window_days = models.IntegerField()
    washout_days = models.IntegerField(default=0)
    weight_goal = models.CharField(max_length=20, default="loss")
    metrics_json = models.JSONField()
    ai_interpretation = models.TextField()
    ai_model = models.CharField(max_length=50, default="claude-sonnet-4-6")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.label} ({self.created_at.date()})"


def _parse_hr_zones_from_workout(workout: dict) -> dict:
    """Read HR zone seconds from whichever location Peloton populates them."""
    top = {
        "hr_z1_seconds": workout.get("heart_rate_z1_duration"),
        "hr_z2_seconds": workout.get("heart_rate_z2_duration"),
        "hr_z3_seconds": workout.get("heart_rate_z3_duration"),
        "hr_z4_seconds": workout.get("heart_rate_z4_duration"),
        "hr_z5_seconds": workout.get("heart_rate_z5_duration"),
    }
    if any(v is not None for v in top.values()):
        return top
    nested = ((workout.get("effort_zones") or {}).get("heart_rate_zone_durations") or {})
    return {
        "hr_z1_seconds": nested.get("heart_rate_z1_duration"),
        "hr_z2_seconds": nested.get("heart_rate_z2_duration"),
        "hr_z3_seconds": nested.get("heart_rate_z3_duration"),
        "hr_z4_seconds": nested.get("heart_rate_z4_duration"),
        "hr_z5_seconds": nested.get("heart_rate_z5_duration"),
    }


class CachedWorkout(models.Model):
    """
    Local cache of a Peloton workout. Stores the fields we query/filter on
    directly, plus the raw JSON blob for anything else. Refresh via /api/sync/.
    """

    workout_id = models.CharField(max_length=64, unique=True, db_index=True)
    ride_id = models.CharField(max_length=64, blank=True, db_index=True)

    # ── Run target pace (from /api/workout/:id/performance_graph) ─────────────
    pace_targets_json = models.JSONField(default=list, blank=True)

    # ── Performance Graph ─────────────────────────────────────────────────────
    # Full parsed metrics dict from get_parsed_performance
    performance_graph_json = models.JSONField(null=True, blank=True)

    # Core metadata
    title = models.CharField(max_length=255, blank=True)
    discipline = models.CharField(max_length=64, blank=True, db_index=True)
    fitness_discipline_display = models.CharField(max_length=64, blank=True)
    workout_type = models.CharField(max_length=64, blank=True)

    # Class metadata
    instructor_name = models.CharField(max_length=128, blank=True)
    instructor_image_url = models.URLField(blank=True)
    class_image_url = models.URLField(blank=True)
    duration_seconds = models.IntegerField(null=True)

    # ── Shared metrics ────────────────────────────────────────────────────────
    calories = models.FloatField(null=True)
    heart_rate_avg = models.FloatField(null=True)
    heart_rate_max = models.FloatField(null=True)
    effort_score = models.FloatField(null=True)

    # Heart rate zone durations (seconds spent in each zone)
    hr_z1_seconds = models.IntegerField(null=True)
    hr_z2_seconds = models.IntegerField(null=True)
    hr_z3_seconds = models.IntegerField(null=True)
    hr_z4_seconds = models.IntegerField(null=True)
    hr_z5_seconds = models.IntegerField(null=True)

    # ── Cycling metrics ───────────────────────────────────────────────────────
    output_watts = models.FloatField(null=True)
    avg_watts = models.FloatField(null=True)
    avg_cadence = models.FloatField(null=True)
    avg_resistance = models.FloatField(null=True)
    avg_speed = models.FloatField(null=True)
    distance = models.FloatField(null=True)
    leaderboard_rank = models.IntegerField(null=True)
    total_leaderboard_users = models.IntegerField(null=True)

    # ── Run-specific metrics ──────────────────────────────────────────────────
    # avg_pace stored as total seconds per mile (e.g. 540 = 9:00/mi)
    avg_pace_seconds = models.IntegerField(null=True)
    distance_miles = models.FloatField(null=True)
    avg_speed_mph = models.FloatField(null=True)
    avg_incline = models.FloatField(null=True)
    max_speed_mph = models.FloatField(null=True)
    max_incline = models.FloatField(null=True)
    elevation_gain = models.FloatField(null=True)

    # ── Garmin running form metrics ───────────────────────────────────────────
    # Populated from Garmin activity summary during sync; null for Peloton-only runs.
    # avg_cadence (steps/min) is the Garmin summary value and includes walking.
    # run_cadence_avg filters the time-series to ≥140 spm to exclude walking intervals.
    run_cadence_avg = models.FloatField(null=True, blank=True)         # steps/min, running only
    stride_length_avg = models.FloatField(null=True, blank=True)       # centimeters
    vertical_oscillation_avg = models.FloatField(null=True, blank=True)  # centimeters
    vertical_ratio_avg = models.FloatField(null=True, blank=True)      # percent
    ground_contact_time_avg = models.FloatField(null=True, blank=True)  # milliseconds

    # Matched Garmin activity (for Peloton runs augmented with Garmin form data).
    # Cached so re-augmentation never needs a Garmin API call.
    garmin_activity_id = models.BigIntegerField(null=True, blank=True)
    garmin_activity_start = models.DateTimeField(null=True, blank=True)

    # Google Health exercise data point ID (trailing segment of the resource
    # name), for source="google_health" workouts. Mirrors garmin_activity_id.
    google_health_activity_id = models.CharField(max_length=64, null=True, blank=True)

    # Per-workout HR zone time breakdown from Google Health's
    # metricsSummary.heartRateZoneDurations — Google's own 4-zone model
    # (light/moderate/vigorous/peak), not the Peloton/Garmin 5-zone
    # breakdown computed client-side from performance_graph_json's HR time
    # series. Only populated for Google Health-sourced workouts (no
    # equivalent field exists to fill on Peloton/Garmin rows, which already
    # get a more precise zone breakdown from their own time series).
    hr_zone_light_seconds = models.IntegerField(null=True, blank=True)
    hr_zone_moderate_seconds = models.IntegerField(null=True, blank=True)
    hr_zone_vigorous_seconds = models.IntegerField(null=True, blank=True)
    hr_zone_peak_seconds = models.IntegerField(null=True, blank=True)

    garmin_form_json = models.JSONField(null=True, blank=True)  # raw metrics_by_slug at every_n=1
    # Seconds into the Garmin recording that corresponds to Peloton t=0.
    # Detected via HR cross-correlation in _apply_garmin_form.
    garmin_offset_seconds = models.IntegerField(null=True, blank=True)

    # ── Strength-specific metrics ─────────────────────────────────────────────
    # Strength has no output/cadence — effort score + HR are the main signals
    segment_count = models.IntegerField(null=True)

    # FTP at the time of the workout (set from UserSettings on sync; backfillable)
    ftp = models.IntegerField(null=True, blank=True)

    # ── Run target pace (from /api/workout/:id/performance_graph) ─────────────
    # Pre-computed midpoint target pace series (min/mile, every_n=5s), indexed
    # to match the performance graph values array.  Populated on first detail view.
    pace_targets_json = models.JSONField(default=list, blank=True)

    # ── Detail fields (from /api/workout/:id) ─────────────────────────────────
    # Populated by sync_workout_details; null until that sync runs.
    average_effort_score = models.FloatField(null=True)
    is_pr = models.BooleanField(default=False)
    class_description = models.TextField(blank=True)
    difficulty_estimate = models.FloatField(null=True)
    strava_id = models.CharField(max_length=64, blank=True)
    leaderboard_distance_rank = models.IntegerField(null=True)
    total_leaderboard_distance_users = models.IntegerField(null=True)
    achievements = models.JSONField(default=list)
    movements = models.JSONField(default=list)
    movement_summary = models.JSONField(default=dict)

    # Class-level exercise plan from /api/ride/{id}/details (segment → exercises
    # the instructor programmed), for classes where Peloton's Movement Tracker
    # doesn't record anything. Same for everyone who takes the class — see
    # peloton_client.parse_class_plan for the shape.
    class_plan_json = models.JSONField(default=list, blank=True)
    # Hand-entered sets/reps/weight per exercise: [{"name", "sets", "reps",
    # "weight_lb", "notes"}]. Deliberately NOT in DETAIL_FIELDS/apply_detail —
    # detail syncs overwrite `movements` wholesale, and this must survive them.
    manual_movements_json = models.JSONField(default=list, blank=True)
    movement_tracker_tier = models.CharField(max_length=32, blank=True)
    detail_synced_at = models.DateTimeField(null=True, blank=True)

    # Timestamps
    created_at = models.DateTimeField(db_index=True)
    synced_at = models.DateTimeField(auto_now=True)
    leaderboard_synced_at = models.DateTimeField(null=True, blank=True)

    source = models.CharField(max_length=20, default="peloton", db_index=True)
    exercise_sets_json = models.JSONField(default=list)
    raw_data = models.JSONField(default=dict)

    # Fields updated by apply_detail — used in save(update_fields=...)
    DETAIL_FIELDS = [
        "average_effort_score", "is_pr", "class_description", "difficulty_estimate",
        "strava_id", "leaderboard_rank", "total_leaderboard_users",
        "leaderboard_distance_rank", "total_leaderboard_distance_users",
        "achievements", "movements", "movement_summary", "movement_tracker_tier",
        "class_plan_json",
        "hr_z1_seconds", "hr_z2_seconds", "hr_z3_seconds", "hr_z4_seconds", "hr_z5_seconds",
        "detail_synced_at",
    ]

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.created_at:%Y-%m-%d})"

    @property
    def duration_minutes(self):
        return self.duration_seconds // 60 if self.duration_seconds else None

    @property
    def effort_points(self):
        """Total effort points from performance graph — matches what workout detail pages display."""
        pg = self.performance_graph_json or {}
        ez = pg.get("effort_zones") or {}
        return ez.get("total_effort_points")

    @property
    def heart_rate_avg_best(self):
        """HR average: model field if set, otherwise from performance graph."""
        if self.heart_rate_avg:
            return self.heart_rate_avg
        pg = self.performance_graph_json or {}
        m = (pg.get("metrics_by_slug") or {}).get("heart_rate") or {}
        return m.get("average_value")

    @property
    def leaderboard_pct(self):
        if self.leaderboard_rank and self.total_leaderboard_users:
            return round((1 - self.leaderboard_rank / self.total_leaderboard_users) * 100, 1)
        return None

    @property
    def avg_pace_display(self):
        """Format avg_pace_seconds as MM:SS/mi."""
        if not self.avg_pace_seconds:
            return None
        minutes, seconds = divmod(int(self.avg_pace_seconds), 60)
        return f"{minutes}:{seconds:02d}/mi"

    @property
    def is_run(self):
        return self.discipline == "running"

    @property
    def is_strength(self):
        return self.discipline == "strength"

    @property
    def is_cycling(self):
        return self.discipline in ("cycling", "bike_bootcamp")

    @property
    def is_walking(self):
        return self.discipline == "walking"

    @property
    def external_url(self):
        """Link to this workout on Peloton.com or Garmin Connect. None if no public link exists."""
        if self.source == "garmin":
            return f"https://connect.garmin.com/modern/activity/{self.workout_id.removeprefix('garmin_')}"
        if self.source == "google_health":
            return None
        return f"https://members.onepeloton.com/profile/workouts/{self.workout_id}"

    def apply_detail(self, detail: dict) -> None:
        """Apply parsed workout detail data (from get_parsed_workout_detail) to model fields."""
        from django.utils import timezone
        self.average_effort_score = detail.get("average_effort_score")
        self.is_pr = detail.get("is_pr", False)
        self.class_description = detail.get("class_description", "")
        self.difficulty_estimate = detail.get("difficulty_estimate")
        self.strava_id = detail.get("strava_id") or ""
        self.leaderboard_rank = detail.get("leaderboard_rank")
        self.total_leaderboard_users = detail.get("total_leaderboard_users")
        self.leaderboard_distance_rank = detail.get("leaderboard_distance_rank")
        self.total_leaderboard_distance_users = detail.get("total_leaderboard_distance_users")
        self.achievements = detail.get("achievements", [])
        self.movements = detail.get("movements", [])
        self.movement_summary = detail.get("movement_summary", {})
        self.movement_tracker_tier = detail.get("movement_tracker_tier") or ""
        # Only overwrite when the caller actually fetched a plan — most disciplines
        # never do, and an empty default must not clobber a stored one.
        if detail.get("class_plan"):
            self.class_plan_json = detail["class_plan"]
        # HR zone durations are more reliably populated from the detail endpoint
        hr_zones = detail.get("hr_zones") or {}
        if hr_zones:
            self.hr_z1_seconds = hr_zones.get("z1")
            self.hr_z2_seconds = hr_zones.get("z2")
            self.hr_z3_seconds = hr_zones.get("z3")
            self.hr_z4_seconds = hr_zones.get("z4")
            self.hr_z5_seconds = hr_zones.get("z5")
        self.detail_synced_at = timezone.now()

    @classmethod
    def from_api(cls, workout: dict) -> "CachedWorkout":
        """
        Build (but don't save) a CachedWorkout from the API payload returned
        by GET /api/user/{userId}/workouts with joins=peloton.ride,peloton.ride.instructor
        """
        from datetime import datetime, timezone

        peloton = workout.get("peloton") or {}
        ride = peloton.get("ride") or {}
        instructor = ride.get("instructor") or {}

        created_ts = workout.get("start_time") or workout.get("created_at")
        created_at = (
            datetime.fromtimestamp(created_ts, tz=timezone.utc) if created_ts else None
        )

        discipline = workout.get("fitness_discipline", "")

        obj = cls(
            workout_id=workout["id"],
            ride_id=ride.get("id", ""),
            title=ride.get("title", workout.get("name", "")),
            discipline=discipline,
            fitness_discipline_display=workout.get("fitness_discipline_display_name", ""),
            workout_type=workout.get("workout_type", ""),
            instructor_name=instructor.get("name", ""),
            instructor_image_url=instructor.get("image_url", ""),
            class_image_url=ride.get("image_url", ""),
            duration_seconds=ride.get("duration"),
            # Shared
            calories=workout.get("calories"),
            heart_rate_avg=workout.get("avg_heart_rate"),
            heart_rate_max=workout.get("max_heart_rate"),
            effort_score=workout.get("effort_score"),
            # HR zones — cycling/running at top level; yoga/other in effort_zones.heart_rate_zone_durations
            **{k: v for k, v in _parse_hr_zones_from_workout(workout).items()},
            # Cycling
            output_watts=workout.get("total_work"),
            avg_watts=workout.get("avg_power"),
            avg_cadence=workout.get("avg_cadence"),
            avg_resistance=workout.get("avg_resistance"),
            avg_speed=workout.get("avg_speed"),
            distance=workout.get("distance"),
            leaderboard_rank=workout.get("leaderboard_rank"),
            total_leaderboard_users=workout.get("total_leaderboard_users"),
            # Run
            avg_pace_seconds=workout.get("avg_pace"),
            distance_miles=workout.get("distance"),
            avg_speed_mph=workout.get("avg_speed"),
            avg_incline=workout.get("avg_incline"),
            max_speed_mph=workout.get("max_speed"),
            max_incline=workout.get("max_incline"),
            elevation_gain=workout.get("elevation_gain"),
            created_at=created_at,
            raw_data=workout,
        )
        return obj

    @classmethod
    def from_garmin(cls, parsed: dict) -> "CachedWorkout":
        """Build (but don't save) a CachedWorkout from GarminClient.parse_activity() output."""
        return cls(
            workout_id=parsed["workout_id"],
            ride_id="",
            title=parsed.get("title", ""),
            discipline=parsed.get("discipline", ""),
            fitness_discipline_display=parsed.get("discipline", "").replace("_", " ").title(),
            workout_type=parsed.get("workout_type", ""),
            instructor_name="",
            instructor_image_url="",
            class_image_url="",
            duration_seconds=parsed.get("duration_seconds") or 0,
            calories=parsed.get("calories"),
            heart_rate_avg=parsed.get("heart_rate_avg"),
            heart_rate_max=parsed.get("heart_rate_max"),
            distance_miles=parsed.get("distance_miles"),
            distance=parsed.get("distance"),
            avg_speed_mph=parsed.get("avg_speed_mph"),
            avg_speed=parsed.get("avg_speed"),
            avg_pace_seconds=parsed.get("avg_pace_seconds"),
            avg_watts=parsed.get("avg_watts"),
            avg_cadence=parsed.get("avg_cadence"),
            elevation_gain=parsed.get("elevation_gain"),
            stride_length_avg=parsed.get("stride_length_avg"),
            vertical_oscillation_avg=parsed.get("vertical_oscillation_avg"),
            vertical_ratio_avg=parsed.get("vertical_ratio_avg"),
            ground_contact_time_avg=parsed.get("ground_contact_time_avg"),
            created_at=parsed.get("created_at"),
            source="garmin",
            raw_data=parsed.get("raw_data", {}),
        )


# ---------------------------------------------------------------------------
# Nutrition
# ---------------------------------------------------------------------------

class NutritionProfile(models.Model):
    """Singleton — stores inputs for the macro target calculator."""
    height_cm = models.FloatField(null=True, blank=True)
    age = models.IntegerField(null=True, blank=True)
    biological_sex = models.CharField(max_length=10, default="female")
    activity_level = models.CharField(max_length=20, default="active")
    goal = models.CharField(max_length=20, default="loss")
    deficit_pct = models.FloatField(default=20.0)
    protein_g_per_kg_lean = models.FloatField(default=2.2)
    manual_calories = models.IntegerField(null=True, blank=True)
    manual_protein_g = models.IntegerField(null=True, blank=True)
    manual_carbs_g = models.IntegerField(null=True, blank=True)
    manual_fat_g = models.IntegerField(null=True, blank=True)
    manual_fiber_g = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"NutritionProfile (goal={self.goal}, activity={self.activity_level})"


class AthleteProfile(models.Model):
    """Singleton (pk=1). Drives persona/coaching context for AI prompts."""
    EXPERIENCE_CHOICES = [
        ("new",          "New to this discipline"),
        ("intermediate", "Intermediate"),
        ("experienced",  "Experienced"),
    ]
    TONE_CHOICES = [
        ("encouraging", "Encouraging"),
        ("direct",      "Direct"),
        ("data_only",   "Data-only, no commentary"),
    ]

    running_experience  = models.CharField(max_length=16, choices=EXPERIENCE_CHOICES, blank=True, default="")
    cycling_experience  = models.CharField(max_length=16, choices=EXPERIENCE_CHOICES, blank=True, default="")
    strength_experience = models.CharField(max_length=16, choices=EXPERIENCE_CHOICES, blank=True, default="")

    primary_disciplines = models.JSONField(default=list, blank=True,
        help_text="List of disciplines the user identifies with. If empty, derived from workout history.")
    training_focus = models.TextField(blank=True,
        help_text="Free-form, e.g. 'building running base', 'training for half marathon', 'recomp'.")
    coaching_tone = models.CharField(max_length=16, choices=TONE_CHOICES, blank=True, default="encouraging")

    rehab_keywords = models.JSONField(default=list, blank=True,
        help_text="Substring matches that flag a workout title as PT/rehab and exclude it from "
                  "training-load reasoning. Example: ['shoulder pt', 'physical therapy', 'rehab', 'prehab'].")

    health_context_override = models.TextField(blank=True,
        help_text="Free-form context to inject verbatim into health/nutrition prompts. "
                  "Leave blank to derive from Interventions only.")

    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"AthleteProfile (tone={self.coaching_tone})"


class FoodEntry(models.Model):
    """One logged food/meal event."""
    MEAL_CHOICES = [
        ("breakfast", "Breakfast"), ("lunch", "Lunch"),
        ("dinner", "Dinner"), ("snack", "Snack"),
    ]
    date = models.DateField(db_index=True)
    logged_at = models.DateTimeField(auto_now_add=True)
    meal = models.CharField(max_length=20, choices=MEAL_CHOICES, blank=True)
    raw_text = models.TextField()
    items_json = models.JSONField(default=list)
    calories = models.FloatField(default=0)
    protein_g = models.FloatField(default=0)
    carbs_g = models.FloatField(default=0)
    fat_g = models.FloatField(default=0)
    fiber_g = models.FloatField(default=0)
    ai_model = models.CharField(max_length=50, blank=True)
    ai_confidence = models.CharField(max_length=10, blank=True)
    edited_by_user = models.BooleanField(default=False)
    is_favorite = models.BooleanField(default=False)
    source_saved_meal = models.ForeignKey(
        "SavedMeal", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="logged_entries",
    )
    google_health_nutrition_log_name = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["logged_at"]

    def __str__(self):
        return f"{self.date} {self.get_meal_display() or 'Log'}: {self.raw_text[:60]}"


class SavedMeal(models.Model):
    """Saved meal for one-click re-logging."""
    name = models.CharField(max_length=200)
    meal = models.CharField(max_length=20, blank=True)
    items_json = models.JSONField(default=list)
    calories = models.FloatField(default=0)
    protein_g = models.FloatField(default=0)
    carbs_g = models.FloatField(default=0)
    fat_g = models.FloatField(default=0)
    fiber_g = models.FloatField(default=0)
    times_logged = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-times_logged", "name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Hunger & Satiety
# ---------------------------------------------------------------------------

class HungerCheck(models.Model):
    CONTEXT_CHOICES = [
        ("morning",   "Morning (waking)"),
        ("pre_meal",  "Before a meal"),
        ("post_meal", "After a meal"),
        ("evening",   "Evening"),
        ("random",    "Random check"),
    ]
    timestamp     = models.DateTimeField(auto_now_add=True, db_index=True)
    date          = models.DateField(db_index=True)
    context       = models.CharField(max_length=20, choices=CONTEXT_CHOICES)
    hunger_level  = models.IntegerField()           # 1–10
    fullness_level = models.IntegerField(null=True, blank=True)  # 1–10, post_meal only
    related_meal  = models.ForeignKey(
        "FoodEntry", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="hunger_checks",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.date} {self.get_context_display()} hunger={self.hunger_level}"


# ---------------------------------------------------------------------------
# Side Effect Log
# ---------------------------------------------------------------------------

class SideEffectLog(models.Model):
    SEVERITY_CHOICES = [(1, "Mild"), (2, "Moderate"), (3, "Severe")]
    SYMPTOM_CHOICES = [
        ("nausea",          "Nausea"),
        ("bloating",        "Bloating"),
        ("constipation",    "Constipation"),
        ("diarrhea",        "Diarrhea"),
        ("reflux",          "Acid reflux"),
        ("fatigue",         "Fatigue"),
        ("headache",        "Headache"),
        ("injection_site",  "Injection site reaction"),
        ("dizziness",       "Dizziness"),
        ("dry_mouth",       "Dry mouth"),
        ("other",           "Other"),
    ]
    timestamp            = models.DateTimeField(auto_now_add=True)
    date                 = models.DateField(db_index=True)
    symptom              = models.CharField(max_length=30, choices=SYMPTOM_CHOICES)
    other_label          = models.CharField(max_length=100, blank=True, default="")
    severity             = models.IntegerField(choices=SEVERITY_CHOICES)
    related_meal         = models.ForeignKey(
        "FoodEntry", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="side_effects",
    )
    related_intervention = models.ForeignKey(
        "Intervention", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="side_effects",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]

    @property
    def display_name(self):
        if self.symptom == "other" and self.other_label:
            return self.other_label
        return self.get_symptom_display()

    def __str__(self):
        return f"{self.date} {self.display_name} ({self.get_severity_display()})"


# ---------------------------------------------------------------------------
# Target Adjustment History
# ---------------------------------------------------------------------------

class TargetAdjustment(models.Model):
    timestamp         = models.DateTimeField(auto_now_add=True)
    previous_calories = models.IntegerField()
    new_calories      = models.IntegerField()
    reason            = models.TextField()
    auto_suggested    = models.BooleanField(default=True)
    accepted_by_user  = models.BooleanField(default=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d} {self.previous_calories} → {self.new_calories}"


class WeeklyReview(models.Model):
    """AI-generated weekly review covering weight, nutrition, workouts, and habits."""
    week_start    = models.DateField(unique=True)
    content       = models.TextField(blank=True, default="")
    generated_at  = models.DateTimeField(auto_now_add=True)
    ai_model      = models.CharField(max_length=64, default="claude-sonnet-4-6")
    batch_id      = models.CharField(max_length=128, null=True, blank=True)

    class Meta:
        ordering = ["-week_start"]

    def __str__(self):
        return f"Weekly Review {self.week_start}"

    @property
    def week_end(self):
        import datetime
        return self.week_start + datetime.timedelta(days=6)


class WithingsAuth(models.Model):
    """
    Singleton (pk=1). Stores Withings OAuth credentials in Postgres so both
    laptop and hosted app can sync from the same source of truth.
    Replaces ~/.fitpulse/withings_tokens.json.
    """
    userid = models.CharField(max_length=64)
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()

    # Webhook observability
    last_subscribed_at = models.DateTimeField(null=True, blank=True)
    last_webhook_received_at = models.DateTimeField(null=True, blank=True)
    webhook_subscription_active = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Withings Auth"
        verbose_name_plural = "Withings Auth"

    def __str__(self):
        return f"WithingsAuth(userid={self.userid}, expires={self.token_expires_at})"

    @classmethod
    def get(cls):
        """Returns the singleton row, or None if not yet seeded."""
        return cls.objects.filter(pk=1).first()


class PelotonAuth(models.Model):
    """
    Singleton (pk=1). Stores Peloton session credentials in Postgres so both
    laptop and hosted app can sync. Peloton has no OAuth — the session cookie
    is extracted manually from browser DevTools and pasted into /settings/integrations/.
    Cookies last weeks to months; rotate when sync starts returning 403.
    """
    session_id = models.CharField(max_length=512, help_text="peloton_session_id cookie")
    user_id = models.CharField(max_length=64, help_text="Peloton user ID")
    last_updated = models.DateTimeField(auto_now=True)
    notes = models.CharField(
        max_length=500,
        blank=True,
        help_text="Optional — e.g. 'extracted from Chrome 2026-06-26'",
    )

    class Meta:
        verbose_name = "Peloton Auth"
        verbose_name_plural = "Peloton Auth"

    def __str__(self):
        return f"PelotonAuth(user_id={self.user_id}, updated={self.last_updated})"

    @classmethod
    def get(cls):
        return cls.objects.filter(pk=1).first()

    @property
    def masked_session_id(self):
        if not self.session_id or len(self.session_id) < 8:
            return "(empty)"
        return f"…{self.session_id[-4:]}"


class GoogleHealthAuth(models.Model):
    """
    Singleton (pk=1). Stores Google Health API OAuth2 credentials in Postgres,
    same pattern as WithingsAuth. Populated by the `google_health_login`
    management command, or by the web reconnect flow at
    /auth/google-health/connect/ (see google_health_oauth_connect/
    google_health_oauth_callback in views.py).
    """
    access_token = models.TextField()
    refresh_token = models.TextField()
    token_expires_at = models.DateTimeField()
    scopes = models.TextField(blank=True, help_text="Space-separated granted scopes")

    # Deliberately NOT auto_now_add — that would freeze this at whenever the
    # row was first created and never move again, which defeats the point:
    # this exists to show "how close to the 7-day refresh-token expiry are
    # we" on the Integrations page, so it must advance every time the user
    # actually reconnects (GoogleHealthClient._save_tokens(mark_reconnected=True),
    # called from exchange_code — NOT from routine refresh_tokens() calls).
    connected_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Google Health Auth"
        verbose_name_plural = "Google Health Auth"

    def __str__(self):
        return f"GoogleHealthAuth(expires={self.token_expires_at})"

    @classmethod
    def get(cls):
        """Returns the singleton row, or None if not yet seeded."""
        return cls.objects.filter(pk=1).first()

    @property
    def days_since_connected(self):
        return (timezone.now() - self.connected_at).days


class Integration(models.Model):
    """
    Tracks each data-source integration (Peloton/Garmin/Withings/Google Health)
    so the UI can toggle them on/off and show connection status, without
    touching the sync logic for the ones left alone.
    """
    KEY_CHOICES = [
        ("peloton", "Peloton"),
        ("garmin", "Garmin"),
        ("withings", "Withings"),
        ("google_health", "Google Health"),
    ]
    key = models.CharField(max_length=32, unique=True, choices=KEY_CHOICES)
    display_name = models.CharField(max_length=64)
    is_enabled = models.BooleanField(default=True)
    is_authenticated = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    SYNC_ALL_URL_NAMES = {
        "peloton": "sync_all_workouts",
        "garmin": "sync_garmin_all",
        "withings": "sync_withings_all",
        "google_health": "sync_google_health_all",
    }

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"Integration({self.key}, enabled={self.is_enabled})"

    @property
    def sync_all_url_name(self):
        """URL name for this source's full-backfill sync — used on the
        Integrations page, which is where 'Sync All' lives now (it's a slow
        maintenance action, not something to fire from the nav regularly)."""
        return self.SYNC_ALL_URL_NAMES.get(self.key)


class WebhookError(models.Model):
    """
    A failed webhook-triggered background sync (currently: Google Health
    notification processing — see _process_google_health_notification in
    sync.py). Webhooks respond before processing happens, so a failure here
    has no other visible trace — the sender already got its 200/204 and
    won't retry. This table exists purely so a failure is visible somewhere
    instead of only in server logs. Shown newest-first at
    /settings/integrations/errors/, linked from the Integrations page.

    Self-pruning: RETENTION_DAYS old rows are deleted both when a new error
    is recorded and when the errors page is viewed, so this never needs a
    separate scheduled cleanup job.
    """
    RETENTION_DAYS = 14

    source = models.CharField(max_length=32)  # e.g. "google_health"
    summary = models.CharField(max_length=255)
    detail = models.TextField(blank=True)  # traceback, if available
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"WebhookError({self.source}, {self.created_at:%Y-%m-%d %H:%M})"

    @classmethod
    def record(cls, source: str, summary: str, detail: str = "") -> None:
        """Log a failure and prune anything past the retention window in
        the same call — the self-cleaning half of this model's contract."""
        cls.objects.create(source=source, summary=summary, detail=detail)
        cls.prune()

    @classmethod
    def prune(cls) -> None:
        from datetime import timedelta
        from django.utils import timezone as _tz
        cutoff = _tz.now() - timedelta(days=cls.RETENTION_DAYS)
        cls.objects.filter(created_at__lt=cutoff).delete()


class Program(models.Model):
    """Durable definition of a plan / collection / split. Created once, reused across runs."""
    KIND_CHOICES = [("plan", "Plan"), ("collection", "Collection"), ("split", "Split")]
    MATCH_CHOICES = [
        ("achievement", "Achievement name"),
        ("description", "Description/title suffix"),
        ("ride_ids", "Ride ID list"),
    ]
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="plan")
    instructor = models.CharField(max_length=120, blank=True)
    match_strategy = models.CharField(max_length=20, choices=MATCH_CHOICES, default="ride_ids")
    # Matched against CachedWorkout.achievements[].name (e.g. "HiLit Training Plan").
    # Peloton's list-sync payload carries no achievement_templates/slug; only the
    # detail-synced `achievements` field (name/description/count) is available.
    achievement_name = models.CharField(max_length=150, blank=True)
    # named group regex over CachedWorkout.title; must expose 'week' and 'day'.
    # Real titles are suffix-style: "10 min Mobility: HiLit W1 D1".
    title_week_day_regex = models.CharField(
        max_length=200, blank=True,
        default=r"W(?:eek)?\s*(?P<week>\d+)[,\s]*D(?:ay)?\s*(?P<day>\d+)")
    series_id_hint = models.CharField(max_length=64, blank=True)      # discovery aid for splits
    description = models.TextField(blank=True)
    # When on, cool-down walks / stretches taken right after a completion are
    # attached to it as ProgramRecovery rows (see programs.attach_recoveries).
    track_recovery = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def active_run(self):
        return self.runs.filter(end_date__isnull=True).order_by("-start_date").first()


class ProgramWeek(models.Model):
    """A canonical week in the definition. A split has exactly one."""
    program = models.ForeignKey(Program, related_name="weeks", on_delete=models.CASCADE)
    number = models.PositiveIntegerField()
    label = models.CharField(max_length=120, blank=True)   # "Base", "Peak"

    class Meta:
        unique_together = [("program", "number")]
        ordering = ["program", "number"]

    def __str__(self):
        return f"{self.program.slug} W{self.number}"


class ProgramSlot(models.Model):
    """A planned class within a canonical week."""
    week = models.ForeignKey(ProgramWeek, related_name="slots", on_delete=models.CASCADE)
    day = models.PositiveIntegerField(null=True, blank=True)     # 1=Mon
    order = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=200)                    # base title, no "HiLit W_ D_" suffix
    discipline = models.CharField(max_length=40, blank=True)
    duration_min = models.PositiveIntegerField(null=True, blank=True)
    peloton_ride_id = models.CharField(max_length=64, blank=True)   # split matcher / exact pin
    # Peloton sometimes re-shoots a class under a new ride id while keeping the same
    # title (e.g. a re-recorded "45 min Upper Body: Pull"). Older completions land
    # under these ids but still belong to this slot.
    alt_ride_ids = models.JSONField(default=list, blank=True)
    optional = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    # "Any class of this kind" slots (e.g. Pilates/Yoga off-days): matched by the
    # workout's discipline, and optionally a title keyword, instead of a ride id.
    # Only workouts on/after the active run's start date qualify.
    match_discipline = models.CharField(max_length=40, blank=True)
    match_title_keyword = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["week", "day", "order"]

    def __str__(self):
        return f"{self.week} · {self.title}"


class ProgramRun(models.Model):
    """One engagement period. end_date NULL == the current tracker."""
    program = models.ForeignKey(Program, related_name="runs", on_delete=models.CASCADE)
    label = models.CharField(max_length=120, blank=True)     # "Cycle 1 — Spring"
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Sonnet retrospective, generated once a run ends (or previewed mid-cycle);
    # cached like WeeklyReview — one per run, regenerated on demand.
    retrospective = models.TextField(blank=True)
    retrospective_generated_at = models.DateTimeField(null=True, blank=True)
    retrospective_model = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        state = "current" if self.end_date is None else "ended"
        return f"{self.program.slug} · {self.label or self.start_date} ({state})"

    @property
    def is_current(self):
        return self.end_date is None

    @property
    def display_name(self):
        """label if one was explicitly set, else the date range itself — so a
        cycle's name never goes stale independently of its actual dates."""
        if self.label:
            return self.label
        start = self.start_date.strftime("%b %-d, %Y") if self.start_date else "?"
        end = self.end_date.strftime("%b %-d, %Y") if self.end_date else "ongoing"
        return f"{start} – {end}"


class RunWeek(models.Model):
    """One pass through a canonical week inside a run. Repeats == more of these."""
    run = models.ForeignKey(ProgramRun, related_name="run_weeks", on_delete=models.CASCADE)
    program_week = models.ForeignKey(ProgramWeek, on_delete=models.CASCADE)
    sequence = models.PositiveIntegerField()   # 1,2,3… position in the run

    # Subjective texture for this pass — one rating per row, not per session (Peloton
    # doesn't expose session-level RPE, and weekly is the review app's natural grain).
    rpe = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(10)])
    note = models.TextField(blank=True)
    rated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("run", "sequence")]
        ordering = ["run", "sequence"]

    def __str__(self):
        return f"{self.run} · seq{self.sequence} (W{self.program_week.number})"


class ProgramRecovery(models.Model):
    """A recovery session (cool-down walk / stretch) taken right after a program
    completion. Hangs off the completion's ProgramWorkout so it lives and dies
    with its pass; a workout can be a recovery for at most one completion."""
    KIND_CHOICES = [("walk", "Cool-down walk"), ("stretch", "Stretch")]
    entry = models.ForeignKey("ProgramWorkout", related_name="recoveries", on_delete=models.CASCADE)
    workout = models.OneToOneField("CachedWorkout", related_name="program_recovery", on_delete=models.CASCADE)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    order = models.PositiveSmallIntegerField(default=0)     # position in the chain after the main workout
    matched_by = models.CharField(max_length=20, default="auto")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["entry", "order"]

    def __str__(self):
        return f"{self.get_kind_display()} after {self.entry_id}"

    @property
    def duration_minutes(self):
        return round((self.workout.duration_seconds or 0) / 60)


class ProgramWorkout(models.Model):
    """A completion pinned to a specific pass + slot."""
    run_week = models.ForeignKey(RunWeek, related_name="entries", on_delete=models.CASCADE)
    slot = models.ForeignKey(ProgramSlot, null=True, blank=True, on_delete=models.SET_NULL)
    workout = models.ForeignKey("CachedWorkout", on_delete=models.CASCADE)
    matched_by = models.CharField(max_length=20, default="auto")   # achievement/description/ride_id/manual
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("run_week", "workout")]
        ordering = ["run_week", "slot"]

    def __str__(self):
        return f"{self.run_week} · {self.workout_id}"
