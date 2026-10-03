from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone


def make_user(username, superuser=False, features=None, ai=False, set_up=True):
    """A user ready to use the app: password already changed and Get Started
    finished (set_up=False leaves them like a brand-new account), with the
    given feature slugs (`ai` turns the AI master switch on)."""
    from workouts.access import access_for
    U = get_user_model()
    if superuser:
        user = U.objects.create_superuser(username, f"{username}@example.com", "pw")
    else:
        user = U.objects.create_user(username, f"{username}@example.com", "pw")
    access = access_for(user)
    access.features = list(features or [])
    access.ai_enabled = ai
    access.must_change_password = not set_up
    access.onboarding_completed_at = timezone.now() if set_up else None
    access.save()
    return user


class _NoNetwork(Exception):
    pass


class TwoUserTestCase(TestCase):
    """self.a (superuser/owner) and self.b (regular, every non-AI feature, set up).
    self.client_a / self.client_b are logged in.

    Anthropic and Garmin calls are blocked (pages generate AI inline, and a real key may be
    in the environment); a test that needs them patches requests itself."""
    def setUp(self):
        self.a = make_user("alice", superuser=True)
        from workouts.access import FEATURES
        self.b = make_user("bob", features=[s for s, f in FEATURES.items() if not f["ai"]])
        self.client_a = self.client_class(); self.client_a.force_login(self.a)
        self.client_b = self.client_class(); self.client_b.force_login(self.b)
        blocked = [f"workouts.llm.requests.{name}" for name in ("post", "get")]
        blocked.append("workouts.sync.GarminClient")   # day_view lazily syncs Garmin for the owner
        for target in blocked:
            p = patch(target, side_effect=_NoNetwork("no network in tests"))
            p.start()
            self.addCleanup(p.stop)
