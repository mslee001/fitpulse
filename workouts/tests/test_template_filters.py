from django.template import Context, Template, TemplateSyntaxError
from django.test import RequestFactory, SimpleTestCase, override_settings

from workouts.glossary import METRIC_HELP
from workouts.templatetags.workout_filters import format_next_workout, ring_offset, tone


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
