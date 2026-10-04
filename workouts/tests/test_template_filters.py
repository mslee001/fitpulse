from django.test import SimpleTestCase

from workouts.templatetags.workout_filters import format_next_workout, tone


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
