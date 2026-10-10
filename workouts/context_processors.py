import hashlib

from .access import has_feature


class _Can:
    """{% if can.nutrition %} in templates — feature checks, cached per request."""
    def __init__(self, user):
        self.user = user
        self._cache = {}

    def __getitem__(self, slug):
        if slug not in self._cache:
            self._cache[slug] = has_feature(self.user, slug)
        return self._cache[slug]


class _LazyNav(dict):
    """nav.training / nav|dict_get:group → that group's visible items
    (navigation.nav_for), built on first use and cached for the request."""
    def __init__(self, user, can):
        super().__init__()
        self.user, self.can = user, can

    def __missing__(self, group):
        from .navigation import NAV_GROUPS, nav_for
        if group not in NAV_GROUPS:
            raise KeyError(group)
        self[group] = nav_for(self.user, group, self.can)
        return self[group]

    def get(self, group, default=None):
        try:
            return self[group]
        except KeyError:
            return default


class _LazyTabs:
    """The section tabs for the current page (navigation.section_tabs): `label`, `items`;
    falsy when the page has none."""
    def __init__(self, user, url_name, can):
        self.user, self.url_name, self.can = user, url_name, can
        self._value = False

    def _load(self):
        if self._value is False:
            from .navigation import section_tabs
            self._value = section_tabs(self.user, self.url_name, self.can) if self.url_name else None
        return self._value

    def __bool__(self):
        return bool(self._load())

    @property
    def label(self):
        return self._load()[0]

    @property
    def items(self):
        return self._load()[1]


class _LazyDemoAvailable:
    """Signed out: whether to offer "Explore the demo" (one query, only if used)."""
    def __bool__(self):
        from .demo import demo_user
        return demo_user() is not None


def access(request):
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return {"demo_available": _LazyDemoAvailable()}
    from .demo import is_demo
    from .navigation import group_for_url_name
    can = _Can(u)
    match = getattr(request, "resolver_match", None)
    url_name = match.url_name if match else None
    return {"can": can, "is_owner": u.is_superuser, "is_demo": is_demo(u), "setup_pending": _LazyPending(u),
            "peloton_reconnect": _LazyPelotonReconnect(u), "plan_nudges": _LazyPlanNudges(u),
            "nav": _LazyNav(u, can), "nav_current_url_name": url_name,
            "nav_current_group": group_for_url_name(url_name),
            "section_tabs": _LazyTabs(u, url_name, can)}


class _LazyPending:
    """Steps that became to-do after setup (e.g. a feature granted later). Lazy,
    so pages that never render the banner don't pay for the lookups."""
    def __init__(self, user):
        self.user = user
        self._steps = None

    def _load(self):
        if self._steps is None:
            from .onboarding import pending_after_setup
            self._steps = pending_after_setup(self.user)
        return self._steps

    def __iter__(self):
        return iter(self._load())

    def __bool__(self):
        return bool(self._load())


class _LazyPelotonReconnect:
    """True when the user's Peloton sign-in ended (or was never renewed after the
    switch to refresh tokens) and the Peloton integration is still enabled.
    `key` changes with each failure, so dismissing the banner hides only this one."""
    def __init__(self, user):
        self.user = user
        self._auth = None
        self._value = None

    def __bool__(self):
        if self._value is None:
            from .models import Integration, PelotonAuth
            self._auth = PelotonAuth.for_user(self.user)
            self._value = bool(
                self._auth and self._auth.needs_reconnect
                and Integration.objects.for_user(self.user).filter(key="peloton", is_enabled=True).exists()
            )
        return self._value

    @property
    def key(self):
        if not self:
            return ""
        failed = self._auth.auth_failed_at
        return str(int(failed.timestamp())) if failed else "no-token"


class _LazyPlanNudges:
    """AI training plans whose runs suggest a reassessment (training_plans.reassess_signals):
    [{"program", "run_pk", "reasons", "key"}]. Only users with the feature are checked."""
    def __init__(self, user):
        self.user = user
        self._items = None

    def _load(self):
        if self._items is None:
            self._items = []
            if has_feature(self.user, "ai_program_tools"):
                from .models import Program
                from .training_plans import reassess_signals
                for p in Program.objects.for_user(self.user).exclude(goal_json={}):
                    run = p.active_run
                    reasons = reassess_signals(p) if run and p.goal_json.get("start_date") else []
                    if reasons:
                        # Stable across restarts (hash() isn't), so a dismissal sticks until the reasons change.
                        digest = hashlib.md5("|".join(reasons).encode()).hexdigest()[:10]
                        self._items.append({"program": p, "run_pk": run.pk, "reasons": reasons,
                                            "key": f"{p.pk}-{digest}"})
        return self._items

    def __iter__(self):
        return iter(self._load())

    def __bool__(self):
        return bool(self._load())
