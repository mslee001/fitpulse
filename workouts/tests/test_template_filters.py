from django.template import Context, Template, TemplateSyntaxError
from django.test import RequestFactory, SimpleTestCase, override_settings

from workouts.glossary import METRIC_HELP
from workouts.templatetags.workout_filters import format_next_workout, reltime, ring_offset, tone


class ToneFilterTests(SimpleTestCase):
    def test_colors_and_labels_map_to_whole_classes(self):
        self.assertEqual(tone("green"), "text-success")
        self.assertEqual(tone("yellow"), "text-warning")
        self.assertEqual(tone("red"), "text-error")
        self.assertEqual(tone("High"), "text-success")
        self.assertEqual(tone(" moderate "), "text-warning")
        self.assertEqual(tone("BALANCED"), "text-success")
        self.assertEqual(tone("UNBALANCED"), "text-warning")

    def test_stroke_kind(self):
        self.assertEqual(tone("green", "stroke"), "stroke-success")
        self.assertEqual(tone("Low", "stroke"), "stroke-error")

    def test_bg_kind(self):
        self.assertEqual(tone("green", "bg"), "bg-success")
        self.assertEqual(tone("yellow", "bg"), "bg-warning")
        self.assertEqual(tone("red", "bg"), "bg-error")
        self.assertEqual(tone("", "bg"), "hidden")

    def test_unknown_or_empty_is_muted(self):
        self.assertEqual(tone("PRIME"), "text-muted")
        self.assertEqual(tone(""), "text-muted")
        self.assertEqual(tone(None), "text-muted")
        self.assertEqual(tone(None, "stroke"), "stroke-current")


class NextWorkoutFilterTests(SimpleTestCase):
    def test_emits_classes_not_inline_styles(self):
        html = format_next_workout("INTENSITY: GO EASY\nACTIVITY: 20 min walk\nREASON: Low HRV.")
        self.assertIn('class="nw-intensity text-success"', html)
        self.assertIn('class="nw-activity"', html)
        self.assertIn('class="nw-reason"', html)
        self.assertNotIn("style=", html)


class RingOffsetTests(SimpleTestCase):
    def test_offset_leaves_score_percent_drawn(self):
        self.assertEqual(ring_offset(100), 0.0)
        self.assertEqual(ring_offset(0), 301.6)
        self.assertEqual(ring_offset(72), 84.4)

    def test_clamps_and_tolerates_junk(self):
        self.assertEqual(ring_offset(150), 0.0)
        self.assertEqual(ring_offset(None), 301.6)


class MetricHelpTagTests(SimpleTestCase):
    def render(self, src):
        request = RequestFactory().get("/")
        return Template("{% load workout_filters %}" + src).render(Context({"request": request}))

    def test_renders_popover_with_unique_ids(self):
        html = self.render('{% metric_help "hrv" %}{% metric_help "hrv" %}')
        self.assertIn('popovertarget="mh-hrv-1"', html)
        self.assertIn('id="mh-hrv-2"', html)
        self.assertIn('aria-label="What is HRV?"', html)
        self.assertIn(METRIC_HELP["hrv"][1][:30], html)

    @override_settings(DEBUG=True)
    def test_unknown_slug_raises_in_debug(self):
        with self.assertRaises(TemplateSyntaxError):
            self.render('{% metric_help "nope" %}')

    @override_settings(DEBUG=False)
    def test_unknown_slug_renders_nothing_in_production(self):
        self.assertEqual(self.render('{% metric_help "nope" %}').strip(), "")


class ReltimeTests(SimpleTestCase):
    def setUp(self):
        from datetime import datetime
        from django.utils import timezone
        self.now = timezone.make_aware(datetime(2026, 10, 4, 15, 0))

    def text(self, **delta):
        import re
        from datetime import timedelta
        html = reltime(self.now - timedelta(**delta), now=self.now)
        return re.search(r">([^<]*)</time>", html).group(1)

    def test_steps(self):
        self.assertEqual(self.text(seconds=20), "just now")
        self.assertEqual(self.text(minutes=5), "5 min ago")
        self.assertEqual(self.text(hours=2), "2 h ago")
        self.assertEqual(self.text(hours=20), "yesterday")       # Oct 3, 7 PM
        self.assertEqual(self.text(days=3), "3 days ago")
        self.assertEqual(self.text(days=20), "Sep 14")
        self.assertEqual(self.text(days=400), "Aug 30, 2025")

    def test_future_reads_just_now(self):
        self.assertEqual(self.text(minutes=-10), "just now")

    def test_markup_has_iso_and_full_title(self):
        html = reltime(self.now, now=self.now)
        self.assertIn('datetime="2026-10-04T15:00:00-07:00"', html)
        self.assertIn('title="Sun, Oct 4, 2026, 3:00 PM"', html)
        self.assertEqual(reltime(None), "")
