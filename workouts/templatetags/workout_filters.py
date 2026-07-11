from django import template
from django.db.models import Max

register = template.Library()


@register.simple_tag
def last_synced():
    from workouts.models import CachedWorkout
    result = CachedWorkout.objects.aggregate(Max('synced_at'))
    return result.get('synced_at__max')


@register.simple_tag
def last_daily_sync():
    from workouts.models import UserSettings
    return UserSettings.objects.filter(pk=1).values_list('last_daily_sync_at', flat=True).first()


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

    intensity_colors = {
        "GO HARD": "var(--accent-red)",
        "GO MODERATE": "#FFCC00",
        "GO EASY": "var(--accent-green)",
        "REST": "var(--accent-alt)",
    }
    color = next((c for k, c in intensity_colors.items() if k in intensity.upper()), "var(--accent)")

    from django.utils.safestring import mark_safe
    if not intensity and not activity:
        return mark_safe(f'<p style="font-size:0.88rem;line-height:1.6">{escape(text)}</p>')

    html = ""
    if intensity:
        html += f'<div style="font-size:1rem;font-weight:800;letter-spacing:0.06em;color:{color};font-family:\'Barlow Condensed\',sans-serif;margin-bottom:0.3rem">{escape(intensity)}</div>'
    if activity:
        html += f'<div style="font-size:0.9rem;font-weight:600;margin-bottom:0.6rem">{escape(activity)}</div>'
    if reason:
        html += f'<p style="font-size:0.85rem;line-height:1.65;color:var(--text-muted);margin:0">{escape(reason)}</p>'
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
        return mark_safe(f'<p style="font-size:0.88rem;line-height:1.6">{escape(text)}</p>')

    html = ""
    if headline:
        html += f'<div style="font-weight:700;font-size:0.95rem;margin-bottom:0.6rem">{escape(headline)}</div>'
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
        return mark_safe(f'<p style="font-size:0.88rem;line-height:1.6">{escape(text)}</p>')
    html = ""
    if headline:
        html += f'<div style="font-weight:700;font-size:0.95rem;margin-bottom:0.6rem">{escape(headline)}</div>'
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
                lines_out.append(f'<p style="font-size:0.875rem;line-height:1.7;color:var(--text-muted);margin-bottom:0.65rem">{rendered}</p>')
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
def dict_get(d, key):
    """Get a value from a dict by key (useful when key is dynamic in templates)."""
    if isinstance(d, dict):
        return d.get(key)
    return None


@register.filter
def nutrition_rows(_unused):
    """Returns (label, key, color) tuples for nutrition progress bars."""
    return [
        ("Calories", "cal",     "#FF6B35"),
        ("Protein",  "protein", "#00D1FF"),
        ("Carbs",    "carbs",   "#B4FF39"),
        ("Fat",      "fat",     "#FF3B5C"),
        ("Fiber",    "fiber",   "#69f0ae"),
    ]


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
