"""Shared --user option for management commands that read or write user data."""
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError

from workouts.users import get_owner


def add_user_argument(parser):
    parser.add_argument("--user", metavar="USERNAME",
                        help="Username to act as (default: the owner / first superuser)")


def resolve_user(opts):
    username = opts.get("user")
    if not username:
        return get_owner()
    try:
        return get_user_model().objects.get(username=username)
    except get_user_model().DoesNotExist:
        raise CommandError(f"No user named {username!r}")
