"""A fake Anthropic SDK: returns queued responses and records every request."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
from anthropic.types import Message


def make_message(
    content: list[dict[str, Any]] | str = "OK",
    *,
    model: str = "claude-sonnet-5-5",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_write: int = 0,
    cache_read: int = 0,
    stop_reason: str = "end_turn",
) -> Message:
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    return Message.model_validate(
        {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        }
    )


def tool_message(name: str, tool_input: dict[str, Any], **kwargs: Any) -> Message:
    block = {"type": "tool_use", "id": "toolu_fake", "name": name, "input": tool_input}
    return make_message([block], stop_reason="tool_use", **kwargs)


def api_error(
    cls: type[anthropic.APIStatusError], status: int, detail: str | None = None
) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    body = {"type": "error", "error": {"type": "x", "message": detail}} if detail else None
    return cls("boom", response=response, body=body)


class FakeMessages:
    def __init__(self, sdk: FakeSdk) -> None:
        self._sdk = sdk

    async def create(self, **kwargs: Any) -> Message:
        self._sdk.requests.append(kwargs)
        if not self._sdk.responses:
            raise AssertionError("FakeSdk: no response queued")
        response = self._sdk.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeSdk:
    def __init__(self, *responses: Message | BaseException) -> None:
        self.responses: list[Message | BaseException] = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = FakeMessages(self)

    def queue(self, *responses: Message | BaseException) -> None:
        self.responses.extend(responses)
