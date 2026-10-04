from django import template
from django.db.models import Max

register = template.Library()


def _request_user(context):
    request = context.get("request")
    return getattr(request, "user", None)


@register.simple_tag(takes_context=True)
def last_synced(context):
    from workouts.models import CachedWorkout
    result = CachedWorkout.objects.for_user(_request_user(context)).aggregate(Max('synced_at'))
    return result.get('synced_at__max')


@register.simple_tag(takes_context=True)
def last_daily_sync(context):
    from workouts.models import UserSettings
    user = _request_user(context)
    if not user or not user.is_authenticated:
        return None
    return UserSettings.for_user(user).last_daily_sync_at


@register.simple_tag(takes_context=True)
def enabled_integrations(context):
    """Set of Integration.key values currently enabled — used to gate the nav
    Sync dropdown so a disabled integration's buttons don't render at all."""
    from workouts.models import Integration
    qs = Integration.objects.for_user(_request_user(context)).filter(is_enabled=True)
    return set(qs.values_list('key', flat=True))


@register.filter
def split(value, delimiter=","):
    return value.split(delimiter)


@register.filter
def index(lst, i):
    try:
        return lst[int(i)]
    except (IndexError, TypeError, ValueError):
        return ""


@register.filter
def to_range(n):
    return range(1, int(n) + 1)


@register.filter
def format_watts(value):
    if value is None:
        return "—"
    return f"{value/1000:,.0f} kJ"


@register.filter
def format_kj(value):
    """Like format_watts but returns just the number, no unit."""
    if value is None:
        return "—"
    return f"{value/1000:,.0f}"


@register.filter
def format_duration(seconds):
    """Convert seconds to M:SS or Xm Xs."""
    if not seconds:
        return "—"
    seconds = int(seconds)
    minutes = seconds // 60
    secs = seconds % 60
    if minutes == 0:
        return f"{secs}s"
    if secs == 0:
        return f"{minutes}m"
    return f"{minutes}m {secs}s"


@register.filter
def format_pace(seconds):
    """Format pace in seconds/mile as MM:SS/mi."""
    if not seconds:
        return "—"
    try:
        seconds = int(seconds)
        minutes, secs = divmod(seconds, 60)
        return f"{minutes}:{secs:02d}/mi"
    except (TypeError, ValueError):
        return "—"


@register.filter
def pct(value, total):
    """Return value as a percentage of total, capped at 100."""
    try:
        result = min((float(value) / float(total)) * 100, 100)
        return round(result, 1)
    except (TypeError, ZeroDivisionError):
        return 0


@register.filter
def floatformat_default(value, arg="0"):
    """floatformat with a fallback of — for None."""
    if value is None:
        return "—"
    try:
        decimals = int(arg)
        return f"{value:.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


@register.filter
def divide(value, arg):
    try:
        return round(float(value) / float(arg), 1)
    except (TypeError, ZeroDivisionError):
        return None


@register.filter
def get_item(obj, key):
    """Access a dict value by variable key in templates."""
    if isinstance(obj, dict):
        return obj.get(key)
    return None


@register.filter
def format_speed(value):
    """Format speed in mph to one decimal place."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "—"


@register.filter
def prettify_slug(value):
    """Convert snake_case slugs to Title Case display strings."""
    if not value:
        return ""
    return value.replace("_", " ").title()


@register.filter
def top_pct(rank, total):
    """Percentage of the field you beat: rank 1/100 → 99, rank 74/100 → 26."""
    try:
        return round((1 - float(rank) / float(total)) * 100, 1)
    except (TypeError, ZeroDivisionError):
        return None


@register.filter
def difficulty_pips(value, total=10):
    """Return a list of booleans for difficulty pip rendering (filled = True)."""
    try:
        filled = round(float(value))
        total = int(total)
    except (TypeError, ValueError):
        return []
    return [i < filled for i in range(total)]


@register.filter
def format_insights(text):
    """Convert Claude's bullet-list response into styled HTML list items."""
    if not text:
        return ""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    lines = text.strip().split("\n")
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line and line[0] in ("•", "-", "*", "–"):
            line = line[1:].strip()
        if line:
            items.append(f'<li class="insights-item">{escape(line)}</li>')
    if not items:
        return mark_safe(escape(text))
    return mark_safe('<ul class="insights-list">' + "".join(items) + "</ul>")


@register.filter
def hr_zones(workout):
    zones = [
        (1, workout.hr_z1_seconds, "#4FC3F7", "Zone 1"),
        (2, workout.hr_z2_seconds, "#81C784", "Zone 2"),
        (3, workout.hr_z3_seconds, "#FFD54F", "Zone 3"),
        (4, workout.hr_z4_seconds, "#FF8A65", "Zone 4"),
        (5, workout.hr_z5_seconds, "#E57373", "Zone 5"),
    ]
    return [(z, s or 0, c, l) for z, s, c, l in zones]


@register.filter
def format_next_workout(text):
    """Render the structured next-workout recommendation as styled HTML."""
    if not text:
        return ""
    from django.utils.html import escape
    intensity = activity = reason = ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("INTENSITY:"):
            intensity = line[len("INTENSITY:"):].strip()
        elif line.upper().startswith("ACTIVITY:"):
            activity = line[len("ACTIVITY:"):].strip()
        elif line.upper().startswith("REASON:"):
            reason = line[len("REASON:"):].strip()

    # Whole class names (Tailwind only generates classes it finds written out).
    intensity_tones = {
        "GO HARD": "text-error",
        "GO MODERATE": "text-warning",
        "GO EASY": "text-success",
        "REST": "text-info",
    }
    tone = next((c for k, c in intensity_tones.items() if k in intensity.upper()), "text-primary")

    from django.utils.safestring import mark_safe
    if not intensity and not activity:
        return mark_safe(f'<p class="ai-text">{escape(text)}</p>')

    html = ""
    if intensity:
        html += f'<div class="nw-intensity {tone}">{escape(intensity)}</div>'
    if activity:
        html += f'<div class="nw-activity">{escape(activity)}</div>'
    if reason:
        html += f'<p class="nw-reason">{escape(reason)}</p>'
    return mark_safe(html)


@register.filter
def format_day_analysis(text):
    """Render the structured day analysis (HEADLINE + bullets) as styled HTML."""
    if not text:
        return ""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    headline = ""
    bullets = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("HEADLINE:"):
            headline = stripped[len("HEADLINE:"):].strip()
        elif stripped.startswith("•") or stripped.startswith("-") or stripped.startswith("*"):
            bullets.append(stripped.lstrip("•-* ").strip())

    if not headline and not bullets:
        return mark_safe(f'<p class="ai-text">{escape(text)}</p>')

    html = ""
    if headline:
        html += f'<div class="ai-headline">{escape(headline)}</div>'
    if bullets:
        items = "".join(f'<li class="insights-item">{escape(b)}</li>' for b in bullets)
        html += f'<ul class="insights-list">{items}</ul>'
    return mark_safe(html)


@register.filter
def format_body_commentary(text):
    """Parse HEADLINE: + bullet points into styled HTML."""
    if not text:
        return ""
    from django.utils.html import escape
    from django.utils.safestring import mark_safe
    headline = ""
    bullets = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("HEADLINE:"):
            headline = stripped[len("HEADLINE:"):].strip()
        elif stripped.startswith("•") or stripped.startswith("-") or stripped.startswith("*"):
            bullets.append(stripped.lstrip("•-* ").strip())
    if not headline and not bullets:
        return mark_safe(f'<p class="ai-text">{escape(text)}</p>')
    html = ""
    if headline:
        html += f'<div class="ai-headline">{escape(headline)}</div>'
    if bullets:
        items = "".join(f'<li class="insights-item">{escape(b)}</li>' for b in bullets)
        html += f'<ul class="insights-list">{items}</ul>'
    return mark_safe(html)


@register.filter
def div(value, arg):
    try:
        return int(value) // int(arg)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


@register.filter
def mod(value, arg):
    try:
        return int(value) % int(arg)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


_GARMIN_EMOJI = {
    "running": "🏃",
    "walking": "🚶",
    "strength": "💪",
    "cycling": "🚴",
    "yoga": "🧘",
    "stretching": "🤸",
    "hiking": "🥾",
    "swimming": "🏊",
    "cardio": "🔥",
    "meditation": "🧠",
}

@register.filter
def garmin_emoji(discipline):
    return _GARMIN_EMOJI.get((discipline or "").lower(), "🏅")


@register.filter
def format_nutrition_insights(text):
    """Render the structured nutrition insights (## headers + body) as styled HTML."""
    if not text:
        return ""
    import re
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    html = ""
    current_section = []
    current_header = None

    def _apply_inline(text):
        """Escape HTML then convert **bold** markdown to <strong>."""
        escaped = escape(text)
        return re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', escaped)

    def _flush(header, body_lines):
        nonlocal html
        if not body_lines:
            return
        if header:
            html += f'<div class="ni-section"><div class="ni-header">{escape(header)}</div>'

        # Group consecutive non-blank lines into paragraphs; detect bullets
        paragraphs = []  # list of ('para' | 'bullet', text)
        current_para = []
        for raw in "\n".join(body_lines).splitlines():
            raw = raw.strip()
            if not raw:
                if current_para:
                    paragraphs.append(('para', ' '.join(current_para)))
                    current_para = []
            elif (raw.startswith("- ") or raw.startswith("• ")
                  or (raw.startswith("* ") and not raw.startswith("**"))):
                if current_para:
                    paragraphs.append(('para', ' '.join(current_para)))
                    current_para = []
                paragraphs.append(('bullet', raw.lstrip("-•* ").strip()))
            else:
                current_para.append(raw)
        if current_para:
            paragraphs.append(('para', ' '.join(current_para)))

        lines_out = []
        for ptype, content in paragraphs:
            rendered = _apply_inline(content)
            if ptype == 'bullet':
                lines_out.append(f'<li class="insights-item">{rendered}</li>')
            else:
                lines_out.append(f'<p class="ni-para">{rendered}</p>')
        block = "".join(lines_out)
        # Wrap consecutive <li> in <ul>
        block = re.sub(r'(<li class="insights-item">.*?</li>)+',
                       lambda m: f'<ul class="ni-list insights-list">{m.group(0)}</ul>',
                       block, flags=re.DOTALL)
        html += block
        if header:
            html += '</div>'

    for line in text.strip().splitlines():
        if line.startswith("## "):
            _flush(current_header, current_section)
            current_header = line[3:].strip()
            current_section = []
        else:
            current_section.append(line)
    _flush(current_header, current_section)

    return mark_safe(html)


@register.filter
def format_chat_answer(text):
    """Render a stats-chat assistant reply: **bold** inline, - bullets grouped
    into a list, blank-line-separated paragraphs. Same shape as the body
    parsing in format_nutrition_insights, minus the ## section splitting —
    chat answers are freeform prose, not headed sections."""
    if not text:
        return ""
    import re
    from django.utils.html import escape
    from django.utils.safestring import mark_safe

    def _apply_inline(s):
        return re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', escape(s))

    paragraphs = []  # list of ('para' | 'bullet', text)
    current_para = []
    for raw in text.strip().splitlines():
        raw = raw.strip()
        if not raw:
            if current_para:
                paragraphs.append(('para', ' '.join(current_para)))
                current_para = []
        elif (raw.startswith("- ") or raw.startswith("• ")
              or (raw.startswith("* ") and not raw.startswith("**"))):
            if current_para:
                paragraphs.append(('para', ' '.join(current_para)))
                current_para = []
            paragraphs.append(('bullet', re.sub(r'^[-•*]\s+', '', raw)))
        else:
            current_para.append(raw)
    if current_para:
        paragraphs.append(('para', ' '.join(current_para)))

    lines_out = []
    for ptype, content in paragraphs:
        rendered = _apply_inline(content)
        if ptype == 'bullet':
            lines_out.append(f'<li>{rendered}</li>')
        else:
            lines_out.append(f'<p>{rendered}</p>')
    html = "".join(lines_out)
    html = re.sub(r'(<li>.*?</li>)+',
                   lambda m: f'<ul class="chat-list">{m.group(0)}</ul>',
                   html, flags=re.DOTALL)

    return mark_safe(html)


@register.filter
def dict_get(d, key):
    """Get a value from a dict by key (useful when key is dynamic in templates)."""
    if isinstance(d, dict):
        return d.get(key)
    return None


@register.filter
def discipline_color(slug):
    """A discipline's DISCIPLINE_COLORS hex (for inline color/border styles)."""
    from workouts.views import DISCIPLINE_COLORS
    return DISCIPLINE_COLORS.get(slug or "", "#888888")


@register.filter
def nutrition_rows(_unused):
    """Returns (label, key, color) tuples for nutrition progress bars."""
    # Fixed chart slots (assets/css/app.css): Calories 1, Protein 2, Fat 3, Fiber 4, Carbs 5.
    return [
        ("Calories", "cal",     "var(--chart-1)"),
        ("Protein",  "protein", "var(--chart-2)"),
        ("Carbs",    "carbs",   "var(--chart-5)"),
        ("Fat",      "fat",     "var(--chart-3)"),
        ("Fiber",    "fiber",   "var(--chart-4)"),
    ]


# Status words → whole Tailwind classes (Tailwind can't see class names built up
# from strings). Status color always comes with its label text, never alone.
TONE_TEXT = {"green": "text-success", "yellow": "text-warning", "red": "text-error",
             "high": "text-success", "moderate": "text-warning", "low": "text-error",
             "balanced": "text-success", "unbalanced": "text-warning", "poor": "text-error"}
TONE_STROKE = {"green": "stroke-success", "yellow": "stroke-warning", "red": "stroke-error",
               "high": "stroke-success", "moderate": "stroke-warning", "low": "stroke-error",
               "balanced": "stroke-success", "unbalanced": "stroke-warning", "poor": "stroke-error"}


@register.filter
def tone(value, kind="text"):
    """'green'/'High'/'BALANCED' → a whole class: text-success, or with
    kind="stroke", stroke-success. Unknown → text-muted / stroke-current."""
    table = TONE_STROKE if kind == "stroke" else TONE_TEXT
    return table.get(str(value or "").strip().lower(), "text-muted" if kind == "text" else "stroke-current")


@register.filter
def format_duration_hm(seconds):
    """Format seconds as 'Xh Ym' (e.g. for sleep duration)."""
    if not seconds:
        return "—"
    try:
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours == 0:
            return f"{minutes}m"
        return f"{hours}h {minutes}m"
    except (TypeError, ValueError):
        return "—"


@register.filter
def ai_plan_tag(program):
    """'AI plan · 5K on Nov 22' for an AI training plan (Program.goal_json), else ''."""
    from datetime import date
    goal = getattr(program, "goal_json", None) or {}
    if not goal.get("goal"):
        return ""
    from workouts.training_plans import RACE_LABELS
    if goal.get("race_date"):
        race = date.fromisoformat(goal["race_date"])
        return f"AI plan · {RACE_LABELS.get(goal['goal'], 'Race')} on {race:%b} {race.day}"
    return f"AI plan · {goal['weeks']}-week base" if goal.get("weeks") else "AI plan · running base"
