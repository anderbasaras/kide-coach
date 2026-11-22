"""Pydantic v2 schema of the user profile.

The profile is the single source of truth about the athlete (PLAN, D-004). This
module only *represents* data: rules, blocks and progression are stored here but
interpreted by the engine in later phases.

Sections: identity, body, goals, competitions, experience, lifestyle, schedule,
training, weight_logging, equipment, maxes, constraints, volume_targets.

Conventions
-----------
* Unknown keys are rejected, so typos surface as errors instead of being ignored.
* Ranges accept ``8``, ``"6-8"`` or ``[6, 8]``.
* Times must be quoted (``"09:00"``): YAML 1.1 reads an unquoted ``9:00`` as a number.
* Brand names are never used as values; use the neutral ones (principle 6, D-014).
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections import Counter
from collections.abc import Callable, Iterator
from functools import cache
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1


class KideModel(BaseModel):
    """Base model: strict about unknown keys, trims surrounding whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Reusable field types
# --------------------------------------------------------------------------

_BRAND_HINTS = {
    "crossfit": "functional_fitness",
    "hyrox": "hybrid_race",
    "hyrox_performance": "hybrid_race",
}


def _no_brand_names(value: object) -> object:
    """Reject brand names with a hint to the neutral value (principle 6)."""
    if isinstance(value, str) and value.strip().lower() in _BRAND_HINTS:
        hint = _BRAND_HINTS[value.strip().lower()]
        raise ValueError(f"'{value}' is a brand name; use the neutral value '{hint}'")
    return value


Goal = Annotated[
    Literal["hypertrophy", "strength", "hybrid_race", "endurance", "general"],
    BeforeValidator(_no_brand_names),
]
ClassType = Annotated[
    Literal[
        "functional_fitness", "hybrid_race", "weightlifting", "gymnastics", "endurance", "other"
    ],
    BeforeValidator(_no_brand_names),
]
CompetitionType = Annotated[
    Literal[
        "hybrid_race", "functional_fitness", "running", "weightlifting", "powerlifting", "other"
    ],
    BeforeValidator(_no_brand_names),
]
Level = Literal["none", "beginner", "intermediate", "advanced"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
Scheme = Literal["top_set_backoff", "straight_sets"]
LoadingMode = Literal[
    "per_side",  # plates on each side, base weight (bar) not counted
    "per_dumbbell",  # weight of one dumbbell / kettlebell
    "total",  # everything on the implement, base weight not counted
    "machine_stack",  # selectorised machine: the number on the pin
    "added_weight",  # belt / vest load on top of bodyweight (negative = assistance)
    "bodyweight",  # no external load
]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def _check_slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(
            f"'{value}' is not a valid id: use lowercase letters, digits and '_' (e.g. 'upper_a')"
        )
    return value


Slug = Annotated[str, AfterValidator(_check_slug)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


def _parse_time(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        raise ValueError(
            'write the time in quotes, e.g. "09:00" (YAML reads an unquoted 9:00 as a number)'
        )
    if isinstance(value, str):
        match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
        if not match:
            raise ValueError(f"expected a 24-hour time like '09:00', got '{value}'")
        return dt.time(int(match[1]), int(match[2]))
    return value


ClockTime = Annotated[dt.time, BeforeValidator(_parse_time)]


def _parse_partial_date(value: object) -> object:
    return value.isoformat() if isinstance(value, dt.date) else value


def _check_partial_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?)?", value):
        raise ValueError(f"expected YYYY, YYYY-MM or YYYY-MM-DD, got '{value}'")
    return value


PartialDate = Annotated[
    str, BeforeValidator(_parse_partial_date), AfterValidator(_check_partial_date)
]


class IntRange(KideModel):
    """Inclusive integer range. Written in YAML as ``8``, ``"6-8"`` or ``[6, 8]``."""

    min: int
    max: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
        return self

    def __str__(self) -> str:
        return str(self.min) if self.min == self.max else f"{self.min}-{self.max}"


def _parse_range(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return {"min": value, "max": value}
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", value)
        if not match:
            raise ValueError(f"expected a number or a range like '6-8', got '{value}'")
        return {"min": int(match[1]), "max": int(match[2] or match[1])}
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"min": value[0], "max": value[1]}
    return value


def _bounded(low: int, high: int, what: str) -> Callable[[IntRange], IntRange]:
    def check(value: IntRange) -> IntRange:
        if value.min < low or value.max > high:
            raise ValueError(f"{what} must be between {low} and {high}, got {value}")
        return value

    return check


RepRange = Annotated[
    IntRange, BeforeValidator(_parse_range), AfterValidator(_bounded(1, 100, "reps"))
]
RirRange = Annotated[
    IntRange, BeforeValidator(_parse_range), AfterValidator(_bounded(0, 10, "RIR"))
]
RestRange = Annotated[
    IntRange, BeforeValidator(_parse_range), AfterValidator(_bounded(0, 900, "rest (seconds)"))
]
SetsRange = Annotated[
    IntRange, BeforeValidator(_parse_range), AfterValidator(_bounded(0, 60, "weekly sets"))
]


def _unique(items: list[Any], what: str) -> None:
    repeated = sorted(str(item) for item, count in Counter(items).items() if count > 1)
    if repeated:
        raise ValueError(f"repeated {what}: {', '.join(repeated)}")


# --------------------------------------------------------------------------
# identity, body, goals, competitions
# --------------------------------------------------------------------------


@cache
def _timezones() -> frozenset[str]:
    return frozenset(available_timezones())


class Identity(KideModel):
    name: NonEmptyStr
    language: str = Field("en", description="Language for the bot and Claude, e.g. 'es' or 'en'.")
    timezone: str = Field(description="IANA time zone, e.g. 'Europe/Madrid'.")

    @field_validator("language")
    @classmethod
    def _language_tag(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", value):
            raise ValueError(f"expected a language code like 'es' or 'en', got '{value}'")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            close = difflib.get_close_matches(value, _timezones(), n=1)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            raise ValueError(f"unknown time zone '{value}'.{hint}") from None
        return value


class Body(KideModel):
    age: int | None = Field(None, ge=14, le=100)
    sex: Literal["male", "female", "other"] | None = None
    height_cm: float | None = Field(None, ge=100, le=250)
    weight_kg: float | None = Field(None, ge=30, le=300)
    body_fat_pct: float | None = Field(None, ge=3, le=60)
    waist_cm: float | None = Field(None, ge=40, le=200)


class Goals(KideModel):
    primary: Goal
    secondary: list[Goal] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _unique(self.secondary, "secondary goals")
        if self.primary in self.secondary:
            raise ValueError(f"'{self.primary}' is already the primary goal")
        return self


class Competition(KideModel):
    name: NonEmptyStr
    date: dt.date
    type: CompetitionType
    category: str | None = Field(None, description="e.g. 'individual', 'mixed pairs'.")
    notes: str | None = None


# --------------------------------------------------------------------------
# experience and lifestyle (D-009)
# --------------------------------------------------------------------------


class PastCompetition(KideModel):
    name: NonEmptyStr
    when: PartialDate
    type: CompetitionType
    category: str | None = None
    result: str | None = None


class Experience(KideModel):
    gym_years: float | None = Field(None, ge=0, le=80)
    box_level: Level | None = None
    box_since: PartialDate | None = None
    hybrid_race_level: Level | None = None
    past_competitions: list[PastCompetition] = Field(default_factory=list)


class Lifestyle(KideModel):
    sleep_hours: float | None = Field(None, ge=3, le=14)
    daily_steps: int | None = Field(None, ge=0, le=100_000)
    steps_goal: int | None = Field(None, ge=0, le=100_000)
    job_activity: Literal["sedentary", "mixed", "active", "very_active"] | None = None
    stress: Literal["low", "moderate", "high"] | None = None
    notes: str | None = None


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------


class WeeklySummary(KideModel):
    day: Weekday = "monday"
    time: ClockTime = dt.time(9, 0)


class BoxSchedule(KideModel):
    name: str | None = None
    classes_per_week: float = Field(0, ge=0, le=14)
    days: list[Weekday] = Field(default_factory=list, description="Empty = no fixed days.")
    class_types: list[ClassType] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _unique(self.days, "days")
        _unique(self.class_types, "class types")
        return self


class Schedule(KideModel):
    checkin_time: ClockTime = dt.time(9, 0)
    weekly_summary: WeeklySummary = Field(default_factory=WeeklySummary)
    gym_session_minutes: int = Field(75, ge=15, le=300)
    gym_sessions_per_week: int | None = Field(None, ge=1, le=7)
    box: BoxSchedule = Field(default_factory=BoxSchedule)


# --------------------------------------------------------------------------
# weight logging (default loading, D-012)
# --------------------------------------------------------------------------


class WeightLogging(KideModel):
    unit: Literal["kg", "lb"] = "kg"
    default_mode: LoadingMode = "per_side"
    includes_base_weight: bool = Field(
        False, description="Whether logged weights include the bar / machine starting resistance."
    )


# --------------------------------------------------------------------------
# training (D-012, D-013, D-014, D-015)
# --------------------------------------------------------------------------


class Loading(KideModel):
    """How the load of an exercise is written down."""

    mode: LoadingMode
    base_weight_kg: float | None = Field(
        None, ge=0, description="Bar or machine starting resistance (e.g. 20, or 11.3 on a Smith)."
    )
    stack_kg: list[float] | None = Field(
        None, description="Known pin positions of a selectorised machine (may be partial)."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.base_weight_kg is not None and self.mode not in ("per_side", "total"):
            raise ValueError(f"base_weight_kg only applies to per_side or total, not {self.mode}")
        if self.stack_kg is not None:
            if self.mode != "machine_stack":
                raise ValueError(f"stack_kg only applies to machine_stack, not {self.mode}")
            if not self.stack_kg or any(value <= 0 for value in self.stack_kg):
                raise ValueError("stack_kg must be a non-empty list of positive weights")
            self.stack_kg = sorted(set(self.stack_kg))
        return self


class RirTargets(KideModel):
    """Target reps in reserve per set type. Merged key by key with the defaults."""

    top_set: RirRange | None = None
    backoff: RirRange | None = None
    sets: RirRange | None = Field(None, description="Straight sets, except the last one.")
    last_set: RirRange | None = Field(None, description="Last straight set (0 = to failure).")


class CurrentLoad(KideModel):
    """Starting load while there are no logged sessions (D-015), in the exercise's loading."""

    top_kg: float | None = None
    backoff_kg: float | None = None
    working_kg: float | None = None


_PRESCRIPTION_FIELDS = (
    "scheme",
    "warmup_sets",
    "top_set_reps",
    "backoff_sets",
    "backoff_reps",
    "backoff_drop_pct",
    "sets",
    "reps",
    "rir",
    "load_increment_kg",
    "rest_seconds",
)
_TOP_SET_ONLY = ("top_set_reps", "backoff_sets", "backoff_reps", "backoff_drop_pct")
_STRAIGHT_ONLY = ("sets", "reps")


class _Prescription(KideModel):
    """Set and rep prescription. Every field is optional so it can be inherited."""

    scheme: Scheme | None = None
    warmup_sets: int | None = Field(None, ge=0, le=6)
    top_set_reps: RepRange | None = None
    backoff_sets: int | None = Field(None, ge=0, le=6)
    backoff_reps: RepRange | None = None
    backoff_drop_pct: float | None = Field(
        None, gt=0, lt=50, description="Back-off load = top-set total load x (1 - pct/100)."
    )
    sets: int | None = Field(None, ge=1, le=10)
    reps: RepRange | None = None
    rir: RirTargets | None = None
    load_increment_kg: float | None = Field(None, gt=0, le=50)
    rest_seconds: RestRange | None = None


class TrainingDefaults(_Prescription):
    """Profile-wide defaults; every exercise can override any field."""


class Exercise(_Prescription):
    id: Slug
    name: NonEmptyStr
    loading: Loading | None = Field(None, description="Defaults to weight_logging.default_mode.")
    unilateral: bool = False
    superset: str | None = Field(None, description="Exercises with the same label are supersetted.")
    current_load: CurrentLoad | None = None
    notes: str | None = None


class Prescription(KideModel):
    """Fully resolved prescription of one exercise (defaults + overrides)."""

    scheme: Scheme
    warmup_sets: int = 0
    top_set_reps: RepRange | None = None
    backoff_sets: int | None = None
    backoff_reps: RepRange | None = None
    backoff_drop_pct: float | None = None
    sets: int | None = None
    reps: RepRange | None = None
    rir: RirTargets = Field(default_factory=RirTargets)
    load_increment_kg: float | None = None
    rest_seconds: RestRange | None = None
    loading: Loading

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.scheme == "top_set_backoff":
            needed = ["top_set_reps", "backoff_sets", "backoff_drop_pct"]
            if self.backoff_sets:
                needed.append("backoff_reps")
        else:
            needed = ["sets", "reps"]
        if self.loading.mode != "bodyweight":
            needed.append("load_increment_kg")
        missing = [name for name in needed if getattr(self, name) is None]
        if missing:
            raise ValueError(
                f"{self.scheme} needs {', '.join(missing)} "
                "(set it on the exercise or in training.defaults)"
            )
        return self


class RotationSession(KideModel):
    id: Slug
    name: NonEmptyStr
    focus: Literal["upper", "lower", "full_body", "other"] | None = None
    exercises: list[Exercise] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _unique([exercise.id for exercise in self.exercises], "exercise ids")
        groups = Counter(exercise.superset for exercise in self.exercises if exercise.superset)
        lonely = sorted(label for label, count in groups.items() if count < 2)
        if lonely:
            raise ValueError(f"superset groups with a single exercise: {', '.join(lonely)}")
        return self


class Deload(KideModel):
    load_pct: float = Field(gt=0, le=100, description="Load as % of the normal working load.")
    sets: int | None = Field(None, ge=1, le=10)
    rir: RirRange | None = None


class TrainingBlock(KideModel):
    """Optional training block (D-013). Represented only: no engine logic yet."""

    weeks: int = Field(ge=1, le=26)
    start_date: dt.date | None = None
    deload_week: int | None = Field(None, ge=1)
    deload: Deload | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.deload_week is not None:
            if self.deload_week > self.weeks:
                raise ValueError(
                    f"deload_week ({self.deload_week}) is after the last week ({self.weeks})"
                )
            if self.deload is None:
                raise ValueError("deload_week is set but the deload section is missing")
        return self


class Training(KideModel):
    defaults: TrainingDefaults = Field(default_factory=TrainingDefaults)
    rotation: list[RotationSession] = Field(
        min_length=1, description="Sessions in order; the rotation does not follow weekdays."
    )
    block: TrainingBlock | None = None
    planning_rules: list[NonEmptyStr] = Field(
        default_factory=list,
        description="How to combine gym and classes, as text until the planner defines a format.",
    )

    @model_validator(mode="after")
    def _unique_sessions(self) -> Self:
        _unique([session.id for session in self.rotation], "session ids")
        return self


# --------------------------------------------------------------------------
# equipment, maxes, constraints
# --------------------------------------------------------------------------


class GymEquipment(KideModel):
    type: Literal["commercial", "home", "other"] | None = None
    items: list[str] = Field(default_factory=list)
    plates_kg: list[float] = Field(default_factory=list, description="Available plate sizes.")
    dumbbell_step_kg: float | None = Field(None, gt=0)
    notes: str | None = None


class BoxEquipment(KideModel):
    items: list[str] = Field(default_factory=list, description="Empty = standard box equipment.")
    notes: str | None = None


class Equipment(KideModel):
    gym: GymEquipment = Field(default_factory=GymEquipment)
    box: BoxEquipment = Field(default_factory=BoxEquipment)


class Max(KideModel):
    exercise: NonEmptyStr = Field(description="Exercise id from the rotation, or a name.")
    kind: Literal["one_rm_estimate", "rep_max"] = "one_rm_estimate"
    value_kg: float = Field(ge=-100, le=1000)
    reps: int = Field(1, ge=1, le=50)
    loading: LoadingMode | None = None
    date: PartialDate | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _reps_match_kind(self) -> Self:
        if self.kind == "one_rm_estimate" and self.reps != 1:
            raise ValueError("a one_rm_estimate has reps: 1; use kind: rep_max for more reps")
        return self


class Constraints(KideModel):
    injuries: list[str] = Field(default_factory=list, description="Free text; never a diagnosis.")
    avoid_exercises: list[str] = Field(default_factory=list)
    notes: str | None = None


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


class Profile(KideModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    identity: Identity
    body: Body = Field(default_factory=Body)
    goals: Goals
    competitions: list[Competition] = Field(default_factory=list)
    experience: Experience = Field(default_factory=Experience)
    lifestyle: Lifestyle = Field(default_factory=Lifestyle)
    schedule: Schedule = Field(default_factory=Schedule)
    training: Training
    weight_logging: WeightLogging = Field(default_factory=WeightLogging)
    equipment: Equipment = Field(default_factory=Equipment)
    maxes: list[Max] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)
    volume_targets: dict[Slug, SetsRange] = Field(
        default_factory=dict, description="Weekly hard sets per muscle group, e.g. chest: 10-20."
    )

    @model_validator(mode="after")
    def _exercises_resolve(self) -> Self:
        problems = []
        for session, exercise in self.iter_exercises():
            try:
                self.prescription(exercise)
            except (ValidationError, ValueError) as exc:
                problems.append(f"session '{session.id}', exercise '{exercise.id}': {_short(exc)}")
        if problems:
            raise ValueError("\n".join(problems))
        return self

    def iter_exercises(self) -> Iterator[tuple[RotationSession, Exercise]]:
        for session in self.training.rotation:
            for exercise in session.exercises:
                yield session, exercise

    def prescription(self, exercise: Exercise) -> Prescription:
        """Merge ``training.defaults`` with the exercise overrides and validate the result."""
        own = exercise.model_dump(include=set(_PRESCRIPTION_FIELDS), exclude_none=True)
        merged = self.training.defaults.model_dump(exclude_none=True)
        rir = {**merged.get("rir", {}), **own.get("rir", {})}
        merged.update(own)
        merged["rir"] = rir
        merged["loading"] = exercise.loading or Loading(mode=self.weight_logging.default_mode)

        scheme = merged.get("scheme")
        not_applicable = _TOP_SET_ONLY if scheme == "straight_sets" else _STRAIGHT_ONLY
        if scheme is not None:
            explicit = [name for name in not_applicable if name in own]
            if explicit:
                raise ValueError(f"{', '.join(explicit)} does not apply to {scheme}")
            for name in not_applicable:  # inherited but irrelevant for this scheme
                merged.pop(name, None)

        result = Prescription.model_validate(merged)
        _check_current_load(exercise.current_load, result, self.body.weight_kg)
        return result


def _check_current_load(
    load: CurrentLoad | None, rx: Prescription, bodyweight: float | None
) -> None:
    if rx.loading.mode == "added_weight" and bodyweight is None:
        raise ValueError("added_weight loading needs body.weight_kg")
    if load is None:
        return
    given = {name for name, value in load.model_dump().items() if value is not None}
    if rx.loading.mode == "bodyweight" and given:
        raise ValueError("current_load does not apply to bodyweight exercises")
    allowed = {"top_kg", "backoff_kg"} if rx.scheme == "top_set_backoff" else {"working_kg"}
    wrong = sorted(given - allowed)
    if wrong:
        raise ValueError(f"current_load.{', '.join(wrong)} does not apply to {rx.scheme}")
    if rx.loading.mode != "added_weight":
        negative = sorted(name for name in given if getattr(load, name) < 0)
        if negative:
            raise ValueError(f"current_load.{', '.join(negative)} cannot be negative")


def _short(exc: Exception) -> str:
    """One-line description of a validation problem."""
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            parts.append(f"{location}: {message}" if location else message)
        return "; ".join(parts)
    return str(exc)
