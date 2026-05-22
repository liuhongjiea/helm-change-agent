"""
Calls the internal Anthropic gateway (model.mify.ai.srv/anthropic).
Uses native /v1/messages protocol. Does NOT send temperature (breaks opus-4-7).
Retries on 429 / 5xx up to 5 times.
"""

from __future__ import annotations

import os
import time

import httpx

from .prompts import SYSTEM_PROMPT, build_user_prompt


def analyze(
    component: str,
    cluster: str,
    namespace: str,
    diff_text: str,
    summary: dict,
) -> str:
    """Returns the AI analysis as a markdown string."""
    base_url = os.getenv("ANTHROPIC_BASE_URL", "http://model.mify.ai.srv/anthropic").rstrip("/")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")
    max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))

    endpoint = f"{base_url}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
    }

    user_content = build_user_prompt(component, cluster, namespace, diff_text, summary)
    # Internal gateway doesn't support top-level "system" field.
    # Merge system prompt into the user message (same semantics, wider compatibility).
    combined_content = SYSTEM_PROMPT + "\n\n---\n\n" + user_content
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": combined_content}],
        # No temperature/top_p — opus-4-7 rejects them
    }

    last_err: Exception | None = None
    for attempt in range(5):
        if attempt > 0:
            backoff = min(10 * (2 ** (attempt - 1)), 60)
            time.sleep(backoff)

        try:
            with httpx.Client(timeout=300.0) as client:
                resp = client.post(endpoint, json=payload, headers=headers)

            if resp.status_code in (429,) or resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:500]}")
                continue

            resp.raise_for_status()
            data = resp.json()

            if data.get("error"):
                raise RuntimeError(f"API error: {data['error'].get('message', data['error'])}")

            content_blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            return text.strip()

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_err = e
            continue
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"AI request failed: {e}") from e

    raise RuntimeError(f"AI request failed after 5 attempts: {last_err}")
