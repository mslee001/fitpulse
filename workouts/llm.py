"""Single chokepoint for Anthropic API calls.

Every call names the user it runs for and the feature it serves (both
required keyword arguments). guard() checks that feature is allowed and the
user's monthly budget isn't spent before any request goes out, and each
response's token usage is logged to AIUsage."""
import json
import re
import logging
import os
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"

# USD per 1M tokens: (input, output, cache_write, cache_read). Batch = 50% of these.
# Checked against Anthropic's published pricing 2026-10-03 — re-check on price changes.
MODEL_PRICES = {
    HAIKU:  (1.00, 5.00, 1.25, 0.10),
    SONNET: (2.00, 10.00, 2.50, 0.20),
}

_BASE_URL  = "https://api.anthropic.com/v1/messages"
_BATCH_URL = "https://api.anthropic.com/v1/messages/batches"


def _headers(api_key=None):
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    return {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }


def extract_text(content_blocks):
    """
    Pull the text block out of a response's content list. Sonnet 5 prepends a
    "thinking" block ahead of the "text" block by default (Haiku doesn't), so the
    text is not reliably at index 0 — search for the block with type "text"
    instead of assuming position.
    """
    block = next((b for b in content_blocks if b.get("type") == "text"), None)
    if block is None:
        raise ValueError(f"No text block found in response content: {content_blocks}")
    return block["text"]


def _disable_thinking(model, body):
    """
    Sonnet 5 runs adaptive thinking by default whenever `thinking` is omitted
    (Sonnet 4.6 ran without thinking by default), and thinking tokens count
    against max_tokens — silently truncating the visible response on prompts
    sized for a fixed budget. None of this app's prompts need extended
    reasoning, so keep it off explicitly. Skip for Haiku, which doesn't
    support the thinking family of models at all.
    """
    if model != HAIKU:
        body["thinking"] = {"type": "disabled"}


class AIBudgetExceeded(Exception):
    """This user has spent their monthly AI budget."""


class AIFeatureDenied(Exception):
    """This AI feature isn't turned on for this user."""


class AIDemoOff(AIFeatureDenied):
    """The read-only demo user never calls the AI live (workouts/demo.py)."""


def _monthly_budget(user):
    """The user's monthly cap in USD, or None for no cap."""
    from .access import access_for
    return access_for(user).monthly_ai_budget_usd


def month_start():
    """Midnight on the 1st of the current month, America/Los_Angeles."""
    from django.utils import timezone
    now = timezone.localtime()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(user):
    from django.db.models import Sum
    from .models import AIUsage
    total = AIUsage.objects.for_user(user).filter(created_at__gte=month_start()).aggregate(t=Sum("cost_usd"))["t"]
    return total or Decimal("0")


def guard(user, feature):
    """Raise before any API call if this user may not use this feature now.
    Superusers are never capped (their usage is still logged)."""
    from .access import has_feature
    from .demo import ai_generation_allowed, is_demo
    if is_demo(user) and not ai_generation_allowed():
        raise AIDemoOff(feature)
    if not has_feature(user, feature):
        raise AIFeatureDenied(feature)
    if user.is_superuser:
        return
    budget = _monthly_budget(user)
    if budget is not None and spent_this_month(user) >= Decimal(budget):
        raise AIBudgetExceeded(feature)


def cost_usd(model, usage, is_batch=False):
    prices = MODEL_PRICES.get(model)
    if prices is None:
        logger.warning("No price for model %r — costing at the Sonnet rate", model)
        prices = MODEL_PRICES[SONNET]
    p_in, p_out, p_write, p_read = prices
    total = (
        (usage.get("input_tokens") or 0) * p_in
        + (usage.get("output_tokens") or 0) * p_out
        + (usage.get("cache_creation_input_tokens") or 0) * p_write
        + (usage.get("cache_read_input_tokens") or 0) * p_read
    ) / 1_000_000
    if is_batch:
        total /= 2
    return Decimal(str(round(total, 6)))


def log_usage(user, feature, model, usage, is_batch=False):
    """Record one response's token usage. Never raises into the caller."""
    try:
        from .models import AIUsage
        usage = usage or {}
        AIUsage.objects.create(
            user=user, feature=feature, model=model or "",
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
            cache_write_tokens=usage.get("cache_creation_input_tokens") or 0,
            is_batch=is_batch,
            cost_usd=cost_usd(model, usage, is_batch),
        )
    except Exception:
        logger.exception("AI usage logging failed (user=%s feature=%s)", getattr(user, "pk", None), feature)


def _send(prompt, *, user, feature, model, max_tokens, system, timeout, message_content):
    guard(user, feature)
    content = message_content if message_content is not None else prompt
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    _disable_thinking(model, body)
    resp = requests.post(_BASE_URL, headers=_headers(), json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    log_usage(user, feature, data.get("model") or model, data.get("usage"))
    return data


def call(prompt, *, user, feature, model=HAIKU, max_tokens=400, system=None, timeout=30, message_content=None):
    """Send a single message. Returns the text response or raises."""
    data = _send(prompt, user=user, feature=feature, model=model, max_tokens=max_tokens, system=system,
                 timeout=timeout, message_content=message_content)
    return extract_text(data["content"]).strip()


def call_raw(body, *, user, feature, timeout=30):
    """
    Send an arbitrary request body (for multi-turn / tool-use conversations
    where the caller needs the full response, not just the extracted text).
    Fills in thinking-disable but leaves model/system/tools/messages to the
    caller. Returns the parsed response JSON.
    """
    guard(user, feature)
    _disable_thinking(body.get("model", SONNET), body)
    resp = requests.post(_BASE_URL, headers=_headers(), json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    log_usage(user, feature, data.get("model") or body.get("model"), data.get("usage"))
    return data


class AIBadJSON(ValueError):
    """The model's reply couldn't be read as JSON. Carries the reply text (for
    WebhookError detail — never shown to users) and the stop reason."""

    def __init__(self, message, text="", stop_reason=""):
        super().__init__(message)
        self.text, self.stop_reason = text, stop_reason


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_text(text, expect=None):
    """Parse a model reply as JSON, tolerating a ```json fence anywhere and prose
    before or after the JSON (models sometimes add "Here's the plan:" despite
    being told not to). With expect=dict (or list), only a value of that type
    counts — a list-looking fragment in the prose ("days [1, 3, 5]") is skipped,
    and each "{" is tried in turn. Raises ValueError when nothing fits."""
    text = (text or "").strip()

    def ok(v):
        return expect is None or isinstance(v, expect)
    try:
        v = json.loads(text)
        if ok(v):
            return v
    except ValueError:
        pass
    m = _FENCE_RE.search(text)
    if m:
        try:
            v = json.loads(m.group(1).strip())
            if ok(v):
                return v
        except ValueError:
            pass
    openers = "{" if expect is dict else "[" if expect is list else "{["
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in openers:
            try:
                v, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if ok(v):
                return v
    raise ValueError(f"no JSON {expect.__name__ if expect else 'value'} found in the reply")


def call_json(prompt, *, user, feature, model=HAIKU, max_tokens=400, system=None, timeout=30,
              message_content=None, expect=None):
    """Same as call() but parses the reply as JSON (see parse_json_text; pass
    expect=dict to require an object). Raises AIBadJSON (a ValueError) when it
    can't — including when the reply was cut off by max_tokens."""
    data = _send(prompt, user=user, feature=feature, model=model, max_tokens=max_tokens, system=system,
                 timeout=timeout, message_content=message_content)
    text = extract_text(data["content"]).strip()
    stop = data.get("stop_reason") or ""
    try:
        return parse_json_text(text, expect=expect)
    except ValueError as e:
        if stop == "max_tokens":
            raise AIBadJSON("The AI's reply was cut off before it finished.", text, stop) from e
        raise AIBadJSON(f"The AI's reply wasn't valid JSON ({e}).", text, stop) from e


def submit_batch(custom_id, prompt, *, user, feature, model=SONNET, max_tokens=1024, system=None):
    """Submit a one-request batch. Returns the batch ID. Usage is logged when
    the result is stored (see log_batch_result), not here."""
    guard(user, feature)
    params = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    _disable_thinking(model, params)
    resp = requests.post(_BATCH_URL, headers=_headers(),
                         json={"requests": [{"custom_id": f"u{user.id}-{feature}-{custom_id}", "params": params}]},
                         timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def get_batch_status(batch_id):
    """Returns the raw batch dict; caller checks processing_status."""
    resp = requests.get(f"{_BATCH_URL}/{batch_id}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_batch_results(batch_id):
    """Iterate JSONL result rows."""
    resp = requests.get(f"{_BATCH_URL}/{batch_id}/results", headers=_headers(), timeout=30)
    resp.raise_for_status()
    for line in resp.iter_lines():
        if line:
            yield json.loads(line)


def parse_custom_id(custom_id):
    """(user_id, feature) from a "u{id}-{feature}-..." batch custom_id, or (None, None)."""
    if not custom_id or not custom_id.startswith("u"):
        return None, None
    head, _, rest = custom_id.partition("-")
    feature = rest.partition("-")[0]
    try:
        return int(head[1:]), feature or None
    except ValueError:
        return None, None


def log_batch_result(row):
    """Log usage for one succeeded batch result row, attributing it to the user
    and feature encoded in its custom_id. Call it where the caller stores the
    result (each batch is stored once, then its id is cleared)."""
    try:
        from django.contrib.auth import get_user_model
        user_id, feature = parse_custom_id(row.get("custom_id"))
        if user_id is None:
            return
        user = get_user_model().objects.filter(pk=user_id).first()
        message = (row.get("result") or {}).get("message") or {}
        if user is not None:
            log_usage(user, feature, message.get("model"), message.get("usage"), is_batch=True)
    except Exception:
        logger.exception("Batch usage logging failed")
