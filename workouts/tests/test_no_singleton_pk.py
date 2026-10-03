"""Guard: per-user settings/auth rows must never be looked up as a pk=1 singleton.

A leftover `UserSettings.get()` or `filter(pk=1)` would quietly read the owner's
row for everyone, so this fails the build on any reintroduction."""
import re
from pathlib import Path

from django.test import SimpleTestCase

WORKOUTS = Path(__file__).resolve().parent.parent

PATTERNS = [
    re.compile(r"\bpk=1\b"),
    re.compile(r"\b(UserSettings|NutritionProfile|AthleteProfile|WithingsAuth|PelotonAuth|GoogleHealthAuth)\.get\(\)"),
    re.compile(r"objects\.filter\(pk=1\)"),
]


class NoSingletonLookupsTests(SimpleTestCase):
    def test_no_pk1_or_singleton_get(self):
        hits = []
        for path in sorted(WORKOUTS.rglob("*.py")):
            rel = path.relative_to(WORKOUTS)
            if rel.parts[0] in ("migrations", "tests"):
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if any(p.search(line) for p in PATTERNS):
                    hits.append(f"workouts/{rel}:{lineno}: {line.strip()}")
        self.assertEqual(hits, [], "Singleton-style lookups found:\n" + "\n".join(hits))
