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


def access(request):
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return {}
    return {"can": _Can(u), "is_owner": u.is_superuser, "setup_pending": _LazyPending(u)}


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
