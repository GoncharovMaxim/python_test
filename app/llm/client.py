"""Thin wrapper around the Anthropic SDK: rate limiting, error classification, no-key mode."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import typing as t
from dataclasses import dataclass

import anthropic

if t.TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx2

    from app.config import Settings

log = logging.getLogger(__name__)

# Sent in no-key mode so the provider receives a real request (and answers 401).
NO_KEY_PLACEHOLDER = "no-api-key-configured"
STUB_MODEL_NAME = "static-stub"
FALLBACKS_BETA = "server-side-fallback-2026-07-01"


class RateLimiter(t.Protocol):
    async def acquire(self) -> None: ...


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    stub: bool


class LLMError(Exception):
    retryable: bool = False

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RetryableLLMError(LLMError):
    retryable = True


class PermanentLLMError(LLMError):
    retryable = False


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        rate_limiter: RateLimiter,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._limiter = rate_limiter
        self.stub_mode = settings.llm_stub_mode
        kwargs: dict[str, t.Any] = {
            "api_key": settings.anthropic_api_key.strip()
            or NO_KEY_PLACEHOLDER,
            # Retries are ours (job queue + backoff), so each attempt passes the rate limiter.
            "max_retries": 0,
            "timeout": settings.llm_timeout_seconds,
        }
        if settings.anthropic_base_url:
            kwargs["base_url"] = settings.anthropic_base_url

        if http_client:
            kwargs["http_client"] = http_client

        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        schema: Mapping[str, t.Any],
        stub_response: str,
    ) -> LLMResult:
        """One rate-limited request returning JSON text that matches `schema`.

        In no-key mode the request is still sent, but `stub_response` is returned instead
        of the provider's answer, after a random 5-30 s delay (like a real call).
        """
        await self._limiter.acquire()
        request = self._build_request(system=system, user=user, schema=schema)
        if self.stub_mode:
            return await self._stub_call(request, stub_response)

        return await self._real_call(request)

    def _build_request(
        self, *, system: str, user: str, schema: Mapping[str, t.Any]
    ) -> dict[str, t.Any]:
        request: dict[str, t.Any] = {
            "model": self._settings.llm_model,
            "max_tokens": self._settings.llm_max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self._settings.llm_effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        if self._settings.llm_server_fallbacks:
            # On a safety decline the API re-runs the request on Anthropic's recommended
            # fallback model inside the same call.
            request["betas"] = [FALLBACKS_BETA]
            request["fallbacks"] = "default"

        return request

    async def _real_call(self, request: Mapping[str, t.Any]) -> LLMResult:
        try:
            response = await self._client.beta.messages.create(**request)
        except anthropic.RateLimitError as exc:
            raise RetryableLLMError(
                "rate limited by provider", _retry_after(exc)
            ) from exc
        except anthropic.APIStatusError as exc:
            message = (
                f"provider returned HTTP {exc.status_code}: {exc.message}"
            )
            if exc.status_code in (408, 409) or exc.status_code >= 500:
                raise RetryableLLMError(message, _retry_after(exc)) from exc

            raise PermanentLLMError(message) from exc
        except anthropic.APIConnectionError as exc:  # includes APITimeoutError
            raise RetryableLLMError(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            raise PermanentLLMError("model declined to answer (refusal)")

        if response.stop_reason == "max_tokens":
            raise PermanentLLMError("response truncated (max_tokens)")

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        if not text.strip():
            raise RetryableLLMError("empty response")

        return LLMResult(text=text, model=response.model, stub=False)

    async def _stub_call(
        self, request: Mapping[str, t.Any], stub_response: str
    ) -> LLMResult:
        started = time.monotonic()
        target = random.uniform(
            self._settings.llm_stub_min_delay_seconds,
            self._settings.llm_stub_max_delay_seconds,
        )
        try:
            await self._client.with_options(
                timeout=max(target, 1.0)
            ).beta.messages.create(**request)
            log.warning(
                "no-key mode: provider unexpectedly accepted the request; ignoring it"
            )
        except anthropic.APIStatusError as exc:
            log.info(
                "no-key mode: provider answered HTTP %s, using static response",
                exc.status_code,
            )
        except anthropic.APIConnectionError as exc:
            log.info(
                "no-key mode: provider unreachable (%s), using static response",
                exc,
            )
        except anthropic.AnthropicError as exc:
            log.info(
                "no-key mode: request failed (%r), using static response", exc
            )

        remaining = target - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)

        return LLMResult(text=stub_response, model=STUB_MODEL_NAME, stub=True)
