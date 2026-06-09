"""
Compare wellness and body composition metrics before vs. after an intervention date.

Usage:
    venv/bin/python3 manage.py analyze_intervention \
        --start 2026-04-03 \
        --label "Tirzepatide 2.5mg" \
        --window 28 \
        --weight-goal loss
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand

from workouts.analysis import run_intervention_analysis, SECONDS_FIELDS


def _fmt(val, decimals=1):
    if val is None:
        return "—"
    return f"{val:.{decimals}f}"


def _delta_arrow(before_mean, after_mean, direction):
    if before_mean is None or after_mean is None:
        return "  ?"
    diff = after_mean - before_mean
    if abs(diff) < 0.01:
        return "  →"
    if direction == "neutral":
        return f"  {'↑' if diff > 0 else '↓'}"
    improved = (direction == "lower" and diff < 0) or (direction == "higher" and diff > 0)
    return f"  {'✓' if improved else '✗'} {'↓' if diff < 0 else '↑'}"


def _pct_str(pct):
    if pct is None:
        return ""
    sign = "+" if pct > 0 else ""
    return f" ({sign}{pct:.1f}%)"


class Command(BaseCommand):
    help = "Compare wellness metrics before vs. after an intervention start date."

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="Intervention start date (YYYY-MM-DD)")
        parser.add_argument("--label", default="Intervention", help="Name/label for the intervention")
        parser.add_argument("--window", type=int, default=28, help="Days to compare before and after (default 28)")
        parser.add_argument(
            "--weight-goal",
            choices=["loss", "gain", "maintain"],
            default="loss",
            help="Weight goal direction: loss (lower=better), gain (higher=better), maintain (neutral)",
        )

    def handle(self, *args, **options):
        try:
            start = date.fromisoformat(options["start"])
        except ValueError:
            self.stderr.write("Invalid --start date. Use YYYY-MM-DD.")
            return

        window = options["window"]
        label  = options["label"]
        weight_goal = options["weight_goal"]

        before_start = start - timedelta(days=window)
        before_end   = start - timedelta(days=1)
        after_start  = start
        after_end    = start + timedelta(days=window - 1)
        today = date.today()
        days_elapsed = (today - start).days

        result = run_intervention_analysis(
            before_start=before_start,
            before_end=before_end,
            after_start=after_start,
            after_end=after_end,
            weight_goal=weight_goal,
        )

        self.stdout.write("\n" + "═" * 70)
        self.stdout.write(f"  INTERVENTION ANALYSIS: {label}")
        self.stdout.write(f"  Start date : {start}  (day {days_elapsed + 1} of intervention)")
        self.stdout.write(f"  Before     : {result['before_start']} → {result['before_end']}  ({result['before_n']} days with data)")
        self.stdout.write(f"  After      : {result['after_start']} → {result['after_end']}  ({result['after_n']} days with data)")
        self.stdout.write(f"  Weight goal: {weight_goal}")
        self.stdout.write("═" * 70 + "\n")

        col_w = 22

        for group in result["groups"]:
            # Check if any metric has data
            has_data = any(
                m["before_n"] > 0 or m["after_n"] > 0
                for m in group["metrics"]
            )
            if not has_data:
                continue

            gname = group["name"]
            self.stdout.write(f"── {gname} " + "─" * (66 - len(gname)))
            self.stdout.write(
                f"  {'Metric':<{col_w}}  {'Before':>10}  {'After':>10}  {'Δ':>14}  {'N':>6}"
            )
            self.stdout.write("  " + "-" * 64)

            for m in group["metrics"]:
                display_name = m["display_name"]
                unit         = m["unit"]
                direction    = m["direction"]
                decimals     = 1 if unit in ("ms", "lb", "%", "br/min") else 0

                bm = m["before_mean"]
                am = m["after_mean"]
                bn = m["before_n"]
                an = m["after_n"]
                pct = m["pct_change"]

                arrow  = _delta_arrow(bm, am, direction)
                change = _pct_str(pct)

                unit_str = f" {unit}" if unit else ""
                b_str = f"{_fmt(bm, decimals)}{unit_str}" if bm is not None else "—"
                a_str = f"{_fmt(am, decimals)}{unit_str}" if am is not None else "—"

                self.stdout.write(
                    f"  {display_name:<{col_w}}  {b_str:>10}  {a_str:>10}  {arrow}{change:<12}  {bn}/{an:>2}"
                )

            self.stdout.write("")

        self.stdout.write("═" * 70)
        self.stdout.write("  ✓ = improved  ✗ = worsened  → = no change  ? = no data")
        self.stdout.write("  N = before_days / after_days with data for that metric")
        self.stdout.write("═" * 70 + "\n")
