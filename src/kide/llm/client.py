"""Thin wrapper around the official Anthropic SDK.

Every call made by Kide goes through :meth:`LlmClient.call`, which:

- uses the async SDK with a per-call timeout; the SDK itself retries connection errors,
  408/409/429 and 5xx responses with exponential backoff (``max_retries``);
- caches the stable part of the system prompt (see :func:`cached_system`). Prefixes are
  cached in the order tools -> system -> messages, so a breakpoint on the system prompt
  also covers the tool definitions;
- records one ``llm_calls`` row per call (also failed ones): purpose, model, tokens,
  cache tokens apart (D-024), estimated cost, latency and the session it belongs to;
- logs ids, types and counters only, never a prompt or a response (D-037).

Errors become :class:`LlmError` with a coarse :class:`LlmErrorKind` that handlers turn
into a user message. The client knows nothing about Telegram or the engine.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import anthropic
from anthropic.types import Message
from sqlalchemy.orm import Session, sessionmaker

from kide.config import Settings
from kide.llm.pricing import TokenUsage, estimate_cost
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
TEXT_TIMEOUT_S = 60.0
IMAGE_TIMEOUT_S = 120.0  # vision calls with several photos take longer


class Purpose(enum.StrEnum):
    """Why a call was made; stored in ``llm_calls.purpose`` to know the cost of each feature."""

    WOD_PARSE_TEXT = "wod_parse_text"
    WOD_PARSE_PHOTO = "wod_parse_photo"
    WOD_ADJUST = "wod_adjust"
    ACTIVITY_PARSE = "activity_parse"
    ACTIVITY_ADJUST = "activity_adjust"
    CHAT = "chat"
    CHECK = "check"
    EVAL = "eval"


class LlmErrorKind(enum.StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    OTHER = "other"


class LlmError(RuntimeError):
    """A call failed after the SDK's own retries.

    ``detail`` is the API's own error message (e.g. "credit balance is too low"). It is
    kept out of the log and of ``llm_calls`` (D-037); only operator tools print it.
    """

    def __init__(
        self, kind: LlmErrorKind, call_id: int | None = None, detail: str | None = None
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.call_id = call_id
        self.detail = detail


def classify_error(exc: BaseException) -> LlmErrorKind:
    # Order matters: APITimeoutError is an APIConnectionError.
    if isinstance(exc, anthropic.APITimeoutError):
        return LlmErrorKind.TIMEOUT
    if isinstance(exc, anthropic.APIConnectionError):
        return LlmErrorKind.CONNECTION
    if isinstance(exc, anthropic.RateLimitError):
        return LlmErrorKind.RATE_LIMIT
    if isinstance(exc, anthropic.OverloadedError):
        return LlmErrorKind.OVERLOADED
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return LlmErrorKind.AUTH
    if isinstance(exc, anthropic.BadRequestError):
        return LlmErrorKind.BAD_REQUEST
    return LlmErrorKind.OTHER


def _error_label(exc: BaseException) -> str:
    """What goes to ``llm_calls.error``: kind, class and status, never the API's message."""
    status = getattr(exc, "status_code", None)
    suffix = f" ({status})" if status else ""
    return f"{classify_error(exc).value}: {type(exc).__name__}{suffix}"


def api_error_detail(exc: BaseException) -> str | None:
    """The message the API gave for a failed request, if any."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and message else None


def cached_system(stable: str, dynamic: str | None = None) -> list[dict[str, Any]]:
    """System prompt blocks: ``stable`` is cached; ``dynamic`` (per-call context) is not.

    Put everything that rarely changes (role, rules, catalog) in ``stable`` so repeated
    calls within the cache lifetime pay 0.05-0.1x for it instead of the full price.
    """
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
    ]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


def usage_from(message: Message) -> TokenUsage:
    usage = message.usage
    created = usage.cache_creation_input_tokens or 0
    breakdown = usage.cache_creation
    one_hour = (breakdown.ephemeral_1h_input_tokens or 0) if breakdown else 0
    return TokenUsage(
        input=usage.input_tokens or 0,
        output=usage.output_tokens or 0,
        cache_write_5m=created - one_hour,
        cache_write_1h=one_hour,
        cache_read=usage.cache_read_input_tokens or 0,
    )


@dataclass(frozen=True)
class LlmResult:
    message: Message
    model: str
    usage: TokenUsage
    cost_usd: Decimal | None
    latency_ms: int
    call_id: int

    @property
    def stop_reason(self) -> str | None:
        return self.message.stop_reason

    @property
    def text(self) -> str:
        """All text blocks of the answer, joined."""
        return "".join(block.text for block in self.message.content if block.type == "text")

    def tool_uses(self, name: str | None = None) -> list[Any]:
        """Tool-use blocks of the answer (``.id``, ``.name``, ``.input``)."""
        return [
            block
            for block in self.message.content
            if block.type == "tool_use" and (name is None or block.name == name)
        ]


class _Messages(Protocol):
    async def create(self, **kwargs: Any) -> Message: ...


class SdkClient(Protocol):
    """The part of ``anthropic.AsyncAnthropic`` Kide uses; tests pass a fake."""

    @property
    def messages(self) -> _Messages: ...


class LlmClient:
    def __init__(
        self,
        sdk: SdkClient,
        *,
        chat_model: str,
        parse_model: str,
        session_factory: sessionmaker[Session],
        user_id: int | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sdk = sdk
        self.chat_model = chat_model
        self.parse_model = parse_model
        self._session_factory = session_factory
        self._user_id = user_id
        self._clock = clock

    @classmethod
    def from_settings(
        cls, settings: Settings, session_factory: sessionmaker[Session], user_id: int | None
    ) -> LlmClient | None:
        """The client, or ``None`` when ANTHROPIC_API_KEY or ANTHROPIC_MODEL is not set."""
        key = settings.anthropic_api_key
        if key is None or not key.get_secret_value().strip() or not settings.anthropic_model:
            return None
        sdk = anthropic.AsyncAnthropic(
            api_key=key.get_secret_value(), max_retries=MAX_RETRIES, timeout=TEXT_TIMEOUT_S
        )
        return cls(
            sdk,
            chat_model=settings.anthropic_model,
            parse_model=settings.anthropic_parse_model or settings.anthropic_model,
            session_factory=session_factory,
            user_id=user_id,
        )

    async def call(
        self,
        *,
        purpose: Purpose,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        session_id: int | None = None,
        timeout_s: float = TEXT_TIMEOUT_S,
        effort: str | None = None,
    ) -> LlmResult:
        """One Messages API call, recorded in ``llm_calls``. Raises :class:`LlmError`.

        ``effort`` (``low`` ... ``max``) is soft guidance on how much the model thinks;
        ``max_tokens`` caps thinking and answer together.
        """
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "timeout": timeout_s,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if effort:
            kwargs["output_config"] = {"effort": effort}

        started = self._clock()
        try:
            message = await self._sdk.messages.create(**kwargs)
        except anthropic.AnthropicError as exc:
            latency = int((self._clock() - started) * 1000)
            kind = classify_error(exc)
            call_id = self._record(
                purpose, model, TokenUsage(), None, latency, session_id, error=_error_label(exc)
            )
            logger.warning(
                "LLM call failed: id=%s purpose=%s model=%s error=%s latency_ms=%d",
                call_id,
                purpose.value,
                model,
                kind.value,
                latency,
            )
            raise LlmError(kind, call_id, api_error_detail(exc)) from exc

        latency = int((self._clock() - started) * 1000)
        usage = usage_from(message)
        cost = estimate_cost(message.model or model, usage)
        call_id = self._record(purpose, message.model or model, usage, cost, latency, session_id)
        logger.info(
            "LLM call: id=%s purpose=%s model=%s in=%d out=%d cache_write=%d cache_read=%d "
            "cost_usd=%s latency_ms=%d stop=%s",
            call_id,
            purpose.value,
            message.model or model,
            usage.input,
            usage.output,
            usage.cache_write,
            usage.cache_read,
            cost if cost is not None else "unknown",
            latency,
            message.stop_reason,
        )
        if cost is None:
            logger.warning("No price known for model %s: cost not estimated", message.model)
        return LlmResult(
            message=message,
            model=message.model or model,
            usage=usage,
            cost_usd=cost,
            latency_ms=latency,
            call_id=call_id,
        )

    def _record(
        self,
        purpose: Purpose,
        model: str,
        usage: TokenUsage,
        cost: Decimal | None,
        latency_ms: int,
        session_id: int | None,
        error: str | None = None,
    ) -> int:
        with unit_of_work(self._session_factory) as repos:
            row = repos.llm_calls.record(
                user_id=self._user_id,
                session_id=session_id,
                purpose=purpose.value,
                model=model,
                input_tokens=usage.input,
                output_tokens=usage.output,
                cache_creation_input_tokens=usage.cache_write,
                cache_read_input_tokens=usage.cache_read,
                estimated_cost_usd=cost,
                latency_ms=latency_ms,
                error=error,
            )
            return row.id
