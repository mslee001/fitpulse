"""The nav groups: one definition for the desktop dropdowns, the phone menu and the
section tabs under each page title. Add a new page here and the menus and tabs follow.

Each item is (label, url_name, requires, muted). `requires` is a feature slug (checked
with has_feature), "owner", or None (the group's own gate in base.html is enough).
Muted items (Settings, Users, Macro Settings) sit under a divider in the menus and are
never tabs. None is a divider.
"""

from django.urls import reverse

from .access import has_feature

NAV_GROUPS = {
    "training": [
        ("Workouts", "history", None, False),
        ("Dashboard", "dashboard", None, False),
        ("Compare", "compare", None, False),
        ("Calendar", "calendar", None, False),
        ("Analytics", "analytics", None, False),
        ("Programs", "program_list", "programs", False),
        ("Strength", "strength_trends", "strength", False),
        None,
        ("Settings", "settings", None, True),
        ("Users", "admin_users", "owner", True),
    ],
    "body": [
        ("Interventions", "interventions", "interventions", False),
        ("Symptoms", "symptoms", "symptoms", False),
        ("Analytics", "body", "body", False),
    ],
    "nutrition": [
        ("Today's Log", "nutrition", None, False),
        ("Analytics", "nutrition_analytics", None, False),
        None,
        ("Macro Settings", "nutrition_targets", None, True),
    ],
    "insights": [
        ("Pattern Insights", "insights", "ai_pattern_insights", False),
        ("Weekly Review", "weekly_review", "ai_weekly_review", False),
        ("Intervention Analysis", "intervention_analysis", "interventions", False),
    ],
}

# Other routes that are the same page as a tab (highlighted as that tab).
TAB_ALIASES = {"calendar_month": "calendar"}

NAV_GROUP_LABELS = {"training": "Training", "body": "Body", "nutrition": "Nutrition", "insights": "Insights"}


def _allowed(user, requires, can=None):
    if requires is None:
        return True
    if requires == "owner":
        return user.is_superuser
    return can[requires] if can is not None else has_feature(user, requires)


def nav_for(user, group, can=None):
    """The group's visible items, in order: {"label", "url_name", "url", "muted"} dicts,
    None for a divider (never first, last or twice in a row). `can` is the request's
    cached feature lookup (context_processors._Can), when there is one."""
    items = []
    for entry in NAV_GROUPS[group]:
        if entry is None:
            if items and items[-1] is not None:
                items.append(None)
            continue
        label, url_name, requires, muted = entry
        if _allowed(user, requires, can):
            items.append({"label": label, "url_name": url_name, "url": reverse(url_name), "muted": muted})
    while items and items[-1] is None:
        items.pop()
    return items


def group_for_url_name(url_name):
    """The group whose tabs include this page (non-muted items only), else None:
    Settings, detail pages, day view and other pages outside the tabs get none."""
    url_name = TAB_ALIASES.get(url_name, url_name)
    for group, entries in NAV_GROUPS.items():
        for entry in entries:
            if entry and not entry[3] and entry[1] == url_name:
                return group
    return None


def section_tabs(user, url_name, can=None):
    """(group label, [tab items]) for the current page, or None when it has no tabs
    (not in a group, or the group has fewer than 2 visible tabs)."""
    group = group_for_url_name(url_name)
    if not group:
        return None
    tabs = [i for i in nav_for(user, group, can) if i and not i["muted"]]
    if len(tabs) < 2:
        return None
    current = TAB_ALIASES.get(url_name, url_name)
    for t in tabs:
        t["current"] = t["url_name"] == current
    return NAV_GROUP_LABELS[group], tabs
