"""Ownership helpers. Every user-facing row belongs to one user; the owner is
Megan's account (the first superuser), which owns all pre-multi-user data."""
from django.contrib.auth import get_user_model
from django.db import models


def get_owner():
    """Megan's account: the first superuser by pk. Raises if none exists."""
    User = get_user_model()
    owner = User.objects.filter(is_superuser=True).order_by("pk").first()
    if owner is None:
        raise RuntimeError("No superuser exists. Run `manage.py createsuperuser` first.")
    return owner


class UserOwnedQuerySet(models.QuerySet):
    def for_user(self, user):
        # No user (or an anonymous one) sees nothing — never "everything".
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        return self.filter(user=user)


UserOwnedManager = models.Manager.from_queryset(UserOwnedQuerySet)
