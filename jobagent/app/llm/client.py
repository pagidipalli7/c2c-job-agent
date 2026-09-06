"""Single entry point for every LLM call.

- tier "haiku"  -> classification / extraction / scoring (cheap, fast)
- tier "sonnet" -> resume tailoring, custom-question answering
- LLM_MODE=mock -> deterministic heuristic responders (tests, offline dry-runs); no network.

`json_call` returns parsed JSON: strips ``` fences, retries once on invalid JSON.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from app.config import get_settings
from app.logging import get_logger

log = get_logger("llm")

MockHandler = Callable[[str, str], dict]
_mock_handlers: dict[str, MockHandler] = {}


class LLMError(RuntimeError):
    pass


class LLMAuthError(LLMError):
    """401/403 from the API: the key is missing, invalid, revoked, or the account has no billing. Never retry."""


def register_mock(purpose: str, handler: MockHandler) -> None:
    """Modules register a deterministic responder per purpose (score_job, tailor_resume, ...)."""
    _mock_handlers[purpose] = handler


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_text(text: str) -> Any:
    """Tolerant JSON parse: strips fences and leading/trailing prose around the outermost {...} or [...]."""
    t = _FENCE.sub("", (text or "").strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise LLMError(f"no JSON object in response: {text[:200]!r}")
    end = max(t.rfind("}"), t.rfind("]"))
    if end <= start:
        raise LLMError(f"unterminated JSON in response: {text[:200]!r}")
    try:
        return json.loads(t[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON in response: {e}: {text[:200]!r}") from e


class LLMClient:
    def __init__(self, mode: str | None = None):
        settings = get_settings()
        self.mode = mode or settings.effective_llm_mode
        self.models = {"haiku": settings.haiku_model, "sonnet": settings.sonnet_model}
        self._client = None
        if self.mode == "live":
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, max_retries=3)

    # ---------------------------------------------------------------- raw
    def complete(self, tier: str, system: str, user: str, *, purpose: str, max_tokens: int = 4096) -> str:
        if self.mode == "mock":
            handler = _mock_handlers.get(purpose)
            if handler is None:
                raise LLMError(f"no mock handler registered for purpose={purpose!r}")
            return json.dumps(handler(system, user))
        import anthropic

        try:
            resp = self._client.messages.create(
                model=self.models[tier],
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e}") from e
        except anthropic.AuthenticationError as e:
            raise LLMAuthError(f"Anthropic rejected the API key ({e.status_code}): check ANTHROPIC_API_KEY in .env and billing at console.anthropic.com") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMAuthError(f"Anthropic API key lacks permission ({e.status_code}): {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"api error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"connection error: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("model refused the request")
        text = "".join(b.text for b in resp.content if b.type == "text")
        log.debug("llm_call", tier=tier, purpose=purpose, input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)
        return text

    def json_call(self, tier: str, system: str, user: str, *, purpose: str, max_tokens: int = 4096) -> Any:
        text = self.complete(tier, system, user, purpose=purpose, max_tokens=max_tokens)
        try:
            return parse_json_text(text)
        except LLMError as e:
            log.warning("llm_invalid_json_retry", purpose=purpose, error=str(e))
            retry_user = user + "\n\nYour previous reply was not valid JSON. Reply with ONLY a single JSON object, no prose, no code fences."
            text = self.complete(tier, system, retry_user, purpose=purpose, max_tokens=max_tokens)
            return parse_json_text(text)


_default: LLMClient | None = None


def get_llm() -> LLMClient:
    global _default
    if _default is None or _default.mode != get_settings().effective_llm_mode:
        _default = LLMClient()
    return _default
