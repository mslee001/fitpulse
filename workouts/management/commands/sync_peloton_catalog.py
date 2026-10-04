"""Sync the shared Peloton class catalog (PelotonClass) with a user's Peloton connection.

    venv/bin/python3 manage.py sync_peloton_catalog [--user USERNAME] [--category running ...] [--full]

Incremental by default (stops each category at the first page of known classes);
--full walks every page and marks classes Peloton removed as unavailable.
"""
from django.core.management.base import BaseCommand, CommandError

from workouts.catalog import CATALOG_CATEGORIES, sync_catalog
from workouts.management.user_arg import add_user_argument, resolve_user
from workouts.services.peloton_client import PelotonAuthError


class Command(BaseCommand):
    help = "Sync the shared Peloton class catalog"

    def add_arguments(self, parser):
        add_user_argument(parser)
        parser.add_argument("--category", action="append", choices=CATALOG_CATEGORIES,
                            help="Only this browse category (repeatable). Default: all.")
        parser.add_argument("--full", action="store_true", help="Every page, and mark removed classes unavailable")

    def handle(self, *args, **opts):
        user = resolve_user(opts)
        try:
            result = sync_catalog(user, categories=opts["category"], full=opts["full"])
        except PelotonAuthError as e:
            raise CommandError(str(e))
        for cat, s in result["categories"].items():
            line = (f"{cat:<11} pages={s['pages']:<4} created={s['created']:<6} updated={s['updated']:<6} "
                    f"unavailable_skipped={s['skipped_unavailable']}")
            if "marked_unavailable" in s:
                line += f" marked_unavailable={s['marked_unavailable']}"
            if s["error"]:
                self.stdout.write(self.style.ERROR(f"{line}  ERROR: {s['error']}"))
            else:
                self.stdout.write(line)
        self.stdout.write(self.style.SUCCESS(
            f"{result['total_classes']} available classes · {result['class_types']} class types"))
