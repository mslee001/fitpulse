"""Django 4.2 can't copy a template Context on Python 3.14 (BaseContext.__copy__), and
`{% include … only %}` and inclusion tags both copy it. The test package patches the
copy so the test client works, which means a broken page would still pass every view
test. These greps are the guard."""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]


class NoContextCopyTests(SimpleTestCase):
    def test_no_include_only(self):
        hits = [
            f"{p.relative_to(ROOT)}:{i}"
            for p in (ROOT / "templates").rglob("*.html")
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r"\{%\s*include\b[^%]*\bonly\s*%\}", line)
        ]
        self.assertEqual(hits, [], "Use a plain {% include … with %} (no `only`) on Python 3.14")

    def test_no_inclusion_tags(self):
        hits = [
            f"{p.relative_to(ROOT)}:{i}"
            for p in (ROOT / "workouts").rglob("*.py")
            if "tests" not in p.parts
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r"\.inclusion_tag\s*\(", line)
        ]
        self.assertEqual(hits, [], "Use a simple_tag + render_to_string instead of an inclusion_tag")
