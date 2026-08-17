"""
security.py
------------
- Cryptographically secure token generation/hashing
- HTML sanitization of AI-generated content before it is ever rendered
- A very small in-memory sliding-window rate limiter for the public
  callback endpoints (swap for Redis in production if you run >1 worker)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

import bleach

# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """256 bits of entropy, URL-safe. This is the ONLY time the raw token
    exists outside the reviewer's inbox -- we hash it immediately and never
    persist or log the raw value."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(candidate_hash: str, stored_hash: str) -> bool:
    """Constant-time comparison to avoid timing side-channels."""
    return hmac.compare_digest(candidate_hash, stored_hash)


def compare_api_key(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate or "", expected or "")


# ---------------------------------------------------------------------------
# HTML sanitization
# ---------------------------------------------------------------------------

# The AI response is untrusted input. It is rendered inside an HTML page
# (the approval page) and inside an HTML email. We never trust it to contain
# safe markup. Two supported modes:
#   - "text" (default): escape everything, render as preformatted text.
#   - "restricted-html": allow a tiny safe subset of tags for readability.

_ALLOWED_TAGS = ["b", "i", "em", "strong", "p", "br", "ul", "ol", "li", "code", "pre"]
_ALLOWED_ATTRS: dict[str, list[str]] = {}


def sanitize_for_html(raw_text: str, allow_restricted_html: bool = False) -> str:
    if allow_restricted_html:
        return bleach.clean(
            raw_text,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            strip=True,
        )
    # Escape everything. bleach.clean with an empty tag allow-list HTML-escapes
    # and strips all markup, neutralizing script tags, event handlers, etc.
    return bleach.clean(raw_text, tags=[], attributes={}, strip=True)


# ---------------------------------------------------------------------------
# Minimal rate limiter (per-process, per-IP sliding window)
# ---------------------------------------------------------------------------

class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        window = self._hits[key]
        while window and now - window[0] > self.window_seconds:
            window.popleft()
        if len(window) >= self.max_requests:
            return False
        window.append(now)
        return True
