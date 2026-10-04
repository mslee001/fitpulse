"""Peloton class catalog sync — the shared library of on-demand classes that
AI training plans pick from.

Global data (PelotonClass / PelotonClassType / PelotonInstructor have no user):
it's Peloton's public library, synced with the owner's Peloton connection.

Paging rules (verified against the live API 2026-10-03): browse_category must
be the lowercase slug; pages are capped at 100 items and page_count is computed
from the *requested* limit, so we always ask for 100 and stop on a short page
or once page >= ceil(total / 100).
"""
import logging
import math
import time
from datetime import datetime, timezone as dt_timezone

from django.db.models import Max
from django.utils import timezone

from .models import PelotonClass, PelotonClassType, PelotonInstructor
from .services.peloton_client import (
    CATALOG_PAGE_SIZE, PelotonAuthError, PelotonClient,
)

logger = logging.getLogger(__name__)

CATALOG_CATEGORIES = ["running", "walking", "strength", "stretching", "pilates", "cycling", "yoga"]

# Every column the sync writes on an existing row (ride_id is the conflict key).
_UPDATE_FIELDS = [
    "title", "description", "discipline", "categories", "class_type_id", "class_type_ids",
    "instructor_id", "instructor_name", "duration_seconds", "length_seconds",
    "difficulty_estimate", "difficulty_level", "difficulty_rating_count",
    "overall_rating_avg", "overall_rating_count", "original_air_time", "is_outdoor",
    "has_tread_pace_target", "equipment_tags", "is_explicit", "language", "image_url",
    "is_available", "last_seen_at",
]


def _merge_categories(existing: str, category: str) -> str:
    cats = [c for c in (existing or "").split(",") if c]
    if category not in cats:
        cats.append(category)
    return f",{','.join(cats)}," if cats else ""


def _upsert_class_types(items):
    rows = [PelotonClassType(id=t["id"], name=t.get("name") or "", display_name=t.get("display_name") or "",
                             discipline=t.get("fitness_discipline") or "", is_active=bool(t.get("is_active", True)))
            for t in items or [] if t.get("id")]
    if rows:
        PelotonClassType.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["id"],
            update_fields=["name", "display_name", "discipline", "is_active", "updated_at"])
    return len(rows)


def _upsert_instructors(items):
    rows = [PelotonInstructor(id=i["id"], name=i.get("name") or "") for i in items or [] if i.get("id")]
    if rows:
        PelotonInstructor.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["id"], update_fields=["name", "updated_at"])
    return {r.id: r.name for r in rows}


def _class_row(item, category, existing_categories, instructor_names, seen_at):
    air = item.get("original_air_time")
    return PelotonClass(
        ride_id=item["id"],
        title=(item.get("title") or "")[:255],
        description=item.get("description") or "",
        discipline=item.get("fitness_discipline") or "",
        categories=_merge_categories(existing_categories, category),
        class_type_id=item.get("ride_type_id") or "",
        class_type_ids=item.get("class_type_ids") or [],
        instructor_id=item.get("instructor_id") or "",
        instructor_name=(instructor_names.get(item.get("instructor_id")) or "")[:120],
        duration_seconds=int(item.get("duration") or 0),
        length_seconds=item.get("length"),
        difficulty_estimate=item.get("difficulty_estimate"),
        difficulty_level=item.get("difficulty_level") or "",
        difficulty_rating_count=int(item.get("difficulty_rating_count") or 0),
        overall_rating_avg=item.get("overall_rating_avg"),
        overall_rating_count=int(item.get("overall_rating_count") or 0),
        original_air_time=datetime.fromtimestamp(air, tz=dt_timezone.utc) if air else seen_at,
        is_outdoor=bool(item.get("is_outdoor")),
        has_tread_pace_target=bool(item.get("has_tread_pace_target")),
        equipment_tags=item.get("equipment_tags") or [],
        is_explicit=bool(item.get("is_explicit")),
        language=(item.get("language") or "")[:20],
        image_url=(item.get("image_url") or "")[:500],
        is_available=True,
        last_seen_at=seen_at,
    )


def _store_page(data, category, run_started_at, instructor_names):
    """Upsert one page. Returns (created, updated, skipped_unavailable, all_preexisting)."""
    available = [d for d in data if d.get("id") and (d.get("availability") or {}).get("is_available", True)]
    unavailable = [d["id"] for d in data if d.get("id") and d not in available]
    if unavailable:   # still listed but not playable — don't let the picker choose it
        PelotonClass.objects.filter(ride_id__in=unavailable).update(is_available=False)
    skipped = len(unavailable)
    ids = [d["id"] for d in available]
    existing = dict(PelotonClass.objects.filter(ride_id__in=ids).values_list("ride_id", "categories"))
    # "Existed before this run" — rows first stored earlier in this same run
    # (e.g. a pilates class just seen under strength) don't count.
    preexisting = set(PelotonClass.objects.filter(ride_id__in=ids, first_seen_at__lt=run_started_at)
                      .values_list("ride_id", flat=True))
    missing_instr = {d.get("instructor_id") for d in available} - set(instructor_names) - {None, ""}
    if missing_instr:
        instructor_names.update(PelotonInstructor.objects.filter(id__in=missing_instr).values_list("id", "name"))
    rows = [_class_row(d, category, existing.get(d["id"], ""), instructor_names, run_started_at) for d in available]
    if rows:
        PelotonClass.objects.bulk_create(rows, update_conflicts=True, unique_fields=["ride_id"],
                                         update_fields=_UPDATE_FIELDS)
    created = sum(1 for i in ids if i not in existing)
    all_preexisting = bool(ids) and all(i in preexisting for i in ids)
    return created, len(ids) - created, skipped, all_preexisting


def sync_catalog(user, categories=None, full=False, pause=0.2) -> dict:
    """Sync the class catalog from Peloton using `user`'s connection (the owner's).

    Incremental (full=False): newest first, stop a category after the first page
    whose classes all existed before this run — usually one request per category.
    Full: every page; afterwards each category that finished cleanly marks its
    rows not seen this run as unavailable (removed from Peloton's library).
    A network error in one category is recorded and the next still runs;
    PelotonAuthError aborts the whole run."""
    client = PelotonClient(user)
    run_started_at = timezone.now()
    summary = {"categories": {}, "class_types": 0, "total_classes": 0}
    instructor_names = {}
    first_request = True
    for category in categories or CATALOG_CATEGORIES:
        stats = {"pages": 0, "created": 0, "updated": 0, "skipped_unavailable": 0, "error": ""}
        summary["categories"][category] = stats
        page = 0
        try:
            while True:
                if not first_request:
                    time.sleep(pause)
                first_request = False
                resp = client.get_archived_classes(category, page)
                stats["pages"] += 1
                if summary["class_types"] == 0:
                    summary["class_types"] = _upsert_class_types(resp.get("class_types"))
                instructor_names.update(_upsert_instructors(resp.get("instructors")))
                data = resp.get("data") or []
                created, updated, skipped, all_known = _store_page(data, category, run_started_at, instructor_names)
                stats["created"] += created
                stats["updated"] += updated
                stats["skipped_unavailable"] += skipped
                total = int(resp.get("total") or 0)
                page += 1
                if len(data) < CATALOG_PAGE_SIZE or page >= math.ceil(total / CATALOG_PAGE_SIZE):
                    break
                if not full and all_known:
                    break
        except PelotonAuthError:
            raise
        except Exception as e:
            logger.warning("Catalog sync failed for %s on page %s: %s", category, page, e)
            stats["error"] = str(e)[:300]
            continue
        if full:
            gone = (PelotonClass.objects.filter(categories__contains=f",{category},", last_seen_at__lt=run_started_at,
                                                is_available=True)
                    .update(is_available=False))
            stats["marked_unavailable"] = gone
    summary["total_classes"] = PelotonClass.objects.filter(is_available=True).count()
    return summary


def catalog_status() -> dict:
    """{"total", "by_category", "last_synced"} for the Integrations card."""
    qs = PelotonClass.objects.filter(is_available=True)
    by_category = {c: qs.filter(categories__contains=f",{c},").count() for c in CATALOG_CATEGORIES}
    return {"total": qs.count(), "by_category": by_category,
            "last_synced": PelotonClass.objects.aggregate(m=Max("last_seen_at"))["m"]}


# ---------------------------------------------------------------------------
# Difficulty in context
# ---------------------------------------------------------------------------

MIN_RANK_CLASSES = 5    # fewer comparable classes than this → show the raw number only


class DifficultyRanker:
    """Where a class's member-rated difficulty sits among available classes of
    the same type and length — the only comparison that number supports (each
    member rates against their own fitness). Caches one sorted list per
    (class type, duration), so ranking a page of workouts costs a few queries."""

    def __init__(self):
        self._values = {}
        self._type_names = None

    def _sorted(self, tid, seconds):
        key = (tid, round(seconds / 60))
        if key not in self._values:
            self._values[key] = sorted(PelotonClass.objects.filter(
                is_available=True, class_type_id=tid, difficulty_estimate__isnull=False,
                duration_seconds__gte=seconds - 30, duration_seconds__lte=seconds + 30,
            ).values_list("difficulty_estimate", flat=True))
        return self._values[key]

    def _type_name(self, tid):
        if self._type_names is None:
            self._type_names = dict(PelotonClassType.objects.values_list("id", "name"))
        return self._type_names.get(tid, "")

    def rank(self, cls, difficulty=None):
        """{"difficulty", "harder_than", "easier_than", "n", "label", "phrase"} or None.
        `cls` is a PelotonClass; `difficulty` overrides its value (e.g. a workout's own)."""
        value = cls.difficulty_estimate if cls is not None and cls.difficulty_estimate is not None else difficulty
        if value is None:
            return None
        info = {"difficulty": value, "harder_than": None, "easier_than": None, "n": 0, "label": "", "phrase": ""}
        if cls is None or not cls.class_type_id or not cls.duration_seconds:
            return info
        values = self._sorted(cls.class_type_id, cls.duration_seconds)
        n = len(values)
        name = self._type_name(cls.class_type_id)
        if n < MIN_RANK_CLASSES or not name:
            return info
        below = sum(1 for v in values if v < value)
        above = sum(1 for v in values if v > value)
        info.update(n=n, harder_than=round(100 * below / n), easier_than=round(100 * above / n),
                    label=f"{round(cls.duration_seconds / 60)}-min {name} classes")
        if info["harder_than"] >= info["easier_than"]:
            info["phrase"] = f"harder than {info['harder_than']}% of {info['label']}"
        else:
            info["phrase"] = f"easier than {info['easier_than']}% of {info['label']}"
        return info

    def for_rides(self, ride_ids):
        """{ride_id: PelotonClass} for the ones in the catalog."""
        ids = [r for r in set(ride_ids) if r]
        return {c.ride_id: c for c in PelotonClass.objects.filter(ride_id__in=ids)} if ids else {}

    def annotate_workouts(self, workouts):
        """Set `difficulty_info` on each CachedWorkout (None when unknown)."""
        workouts = list(workouts)
        classes = self.for_rides(w.ride_id for w in workouts)
        for w in workouts:
            w.difficulty_info = self.rank(classes.get(w.ride_id), w.difficulty_estimate)
        return workouts
