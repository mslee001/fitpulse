"""Single chokepoint for Anthropic API calls."""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"

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


def call(prompt, *, model=HAIKU, max_tokens=400, system=None, timeout=30, message_content=None):
    """Send a single message. Returns the text response or raises."""
    content = message_content if message_content is not None else prompt
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = system
    _disable_thinking(model, body)
    resp = requests.post(_BASE_URL, headers=_headers(), json=body, timeout=timeout)
    resp.raise_for_status()
    return extract_text(resp.json()["content"]).strip()


def call_json(prompt, **kwargs):
    """Same as call() but strips ```json fences and parses. Raises ValueError on bad JSON."""
    text = call(prompt, **kwargs)
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def submit_batch(custom_id, prompt, *, model=SONNET, max_tokens=1024, system=None):
    """Submit a one-request batch. Returns the batch ID."""
    params = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
    if system:
        params["system"] = system
    _disable_thinking(model, params)
    resp = requests.post(_BATCH_URL, headers=_headers(),
                         json={"requests": [{"custom_id": custom_id, "params": params}]},
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
