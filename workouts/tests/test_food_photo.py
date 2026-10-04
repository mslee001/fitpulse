from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.urls import reverse

from workouts import llm
from workouts.ai import parse_food_text

MEAL_RESPONSE = {
    "image_type": "meal",
    "items": [{"name": "rice", "quantity": "~1 cup", "calories": 205, "protein_g": 4,
               "carbs_g": 45, "fat_g": 0, "fiber_g": 1}],
    "meal_guess": "dinner",
    "confidence": "medium",
    "note": "x",
}


def jpeg(size=100):
    return SimpleUploadedFile("p.jpg", b"\xff\xd8\xff" + b"0" * size, content_type="image/jpeg")


class ParseFoodTextTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")

    @patch("workouts.ai.llm.call_json", return_value=dict(MEAL_RESPONSE))
    def test_image_branch_uses_sonnet_and_sends_image(self, call_json):
        result = parse_food_text(self.user, "", "", image_b64="AAAA", image_media_type="image/jpeg")

        prompt = call_json.call_args.args[0]
        kwargs = call_json.call_args.kwargs
        self.assertEqual(kwargs["model"], llm.SONNET)
        self.assertEqual(kwargs["message_content"][0]["type"], "image")
        self.assertIn("CLASSIFY", prompt)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], llm.SONNET)
        self.assertEqual(result["image_type"], "meal")

    @patch("workouts.ai.llm.call_json")
    def test_missing_image_type_defaults_to_label(self, call_json):
        call_json.return_value = {"items": [], "confidence": "high", "note": ""}
        result = parse_food_text(self.user, "", "", image_b64="AAAA")
        self.assertEqual(result["image_type"], "label")

    @patch("workouts.ai._lookup_branded_nutrition", return_value=[])
    @patch("workouts.ai._lookup_open_food_facts", return_value=[])
    @patch("workouts.ai.llm.call_json")
    def test_text_branch_unchanged(self, call_json, *_):
        call_json.return_value = {"items": [{"name": "eggs"}], "confidence": "high", "note": ""}
        result = parse_food_text(self.user, "2 eggs")

        kwargs = call_json.call_args.kwargs
        self.assertEqual(kwargs["model"], llm.HAIKU)
        self.assertIsInstance(kwargs["message_content"], str)
        self.assertEqual(result["model"], llm.HAIKU)
        self.assertNotIn("image_type", result)


class NutritionParseViewTests(TestCase):
    # The view is called directly (not via self.client) because Django 4.2's test
    # client can't copy template contexts on Python 3.14 — same as test_program_config.
    def setUp(self):
        self.user = get_user_model().objects.create_user("t", password="x")

    def post(self, data):
        from workouts.views import nutrition_parse_api
        req = RequestFactory().post(reverse("nutrition_parse"), data)
        req.user, req.session = self.user, {}
        return nutrition_parse_api(req)

    @patch("workouts.ai.llm.call_json", return_value=dict(MEAL_RESPONSE))
    def test_photo_only_parse_sets_raw_text_fallback(self, call_json):
        resp = self.post({"raw_text": "", "label_image": jpeg()})

        self.assertContains(resp, 'value="Photo: rice"')
        self.assertContains(resp, f'name="ai_model" value="{llm.SONNET}"')
        self.assertContains(resp, "Estimated from photo")
        call_json.assert_called_once()

    @patch("workouts.ai.llm.call_json")
    def test_oversized_upload_rejected(self, call_json):
        resp = self.post({"label_image": jpeg(4_000_000)})

        self.assertContains(resp, "too large")
        call_json.assert_not_called()

    @patch("workouts.ai.llm.call_json")
    def test_heic_rejected(self, call_json):
        heic = SimpleUploadedFile("p.heic", b"\x00" * 100, content_type="image/heic")
        resp = self.post({"label_image": heic})

        self.assertContains(resp, "image format can&#x27;t be read")
        call_json.assert_not_called()
