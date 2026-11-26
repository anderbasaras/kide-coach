"""Structured outputs Claude produces: a box WOD and an "Other" activity (D-041).

Claude fills these through tool use (see ``llm/parsing.py``); Pydantic validates the
result. Bounds are generous sanity limits that catch misreadings (a 4,000 kg load, a
negative distance), not training rules: deciding load is the engine's job (D-005).

What is stored in ``training_sessions.parsed_json`` is an :class:`ParsedRecord` envelope:
``{kind, schema_version, status, data}``. ``status`` is ``draft`` until the user taps
Confirm. The load engine (phase 4) only reads ``confirmed`` records.
"""

from __future__ import annotations

import copy
import enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kide.catalog import MovementCatalog

SCHEMA_VERSION = 1


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class Confidence(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --- WOD -------------------------------------------------------------------------------


class BlockFormat(enum.StrEnum):
    AMRAP = "amrap"
    EMOM = "emom"
    FOR_TIME = "for_time"
    ROUNDS = "rounds"  # N rounds, not timed
    INTERVALS = "intervals"  # work/rest intervals (e.g. 8 x 20 s on / 10 s off)
    STRENGTH = "strength"  # sets x reps at a load (e.g. back squat 5x5)
    OTHER = "other"


class WodFormat(enum.StrEnum):
    """Overall format: the main block's, or ``mixed`` when the main blocks differ."""

    AMRAP = "amrap"
    EMOM = "emom"
    FOR_TIME = "for_time"
    ROUNDS = "rounds"
    INTERVALS = "intervals"
    STRENGTH = "strength"
    MIXED = "mixed"
    OTHER = "other"


class Scaling(enum.StrEnum):
    RX = "rx"
    SCALED = "scaled"
    FOUNDATIONS = "foundations"


class WodMovement(_Schema):
    movement_id: str | None = Field(
        None, description="Id from the movement catalog, or null if it is not in the catalog."
    )
    name: str = Field(min_length=1, max_length=80, description="Name as written on the board.")
    reps: int | None = Field(
        None, ge=1, le=10_000, description="Reps per round or per set (single number)."
    )
    reps_per_round: list[int] | None = Field(
        None,
        min_length=2,
        max_length=30,
        description="Rep ladder when it changes by round, e.g. 21-15-9 -> [21, 15, 9].",
    )
    load_kg: float | None = Field(
        None,
        ge=0,
        le=500,
        description="Load for THIS athlete in kg, per implement (one dumbbell/kettlebell).",
    )
    implements: int | None = Field(
        None, ge=1, le=2, description="Number of dumbbells/kettlebells (1 or 2)."
    )
    load_text: str | None = Field(
        None,
        max_length=60,
        description="Load as written when it is not plain kg, e.g. '43/29', '75%', '2x22.5'.",
    )
    distance_m: float | None = Field(None, ge=0, le=100_000)
    calories: float | None = Field(None, ge=0, le=2_000)
    duration_s: int | None = Field(None, ge=0, le=36_000)
    height_cm: float | None = Field(
        None, ge=0, le=400, description="Box height or wall-ball target height."
    )
    notes: str | None = Field(
        None,
        max_length=200,
        description=(
            "Only extra text from the input that fits no other field (e.g. 'max cal', "
            "'unbroken'). Usually null. Never explain your own choices here."
        ),
    )

    @model_validator(mode="after")
    def _ladder_and_reps(self) -> WodMovement:
        if self.reps_per_round:
            if any(reps < 1 for reps in self.reps_per_round):
                raise ValueError("reps_per_round values must be >= 1")
            if self.reps is not None:
                raise ValueError("use either reps or reps_per_round, not both")
        return self


class WodBlock(_Schema):
    label: str | None = Field(None, max_length=40, description="e.g. 'A', 'Strength', 'Metcon'.")
    format: BlockFormat
    is_warmup: bool = Field(False, description="True for the warm-up or mobility block.")
    rounds: int | None = Field(None, ge=1, le=200)
    sets: int | None = Field(None, ge=1, le=50, description="Strength sets, e.g. 5 in 5x5.")
    duration_min: float | None = Field(
        None, gt=0, le=300, description="AMRAP/EMOM length or total block time."
    )
    time_cap_min: float | None = Field(None, gt=0, le=300)
    every_min: float | None = Field(
        None, gt=0, le=30, description="EMOM/E2MOM interval in minutes (1, 2, 3...)."
    )
    work_s: int | None = Field(None, gt=0, le=3_600, description="Interval work time.")
    rest_s: int | None = Field(None, ge=0, le=3_600, description="Interval rest time.")
    load_pct_1rm: float | None = Field(
        None, gt=0, le=120, description="Strength intensity as % of 1RM, if written."
    )
    movements: list[WodMovement] = Field(min_length=1, max_length=30)
    notes: str | None = Field(
        None,
        max_length=300,
        description="Only extra text from the input about this block. Usually null.",
    )


class Wod(_Schema):
    title: str | None = Field(
        None, max_length=80, description="Workout name, e.g. a benchmark name."
    )
    format: WodFormat
    blocks: list[WodBlock] = Field(min_length=1, max_length=10)
    scaling: Scaling | None = Field(None, description="Level the athlete did, if stated.")
    result: str | None = Field(
        None, max_length=80, description="Score if stated: time, rounds+reps, load..."
    )
    notes: str | None = Field(
        None,
        max_length=500,
        description=(
            "Only extra text from the input about the whole workout (e.g. 'partner WOD'). "
            "Usually null. Never explain your own choices here."
        ),
    )
    unreadable: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Parts that ARE in the input but are illegible or ambiguous. Not for "
            "information that is simply not given (e.g. a target height nobody wrote), "
            "and never for explaining a choice such as which value of a pair was used."
        ),
    )
    confidence: Confidence

    @model_validator(mode="after")
    def _mixed_when_main_blocks_differ(self) -> Wod:
        """Keep the overall format consistent with the blocks (deterministic fix-up)."""
        main = [block.format for block in self.blocks if not block.is_warmup]
        if len(set(main)) > 1:
            self.format = WodFormat.MIXED
        elif len(set(main)) == 1 and self.format is WodFormat.MIXED:
            self.format = WodFormat(main[0].value)
        return self

    def movements(self, include_warmup: bool = False) -> list[WodMovement]:
        return [
            movement
            for block in self.blocks
            if include_warmup or not block.is_warmup
            for movement in block.movements
        ]


def apply_catalog(wod: Wod, catalog: MovementCatalog) -> Wod:
    """Make every ``movement_id`` a real catalog id or ``None``.

    An id the catalog does not know is dropped; a missing id is looked up from the
    written name. Returns a new :class:`Wod`.
    """
    data = wod.model_dump(mode="python")
    for block in data["blocks"]:
        for movement in block["movements"]:
            movement_id = movement["movement_id"]
            if movement_id is not None and movement_id not in catalog:
                movement_id = None
            if movement_id is None:
                found = catalog.resolve(movement["name"])
                movement_id = found.id if found else None
            movement["movement_id"] = movement_id
    return Wod.model_validate(data)


# --- Activity ("Other") ----------------------------------------------------------------


class ActivityType(enum.StrEnum):
    RUN = "run"
    WALK = "walk"
    HIKE = "hike"
    BIKE = "bike"
    ROW = "row"
    SWIM = "swim"
    OTHER = "other"


class Activity(_Schema):
    type: ActivityType
    name: str | None = Field(None, max_length=80, description="e.g. a route or a mountain.")
    duration_min: float | None = Field(None, gt=0, le=1_440)
    distance_km: float | None = Field(None, gt=0, le=500)
    elevation_gain_m: float | None = Field(None, ge=0, le=10_000)
    avg_pace_s_per_km: int | None = Field(
        None, ge=60, le=3_600, description="Average pace in seconds per km, if shown."
    )
    avg_hr_bpm: int | None = Field(None, ge=30, le=230, description="Average heart rate.")
    rpe: int | None = Field(
        None, ge=1, le=10, description="Perceived effort 1-10, only if the athlete stated it."
    )
    notes: str | None = Field(
        None,
        max_length=300,
        description="Only extra text the athlete wrote (e.g. 'muddy trail'). Usually null.",
    )
    confidence: Confidence

    @property
    def pace_s_per_km(self) -> int | None:
        """The stated pace, or duration / distance for runs, walks and hikes."""
        if self.avg_pace_s_per_km is not None:
            return self.avg_pace_s_per_km
        if (
            self.type in (ActivityType.RUN, ActivityType.WALK, ActivityType.HIKE)
            and self.duration_min
            and self.distance_km
        ):
            return round(self.duration_min * 60 / self.distance_km)
        return None


# --- Stored envelope -------------------------------------------------------------------

Kind = Literal["wod", "activity"]


class RecordStatus(enum.StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"


class ParsedRecord(BaseModel):
    """What goes into ``training_sessions.parsed_json``."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    schema_version: int = SCHEMA_VERSION
    status: RecordStatus = RecordStatus.DRAFT
    data: dict[str, Any]

    @classmethod
    def draft(cls, item: Wod | Activity) -> ParsedRecord:
        kind: Kind = "wod" if isinstance(item, Wod) else "activity"
        return cls(kind=kind, data=item.model_dump(mode="json"))

    def confirmed(self) -> ParsedRecord:
        return self.model_copy(update={"status": RecordStatus.CONFIRMED})

    def item(self) -> Wod | Activity:
        return (
            Wod.model_validate(self.data)
            if self.kind == "wod"
            else Activity.model_validate(self.data)
        )

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def tool_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON schema of ``model`` with every ``$ref`` inlined, for a tool's ``input_schema``."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                resolved = inline(copy.deepcopy(definitions[name]))
                extra = {key: value for key, value in node.items() if key != "$ref"}
                return {**resolved, **extra}
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(schema)
