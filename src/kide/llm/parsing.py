"""Turn a WOD or an activity (text and photos) into validated structured data.

``Parser`` offers Claude one tool (record_wod or record_activity) with ``tool_choice:
auto`` and validates the tool input with Pydantic. Forced tool use is not used: Claude
Opus 5.5 and Sonnet 5.5 reject it, and strict tools or JSON outputs cannot express our
schemas within their complexity limits. So:

- no tool call (Claude answered in text): one more try, asking it to call the tool;
- invalid input: Claude gets the validation errors back as an error ``tool_result``
  and one more try;
- a refusal, or a second failure: the outcome is ``invalid`` and the bot asks the user
  to clarify (D-005: Claude only structures; nothing is decided here).

Parsing runs at low effort: extraction needs little reasoning, and thinking tokens are
billed as output. Thinking blocks are passed back unmodified on the retry.

Photos are read from DATA_DIR, rotated by their EXIF orientation, shrunk so their long
edge is at most ``max_image_edge`` px and re-encoded as JPEG. That keeps every image well
under the API's 10 MB limit (Telegram files can be 20 MB) and bounds the tokens per image.

Corrections ("Adjust") send the current reading, the original text and the user's
correction, never the photos again (cheaper, and principle 5).
"""

from __future__ import annotations

import base64
import enum
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError

from kide.catalog import MovementCatalog
from kide.llm.client import IMAGE_TIMEOUT_S, TEXT_TIMEOUT_S, LlmClient, Purpose, cached_system
from kide.llm.prompts.parsing import (
    ACTIVITY_TOOL,
    WOD_TOOL,
    athlete_context,
    language_name,
    parse_system_prompt,
    parse_tools,
)
from kide.llm.schemas import Activity, Wod, apply_catalog

logger = logging.getLogger(__name__)

# Long edge sent to the API. Claude 5.5 models downscale anything above 2576 px anyway;
# Telegram photos are usually 1280 px. Tuned with `kide eval` on real boards.
DEFAULT_MAX_IMAGE_EDGE = 2000
JPEG_QUALITY = 85
MAX_IMAGES = 6
WOD_MAX_TOKENS = 8192  # thinking + the tool call; a full board is ~1-2k output tokens
ACTIVITY_MAX_TOKENS = 4096
PARSE_EFFORT = "low"
MAX_ERRORS_SHOWN = 15


class Kind(enum.StrEnum):
    WOD = "wod"
    ACTIVITY = "activity"


class ParseStatus(enum.StrEnum):
    OK = "ok"
    INVALID = "invalid"  # no valid tool call after the retry: ask the user to clarify
    NO_INPUT = "no_input"  # nothing usable to read (no text, no readable photo)


@dataclass(frozen=True)
class AthleteInfo:
    """The only profile data a parsing call needs (principle 5)."""

    sex: str | None
    language: str


@dataclass
class ParseOutcome:
    status: ParseStatus
    item: Wod | Activity | None = None
    call_ids: list[int] = field(default_factory=list)
    attempts: int = 0
    skipped_images: int = 0

    @property
    def ok(self) -> bool:
        return self.status is ParseStatus.OK


_TOOLS = {Kind.WOD: WOD_TOOL, Kind.ACTIVITY: ACTIVITY_TOOL}
_MODELS: dict[Kind, type[Wod] | type[Activity]] = {Kind.WOD: Wod, Kind.ACTIVITY: Activity}
_MAX_TOKENS = {Kind.WOD: WOD_MAX_TOKENS, Kind.ACTIVITY: ACTIVITY_MAX_TOKENS}


def image_block(path: Path, max_edge: int = DEFAULT_MAX_IMAGE_EDGE) -> dict[str, Any] | None:
    """A base64 JPEG image content block, or ``None`` if the file is not a readable image."""
    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        logger.warning("Skipping an image that could not be read: %s", type(exc).__name__)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


def format_validation_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors()[:MAX_ERRORS_SHOWN]:
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"- {location}: {error['msg']}")
    return "The tool input is not valid:\n" + "\n".join(lines) + "\nCall the tool again, fixed."


class Parser:
    def __init__(
        self,
        llm: LlmClient,
        catalog: MovementCatalog,
        data_dir: Path,
        max_image_edge: int = DEFAULT_MAX_IMAGE_EDGE,
    ) -> None:
        self._llm = llm
        self._catalog = catalog
        self._data_dir = data_dir
        self._max_image_edge = max_image_edge
        self._system_prompt = parse_system_prompt(catalog)
        self._tools = {tool["name"]: tool for tool in parse_tools()}

    async def parse(
        self,
        kind: Kind,
        *,
        text: str | None,
        photo_paths: list[str],
        athlete: AthleteInfo,
        session_id: int | None = None,
    ) -> ParseOutcome:
        """Read a WOD or an activity from the user's text and photos (paths under DATA_DIR)."""
        images = []
        for relative in photo_paths[:MAX_IMAGES]:
            block = image_block(self._data_dir / relative, self._max_image_edge)
            if block is not None:
                images.append(block)
        skipped = len(photo_paths) - len(images)
        text = (text or "").strip()
        if not text and not images:
            return ParseOutcome(ParseStatus.NO_INPUT, skipped_images=skipped)

        request = (
            "Read this workout and call record_wod."
            if kind is Kind.WOD
            else "Read this activity and call record_activity."
        )
        request += f" Free-text fields in {language_name(athlete.language)}."
        content: list[dict[str, Any]] = [*images]
        body = f"{request}\n\nThe athlete wrote:\n{text}" if text else request
        content.append({"type": "text", "text": body})
        if kind is Kind.WOD:
            purpose = Purpose.WOD_PARSE_PHOTO if images else Purpose.WOD_PARSE_TEXT
        else:
            purpose = Purpose.ACTIVITY_PARSE
        outcome = await self._run(kind, content, athlete, purpose, session_id, bool(images))
        outcome.skipped_images = skipped
        return outcome

    async def adjust(
        self,
        kind: Kind,
        *,
        current: Wod | Activity,
        original_text: str | None,
        correction: str,
        athlete: AthleteInfo,
        session_id: int | None = None,
    ) -> ParseOutcome:
        """Apply the user's correction, in their own words, to the current reading."""
        tool = _TOOLS[kind]
        parts = [
            f"The athlete corrects your previous reading. Apply the correction and call {tool} "
            "with the complete, corrected data (not only the changed part).",
            "Current reading (JSON):",
            json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
        ]
        if original_text and original_text.strip():
            parts += ["What the athlete originally wrote:", original_text.strip()]
        parts += ["Correction:", correction.strip()]
        content = [{"type": "text", "text": "\n\n".join(parts)}]
        purpose = Purpose.WOD_ADJUST if kind is Kind.WOD else Purpose.ACTIVITY_ADJUST
        return await self._run(kind, content, athlete, purpose, session_id, False)

    async def _run(
        self,
        kind: Kind,
        content: list[dict[str, Any]],
        athlete: AthleteInfo,
        purpose: Purpose,
        session_id: int | None,
        has_images: bool,
    ) -> ParseOutcome:
        tool = _TOOLS[kind]
        model = _MODELS[kind]
        system = cached_system(self._system_prompt, athlete_context(athlete.sex, athlete.language))
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        outcome = ParseOutcome(ParseStatus.INVALID)

        for _attempt in range(2):  # first try + one retry
            result = await self._llm.call(
                purpose=purpose,
                model=self._llm.parse_model,
                system=system,
                messages=messages,
                max_tokens=_MAX_TOKENS[kind],
                tools=[self._tools[tool]],
                tool_choice={"type": "auto"},
                session_id=session_id,
                timeout_s=IMAGE_TIMEOUT_S if has_images else TEXT_TIMEOUT_S,
                effort=PARSE_EFFORT,
            )
            outcome.call_ids.append(result.call_id)
            outcome.attempts += 1
            # Passed back unmodified on a retry (thinking blocks included).
            assistant_turn = {
                "role": "assistant",
                "content": [
                    block.model_dump(exclude_none=True) for block in result.message.content
                ],
            }
            uses = result.tool_uses(tool)
            if not uses:
                logger.info(
                    "Parse call %s returned no %s call (stop=%s)",
                    result.call_id,
                    tool,
                    result.stop_reason,
                )
                if result.stop_reason in ("refusal", "max_tokens"):
                    return outcome
                nudge = (
                    f"Do not answer in text. Call {tool} now with what you can read; "
                    "describe anything you cannot read in `unreadable`."
                )
                messages = [*messages, assistant_turn, {"role": "user", "content": nudge}]
                continue
            use = uses[0]
            try:
                item = model.model_validate(use.input)
            except ValidationError as exc:
                logger.info(
                    "Parse call %s: invalid %s input (%d errors)",
                    result.call_id,
                    kind.value,
                    exc.error_count(),
                )
                messages = [
                    *messages,
                    assistant_turn,
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": use.id,
                                "is_error": True,
                                "content": format_validation_errors(exc),
                            }
                        ],
                    },
                ]
                continue
            if isinstance(item, Wod):
                item = apply_catalog(item, self._catalog)
            outcome.status = ParseStatus.OK
            outcome.item = item
            return outcome
        return outcome
