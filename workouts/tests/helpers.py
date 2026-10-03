from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase


def make_user(username, superuser=False):
    U = get_user_model()
    if superuser:
        return U.objects.create_superuser(username, f"{username}@example.com", "pw")
    return U.objects.create_user(username, f"{username}@example.com", "pw")


class _NoNetwork(Exception):
    pass


class TwoUserTestCase(TestCase):
    """self.a (superuser/owner) and self.b (regular). self.client_a / self.client_b are logged in.

    Anthropic and Garmin calls are blocked (pages generate AI inline, and a real key may be
    in the environment); a test that needs them patches requests itself."""
    def setUp(self):
        self.a = make_user("alice", superuser=True)
        self.b = make_user("bob")
        self.client_a = self.client_class(); self.client_a.force_login(self.a)
        self.client_b = self.client_class(); self.client_b.force_login(self.b)
        blocked = [f"workouts.llm.requests.{name}" for name in ("post", "get")]
        blocked.append("workouts.sync.GarminClient")   # day_view lazily syncs Garmin for the owner
        for target in blocked:
            p = patch(target, side_effect=_NoNetwork("no network in tests"))
            p.start()
            self.addCleanup(p.stop)
