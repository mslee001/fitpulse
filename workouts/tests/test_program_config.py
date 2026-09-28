from datetime import date, datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from workouts.models import (
    CachedWorkout, Program, ProgramRecovery, ProgramRun, ProgramSlot, ProgramWeek, ProgramWorkout, RunWeek,
)
from workouts.programs import (
    ANY_CLASS_PRESETS, associate_workout, attach_recoveries, create_plan, duplicate_program,
    preset_for, recovery_kind, resolve_slot_match,
)

RIDE_A = "a" * 32
RIDE_B = "b" * 32
BASE = datetime(2026, 9, 22, 19, 0, tzinfo=dt_tz.utc)
_n = 0


def mk(offset_min, minutes, discipline, title, ride_id=""):
    global _n
    _n += 1
    return CachedWorkout.objects.create(
        workout_id=f"c{_n}", ride_id=ride_id, title=title, discipline=discipline, source="peloton",
        created_at=BASE + timedelta(minutes=offset_min), duration_seconds=minutes * 60)


class ResolveSlotMatchTests(SimpleTestCase):
    def test_ride_accepts_id_and_class_link(self):
        self.assertEqual(resolve_slot_match("ride", RIDE_A)["peloton_ride_id"], RIDE_A)
        linked = resolve_slot_match("ride", f"https://members.onepeloton.com/classes/player/{RIDE_B}")
        self.assertEqual(linked["peloton_ride_id"], RIDE_B)

    def test_ride_rejects_unreadable_input_only_when_strict(self):
        with self.assertRaises(ValueError):
            resolve_slot_match("ride", "not a ride")
        self.assertEqual(resolve_slot_match("ride", "not a ride", strict=False)["peloton_ride_id"], "")
        self.assertEqual(resolve_slot_match("ride", "")["peloton_ride_id"], "")    # blank is fine: matches by title

    def test_any_class_preset_and_custom(self):
        m = resolve_slot_match("any", preset="pilates")
        self.assertEqual((m["match_discipline"], m["match_title_keyword"], m["peloton_ride_id"]), ("strength", "pilates", ""))
        c = resolve_slot_match("any", preset="custom", discipline=" Cycling ", keyword=" Power Zone ")
        self.assertEqual((c["match_discipline"], c["match_title_keyword"]), ("cycling", "power zone"))

    def test_any_class_needs_a_type(self):
        with self.assertRaises(ValueError):
            resolve_slot_match("any", preset="")
        with self.assertRaises(ValueError):
            resolve_slot_match("any", preset="nope")
        self.assertEqual(resolve_slot_match("any", preset="", strict=False)["match_discipline"], "")

    def test_title_match_clears_everything(self):
        self.assertEqual(resolve_slot_match("title", RIDE_A, "yoga"),
                         {"peloton_ride_id": "", "match_discipline": "", "match_title_keyword": ""})

    def test_preset_round_trip(self):
        for key, (_label, disc, kw) in ANY_CLASS_PRESETS.items():
            self.assertEqual(preset_for(disc, kw), key)
        self.assertEqual(preset_for("yoga", "vinyasa"), "custom")
        self.assertEqual(preset_for("", ""), "")


class ProgramConfigBase(TestCase):
    def setUp(self):
        self.program = Program.objects.create(name="Split", slug="split", kind="split", match_strategy="ride_ids")
        self.week = ProgramWeek.objects.create(program=self.program, number=1)
        self.push = ProgramSlot.objects.create(week=self.week, title="Push", peloton_ride_id=RIDE_A, day=1,
                                               alt_ride_ids=[RIDE_B])
        self.yoga = ProgramSlot.objects.create(week=self.week, title="Yoga (any class)", day=6,
                                               match_discipline="yoga", discipline="yoga")


class DuplicateTests(ProgramConfigBase):
    def test_copy_clears_ride_ids_by_default_and_keeps_any_class_and_settings(self):
        self.program.track_recovery, self.program.recovery_window_min = True, 15
        self.program.recovery_walks = False
        self.program.save()
        ProgramRun.objects.create(program=self.program, start_date=date(2026, 9, 14))
        copy = duplicate_program(self.program)
        self.assertEqual(copy.name, "Split (copy)")
        self.assertNotEqual(copy.slug, self.program.slug)
        self.assertEqual(copy.runs.count(), 0)                                   # no cycles copied
        slots = {s.title: s for s in ProgramSlot.objects.filter(week__program=copy)}
        self.assertEqual(slots["Push"].peloton_ride_id, "")
        self.assertEqual(slots["Push"].alt_ride_ids, [])
        self.assertEqual(slots["Yoga (any class)"].match_discipline, "yoga")
        self.assertEqual((copy.track_recovery, copy.recovery_window_min, copy.recovery_walks), (True, 15, False))

    def test_keep_ride_ids_and_unique_slugs(self):
        a = duplicate_program(self.program, name="Same Name", keep_ride_ids=True)
        b = duplicate_program(self.program, name="Same Name")
        self.assertEqual(ProgramSlot.objects.get(week__program=a, title="Push").peloton_ride_id, RIDE_A)
        self.assertEqual(ProgramSlot.objects.get(week__program=a, title="Push").alt_ride_ids, [RIDE_B])
        self.assertNotEqual(a.slug, b.slug)


class CreatePlanTests(TestCase):
    def test_kind_defaults_and_any_class_fields(self):
        one = create_plan("One Week", "one-week", "", [{"number": 1, "slots": [
            {"title": "Pilates (any class)", "match_discipline": "strength", "match_title_keyword": "pilates", "day": 3}]}])
        two = create_plan("Two Weeks", "two-weeks", "", [{"number": 1, "slots": []}, {"number": 2, "slots": []}])
        self.assertEqual((one.kind, two.kind), ("split", "plan"))
        self.assertEqual(create_plan("Forced", "forced", "", [{"number": 1, "slots": []}], kind="plan").kind, "plan")
        slot = ProgramSlot.objects.get(week__program=one)
        self.assertEqual((slot.match_discipline, slot.match_title_keyword, slot.discipline), ("strength", "pilates", "strength"))


class RecoverySettingsTests(ProgramConfigBase):
    def setUp(self):
        super().setUp()
        self.program.track_recovery = True
        self.program.save()
        self.run = ProgramRun.objects.create(program=self.program, start_date=date(2026, 9, 14))
        self.entry = associate_workout(mk(0, 45, "circuit", "Push", RIDE_A))

    def test_window_is_per_program(self):
        mk(45 + 20, 5, "walking", "late walk")                # 20 min gap
        self.assertEqual(attach_recoveries(self.run), 0)
        self.program.recovery_window_min = 25
        self.program.save()
        self.assertEqual(attach_recoveries(self.run), 1)

    def test_max_length_is_per_program(self):
        w = mk(46, 40, "walking", "40 min walk")
        self.assertIsNone(recovery_kind(w, self.program))                 # default cap is 30
        self.program.recovery_max_min = 45
        self.assertEqual(recovery_kind(w, self.program), "walk")

    def test_walks_and_stretches_can_be_switched_off(self):
        walk, stretch = mk(46, 5, "walking", "walk"), mk(46, 5, "stretching", "stretch")
        self.program.recovery_walks = False
        self.assertIsNone(recovery_kind(walk, self.program))
        self.assertEqual(recovery_kind(stretch, self.program), "stretch")
        self.program.recovery_walks, self.program.recovery_stretches = True, False
        self.assertEqual(recovery_kind(walk, self.program), "walk")
        self.assertIsNone(recovery_kind(stretch, self.program))


class EditViewTests(ProgramConfigBase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("t", password="x")
        self.client.force_login(self.user)
        self.url = reverse("program_edit", args=["split"])

    def form(self, **extra):
        """A complete, unchanged edit form for the current state, plus overrides."""
        data = {
            "name": "Split", "instructor": "", "description": "",
            f"week_{self.week.pk}_label": "",
        }
        for s in (self.push, self.yoga):
            p = f"slot_{s.pk}_"
            data.update({
                p + "present": "1", p + "title": s.title, p + "day": s.day or "", p + "order": s.order,
                p + "duration": "", p + "match": "any" if s.match_discipline else "ride",
                p + "ride_id": s.peloton_ride_id, p + "preset": "yoga" if s.match_discipline else "",
                p + "discipline": "", p + "keyword": "",
            })
        data.update(extra)
        return data

    def post(self, **extra):
        return self.client.post(self.url, self.form(**extra), SERVER_NAME="localhost")

    def refresh(self):
        self.push.refresh_from_db(); self.yoga.refresh_from_db(); self.program.refresh_from_db()

    def test_program_and_recovery_settings_save(self):
        resp = self.post(name="Renamed", track_recovery="on", recovery_walks="on",
                         recovery_window_min="15", recovery_max_min="40", instructor="Robin")
        self.assertEqual(resp.status_code, 302)
        self.refresh()
        self.assertEqual((self.program.name, self.program.instructor), ("Renamed", "Robin"))
        self.assertEqual(self.program.slug, "split")                          # URLs stay stable
        self.assertTrue(self.program.track_recovery)
        self.assertTrue(self.program.recovery_walks)
        self.assertFalse(self.program.recovery_stretches)                     # unchecked box = off
        self.assertEqual((self.program.recovery_window_min, self.program.recovery_max_min), (15, 40))

    def test_out_of_range_numbers_keep_previous_values(self):
        self.post(recovery_window_min="9999", recovery_max_min="abc")
        self.program.refresh_from_db()
        self.assertEqual((self.program.recovery_window_min, self.program.recovery_max_min), (10, 30))

    def test_edit_slot_switching_a_ride_slot_to_any_class(self):
        p = f"slot_{self.push.pk}_"
        self.post(**{p + "match": "any", p + "preset": "pilates", p + "title": "Pilates day", p + "optional": "on"})
        self.refresh()
        self.assertEqual((self.push.title, self.push.match_discipline, self.push.match_title_keyword),
                         ("Pilates day", "strength", "pilates"))
        self.assertEqual((self.push.peloton_ride_id, self.push.alt_ride_ids, self.push.optional), ("", [], True))

    def test_changing_the_ride_id_drops_its_alternates(self):
        p = f"slot_{self.push.pk}_"
        self.post(**{p + "ride_id": f"https://members.onepeloton.com/classes/player/{RIDE_B}"})
        self.refresh()
        self.assertEqual((self.push.peloton_ride_id, self.push.alt_ride_ids), (RIDE_B, []))

    def test_unchanged_ride_id_keeps_its_alternates(self):
        self.post()
        self.refresh()
        self.assertEqual(self.push.alt_ride_ids, [RIDE_B])

    def test_delete_slot_keeps_its_completions_as_unslotted_entries(self):
        run = ProgramRun.objects.create(program=self.program, start_date=date(2026, 9, 14))
        rw = RunWeek.objects.create(run=run, program_week=self.week, sequence=1)
        entry = ProgramWorkout.objects.create(run_week=rw, slot=self.push, workout=mk(0, 45, "circuit", "Push", RIDE_A))
        self.post(**{f"slot_{self.push.pk}_delete": "on"})
        self.assertFalse(ProgramSlot.objects.filter(pk=self.push.pk).exists())
        entry.refresh_from_db()
        self.assertIsNone(entry.slot)

    def test_add_new_slots_and_skip_blank_and_invalid_ones(self):
        w = self.week.pk
        self.post(**{
            f"new_{w}_0_title": "Lower Body", f"new_{w}_0_day": "2", f"new_{w}_0_match": "ride",
            f"new_{w}_0_ride_id": RIDE_B,
            f"new_{w}_1_title": "", f"new_{w}_1_match": "ride",                       # blank row: ignored
            f"new_{w}_2_title": "Broken any", f"new_{w}_2_match": "any", f"new_{w}_2_preset": "",   # no type: rejected
            f"new_{w}_3_title": "Stretch day", f"new_{w}_3_match": "any", f"new_{w}_3_preset": "custom",
            f"new_{w}_3_discipline": "Stretching", f"new_{w}_3_keyword": "",
        })
        titles = set(ProgramSlot.objects.filter(week=self.week).values_list("title", flat=True))
        self.assertEqual(titles, {"Push", "Yoga (any class)", "Lower Body", "Stretch day"})
        low = ProgramSlot.objects.get(title="Lower Body")
        self.assertEqual((low.day, low.peloton_ride_id), (2, RIDE_B))
        self.assertEqual(ProgramSlot.objects.get(title="Stretch day").match_discipline, "stretching")

    def test_a_bad_row_is_reported_and_left_unchanged_while_others_still_save(self):
        from django.contrib.messages import get_messages
        p = f"slot_{self.yoga.pk}_"
        resp = self.post(name="Still Saved", **{p + "preset": "", p + "match": "any"})
        self.refresh()
        self.assertEqual(self.program.name, "Still Saved")
        self.assertEqual(self.yoga.match_discipline, "yoga")                  # unchanged
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("choose a class type" in m and "left unchanged" in m for m in msgs), msgs)

    def test_add_week_and_remove_week(self):
        self.post(action="add_week")
        self.assertEqual(sorted(self.program.weeks.values_list("number", flat=True)), [1, 2])
        week2 = self.program.weeks.get(number=2)
        self.post(delete_week=str(week2.pk))
        self.assertEqual(list(self.program.weeks.values_list("number", flat=True)), [1])

    def test_cannot_remove_a_week_that_has_completions(self):
        self.post(action="add_week")
        week2 = self.program.weeks.get(number=2)
        run = ProgramRun.objects.create(program=self.program, start_date=date(2026, 9, 14))
        RunWeek.objects.create(run=run, program_week=week2, sequence=1)
        self.post(delete_week=str(week2.pk))
        self.assertEqual(self.program.weeks.count(), 2)

    def test_cannot_remove_the_only_week(self):
        self.post(delete_week=str(self.week.pk))
        self.assertTrue(ProgramWeek.objects.filter(pk=self.week.pk).exists())

    def test_edit_page_renders_current_state(self):
        req = RequestFactory().get(self.url)
        req.user, req.session = self.user, {}
        req._messages = FallbackStorage(req)
        from workouts.program_views import program_edit
        html = program_edit(req, "split").content.decode()
        self.assertIn("Yoga (any class)", html)
        self.assertIn(f'name="slot_{self.push.pk}_ride_id"', html)
        self.assertIn(RIDE_A, html)
        self.assertIn("RECOVERY TRACKING", html)


class OtherViewTests(ProgramConfigBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user("t", password="x"))

    def test_duplicate_view_redirects_to_the_copys_editor(self):
        resp = self.client.post(reverse("program_duplicate", args=["split"]), {"name": "My Variant"}, SERVER_NAME="localhost")
        copy = Program.objects.get(name="My Variant")
        self.assertRedirects(resp, reverse("program_edit", args=[copy.slug]), fetch_redirect_response=False)
        self.assertEqual(ProgramSlot.objects.get(week__program=copy, title="Push").peloton_ride_id, "")

    def test_blank_program(self):
        resp = self.client.post(reverse("program_new_blank"), {"name": "Summer Block", "kind": "plan"}, SERVER_NAME="localhost")
        p = Program.objects.get(name="Summer Block")
        self.assertEqual((p.kind, p.weeks.count(), ProgramSlot.objects.filter(week__program=p).count()), ("plan", 1, 0))
        self.assertRedirects(resp, reverse("program_edit", args=[p.slug]), fetch_redirect_response=False)
        self.client.post(reverse("program_new_blank"), {"name": "Summer Block"}, SERVER_NAME="localhost")   # name clash
        self.assertEqual(Program.objects.filter(name="Summer Block").count(), 2)
        self.assertEqual(len({p.slug for p in Program.objects.filter(name="Summer Block")}), 2)

    def test_importer_create_stage_builds_ride_and_any_class_slots(self):
        resp = self.client.post(reverse("program_new_plan"), {
            "stage": "create", "name": "Imported", "instructor": "Robin", "kind": "",
            "include": ["0", "1"],
            "week": ["1", "1"], "day": ["1", "3"], "order": ["0", "0"],
            "title": ["45 min Push + Run", "Pilates (any class)"],
            "discipline": ["circuit", "strength"], "duration": ["45", ""],
            "ride_id": [RIDE_A, ""], "match": ["ride", "any"], "preset": ["", "pilates"],
        }, SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 302)
        p = Program.objects.get(name="Imported")
        self.assertEqual(p.kind, "split")                                      # single week -> split
        slots = {s.title: s for s in ProgramSlot.objects.filter(week__program=p)}
        self.assertEqual(slots["45 min Push + Run"].peloton_ride_id, RIDE_A)
        pil = slots["Pilates (any class)"]
        self.assertEqual((pil.match_discipline, pil.match_title_keyword, pil.peloton_ride_id), ("strength", "pilates", ""))
