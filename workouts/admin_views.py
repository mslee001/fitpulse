"""/settings/users/ — the owner manages household accounts: features, AI budget,
temporary passwords, onboarding and connection status.

Privacy rule: these pages show account, feature, AI-spend, onboarding and
connection status only. They never query or display anyone's workouts, daily
stats, body, nutrition, intervention, symptom or AI-output data
(test_admin_users enforces this)."""
import secrets
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model, views as auth_views
from django.db.models import Count, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import llm
from .access import AI_FEATURES, FEATURES, GROUPS, access_for
from .models import (
    AIUsage, GoogleHealthAuth, Integration, PelotonAuth, SyncJob, WebhookError, WithingsAuth,
)
from .onboarding import steps_for

User = get_user_model()

PRESETS = {
    "nothing": ("Nothing", []),
    "nutrition": ("Nutrition only", ["nutrition", "ai_food_parse"]),
    "training": ("Training only", ["training", "programs", "strength"]),
    "no_ai": ("Everything except AI", [s for s, f in FEATURES.items() if not f["ai"]]),
    "everything": ("Everything", list(FEATURES)),
}


def _owner_only(view):
    """Defense in depth on top of the middleware's ADMIN_URL_NAMES check."""
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden("Owner only.")
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def _temp_password():
    return secrets.token_urlsafe(9)   # 12 URL-safe characters


def _onboarding_summary(user):
    access = access_for(user)
    if user.is_superuser or access.onboarding_completed_at:
        return "Complete"
    steps = [s for s in steps_for(user) if s.status != "skipped"]
    done = sum(1 for s in steps if s.status == "done")
    if done == 0 or (done == 1 and steps and steps[0].key == "password"):
        return "Not started"
    return f"In progress ({done} of {len(steps)})"


def _gh_freshness(auth):
    """"ok" / "amber" / "red" for the Google Health 7-day refresh-token clock."""
    if auth is None:
        return None
    days = auth.days_since_connected
    return "red" if days >= 7 else "amber" if days >= 6 else "ok"


def _connections(user):
    """Per-integration status — whether connected, never what was synced."""
    gh = GoogleHealthAuth.for_user(user)
    auths = {"peloton": PelotonAuth.for_user(user), "withings": WithingsAuth.for_user(user), "google_health": gh}
    rows = []
    for integration in Integration.objects.for_user(user):
        if integration.key == "garmin" and not user.is_superuser:
            continue
        auth = auths.get(integration.key)
        job = SyncJob.objects.for_user(user).filter(source=integration.key).order_by("-started_at").first()
        rows.append({
            "integration": integration,
            "connected": auth is not None if integration.key != "garmin" else integration.is_authenticated,
            "gh_days": gh.days_since_connected if integration.key == "google_health" and gh else None,
            "gh_freshness": _gh_freshness(gh) if integration.key == "google_health" else None,
            "webhook_active": auths["withings"].webhook_subscription_active
            if integration.key == "withings" and auths["withings"] else None,
            "peloton_username": auth.peloton_username if integration.key == "peloton" and auth else "",
            "job": job.refreshed() if job else None,
        })
    return rows


def _ai_month(user):
    access = access_for(user)
    spent = llm.spent_this_month(user)
    if not user.is_superuser and not access.ai_enabled:
        label = "off"
    elif user.is_superuser or access.monthly_ai_budget_usd is None:
        label = f"${spent:.2f} / no cap"
    else:
        label = f"${spent:.2f} / ${access.monthly_ai_budget_usd:.2f}"
    return spent, label


def _feature_row(user, slug, access=None):
    access = access or access_for(user)
    f = FEATURES[slug]
    granted = set(access.features or [])
    missing = [FEATURES[r]["label"] for r in f["requires"] if r not in granted]
    return {"slug": slug, "label": f["label"], "note": f["note"], "ai": f["ai"],
            "granted": slug in granted, "needs": missing}


@_owner_only
def admin_users(request):
    rows = []
    for u in User.objects.order_by("-is_superuser", "username"):
        access = access_for(u)
        gh = GoogleHealthAuth.for_user(u)
        dots = [
            ("Peloton", "ok" if PelotonAuth.for_user(u) else None),
            ("Withings", "ok" if WithingsAuth.for_user(u) else None),
            ("Google Health", _gh_freshness(gh)),
        ]
        if u.is_superuser:
            garmin = Integration.objects.for_user(u).filter(key="garmin", is_authenticated=True).exists()
            dots.append(("Garmin", "ok" if garmin else None))
        rows.append({
            "user": u,
            "setup": _onboarding_summary(u),
            "features": len(FEATURES) if u.is_superuser else len([s for s in access.features if s in FEATURES]),
            "ai": _ai_month(u)[1],
            "dots": dots,
        })
    return render(request, "workouts/admin_users.html", {"rows": rows, "feature_total": len(FEATURES)})


@_owner_only
def admin_user_new(request):
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        first_name = (request.POST.get("first_name") or "").strip()[:150]
        preset = request.POST.get("preset") if request.POST.get("preset") in PRESETS else "nothing"
        error = None
        if not username:
            error = "Username is required."
        elif User.objects.filter(username__iexact=username).exists():
            error = f'"{username}" is already taken.'
        if error:
            return render(request, "workouts/admin_user_new.html",
                          {"presets": PRESETS, "error": error, "username": username,
                           "first_name": first_name, "preset": preset})
        password = _temp_password()
        user = User.objects.create_user(username, password=password, first_name=first_name)
        features = list(PRESETS[preset][1])
        access = access_for(user)   # created by the post_save signal
        access.features = features
        access.ai_enabled = any(s in AI_FEATURES for s in features)
        access.must_change_password = True
        access.save()
        Integration.ensure_for_user(user)
        # Shown once in this response only — never stored, logged or put in messages.
        return render(request, "workouts/admin_user_password.html",
                      {"target": user, "password": password, "is_new": True})
    return render(request, "workouts/admin_user_new.html", {"presets": PRESETS, "preset": "nothing"})


@_owner_only
def admin_user_detail(request, pk):
    target = get_object_or_404(User, pk=pk)
    access = access_for(target)
    groups = [(g, [_feature_row(target, s, access) for s, f in FEATURES.items() if f["group"] == g])
              for g in GROUPS]
    usage = list(
        AIUsage.objects.for_user(target).filter(created_at__gte=llm.month_start())
        .values("feature")
        .annotate(calls=Count("id"), tokens_in=Sum("input_tokens"), tokens_out=Sum("output_tokens"),
                  cost=Sum("cost_usd"))
        .order_by("-cost")
    )
    for row in usage:
        row["label"] = FEATURES.get(row["feature"], {}).get("label", row["feature"])
    spent, ai_label = _ai_month(target)
    from datetime import timedelta
    error_cutoff = timezone.now() - timedelta(days=WebhookError.RETENTION_DAYS)
    return render(request, "workouts/admin_user_detail.html", {
        "target": target,
        "access": access,
        "groups": groups,
        "usage": usage,
        "spent": spent,
        "ai_label": ai_label,
        "steps": steps_for(target) if not target.is_superuser else [],
        "setup": _onboarding_summary(target),
        "connections": _connections(target),
        "webhook_errors": WebhookError.objects.for_user(target).filter(created_at__gte=error_cutoff).count(),
        "can_deactivate": _can_deactivate(request.user, target),
    })


def _can_deactivate(actor, target):
    if target == actor:
        return False
    if target.is_superuser and target.is_active and \
            User.objects.filter(is_superuser=True, is_active=True).count() <= 1:
        return False
    return True


@_owner_only
@require_POST
def admin_user_feature_toggle(request, pk, slug):
    target = get_object_or_404(User, pk=pk)
    if slug not in FEATURES:
        return HttpResponseForbidden("Unknown feature.")
    access = access_for(target)
    features = [s for s in access.features if s != slug]
    if slug not in access.features:
        features.append(slug)
    access.features = features
    access.save(update_fields=["features", "updated_at"])
    return render(request, "workouts/partials/admin_feature_row.html",
                  {"target": target, "row": _feature_row(target, slug, access)})


@_owner_only
@require_POST
def admin_user_ai(request, pk):
    target = get_object_or_404(User, pk=pk)
    access = access_for(target)
    error = None
    access.ai_enabled = request.POST.get("ai_enabled") == "on"
    raw = (request.POST.get("monthly_ai_budget_usd") or "").strip().lstrip("$")
    if raw:
        try:
            budget = Decimal(raw).quantize(Decimal("0.01"))
            if budget < 0:
                raise InvalidOperation
            access.monthly_ai_budget_usd = budget
        except (InvalidOperation, ValueError):
            error = f"Couldn't read {raw!r} as a dollar amount."
    else:
        access.monthly_ai_budget_usd = None
    access.save()
    spent, ai_label = _ai_month(target)
    return render(request, "workouts/partials/admin_ai_form.html",
                  {"target": target, "access": access, "spent": spent, "ai_label": ai_label,
                   "error": error, "saved": error is None})


@_owner_only
@require_POST
def admin_user_reset_password(request, pk):
    target = get_object_or_404(User, pk=pk)
    password = _temp_password()
    target.set_password(password)
    target.save(update_fields=["password"])
    access = access_for(target)
    access.must_change_password = True
    access.save(update_fields=["must_change_password", "updated_at"])
    return render(request, "workouts/admin_user_password.html",
                  {"target": target, "password": password, "is_new": False})


@_owner_only
@require_POST
def admin_user_active(request, pk):
    target = get_object_or_404(User, pk=pk)
    if target.is_active and not _can_deactivate(request.user, target):
        messages.error(request, "You can't deactivate yourself or the last active owner account.")
    else:
        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        messages.success(request, f"{target.username} is now {'active' if target.is_active else 'deactivated'}.")
    return redirect("admin_user_detail", pk=target.pk)


@_owner_only
@require_POST
def admin_user_reset_onboarding(request, pk):
    target = get_object_or_404(User, pk=pk)
    access = access_for(target)
    access.onboarding_completed_at = None
    access.save(update_fields=["onboarding_completed_at", "updated_at"])
    messages.success(request, f"{target.username} will see Get Started again on their next visit.")
    return redirect("admin_user_detail", pk=target.pk)


@_owner_only
@require_POST
def admin_user_gh_test_user(request, pk):
    target = get_object_or_404(User, pk=pk)
    access = access_for(target)
    access.google_test_user_added = not access.google_test_user_added
    access.save(update_fields=["google_test_user_added", "updated_at"])
    return render(request, "workouts/partials/admin_gh_test_user.html", {"target": target, "access": access})


class PasswordChangeView(auth_views.PasswordChangeView):
    """Django's password change, clearing the forced-change flag on success."""
    template_name = "registration/password_change.html"
    success_url = reverse_lazy("password_change_done")

    def form_valid(self, form):
        response = super().form_valid(form)
        access = access_for(self.request.user)
        if access.must_change_password:
            access.must_change_password = False
            access.save(update_fields=["must_change_password", "updated_at"])
        return response


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"
