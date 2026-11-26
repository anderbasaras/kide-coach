"""Movement catalog: box and hybrid-race movements with their board aliases.

``data/movements.yaml`` lists each movement once with a stable id, a display name per
bot language and the spellings found on whiteboards ("T2B", "C&J", "zancadas con saco").
Claude is asked to pick an id from this list when it reads a WOD; :meth:`resolve` is the
deterministic fallback that maps a written name to an id, or to nothing when unsure.

Phase 4 extends each entry with muscle groups, movement pattern and type for the engine.
"""

from __future__ import annotations

import enum
import re
import unicodedata
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# The repo's data/ folder (src/kide/catalog/movements.py -> repo root). The bot runs from
# a checkout or a Docker image of the repo, never from a bare wheel (see docs, phase 7).
CATALOG_DIR = Path(__file__).resolve().parents[3] / "data"
MOVEMENTS_PATH = CATALOG_DIR / "movements.yaml"

_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_NOT_ALNUM = re.compile(r"[^a-z0-9&+]+")


class CatalogError(ValueError):
    """The catalog file is missing or invalid."""


class Category(enum.StrEnum):
    BARBELL = "barbell"
    DUMBBELL = "dumbbell"
    KETTLEBELL = "kettlebell"
    GYMNASTICS = "gymnastics"
    BODYWEIGHT = "bodyweight"
    MONOSTRUCTURAL = "monostructural"
    ODD_OBJECT = "odd_object"


class Unit(enum.StrEnum):
    """How the work of a movement is usually counted on a board."""

    REPS = "reps"
    CALORIES = "cal"
    METRES = "m"
    SECONDS = "s"


def normalize_name(text: str) -> str:
    """Lower case, no accents, punctuation as spaces: ``"Toes-To-Bar"`` -> ``"toes to bar"``.

    ``&`` and ``+`` are kept because they carry meaning on boards (``C&J``).
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_NOT_ALNUM.sub(" ", plain).split())


class Movement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    names: dict[str, str] = Field(min_length=1)
    category: Category
    unit: Unit
    aliases: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _snake_case(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("id must be snake_case (a-z, 0-9, _)")
        return value

    @field_validator("names")
    @classmethod
    def _non_empty_names(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not name.strip() for name in value.values()):
            raise ValueError("names must not be empty")
        return value

    def name(self, locale: str) -> str:
        """Display name in ``locale``, falling back to English."""
        return self.names.get(locale) or self.names.get("en") or self.id

    def spellings(self) -> set[str]:
        """Every normalized way of writing this movement: id, names and aliases."""
        raw = [self.id.replace("_", " "), *self.names.values(), *self.aliases]
        return {normalize_name(text) for text in raw if normalize_name(text)}


class _CatalogFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    movements: list[Movement] = Field(min_length=1)


class MovementCatalog:
    """Movements by id, with a name -> id index built from ids, names and aliases."""

    def __init__(self, movements: Iterable[Movement]) -> None:
        self._by_id: dict[str, Movement] = {}
        self._index: dict[str, str] = {}
        for movement in movements:
            if movement.id in self._by_id:
                raise CatalogError(f"duplicate movement id '{movement.id}'")
            self._by_id[movement.id] = movement
            for spelling in movement.spellings():
                owner = self._index.setdefault(spelling, movement.id)
                if owner != movement.id:
                    raise CatalogError(f"'{spelling}' points to both '{owner}' and '{movement.id}'")

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[Movement]:
        return iter(self._by_id.values())

    def __contains__(self, movement_id: object) -> bool:
        return movement_id in self._by_id

    def get(self, movement_id: str) -> Movement | None:
        return self._by_id.get(movement_id)

    @property
    def ids(self) -> list[str]:
        return list(self._by_id)

    def resolve(self, written: str) -> Movement | None:
        """The movement a board spelling refers to, or ``None`` if unknown or ambiguous.

        Tries the exact normalized text, then without a plural ending ("burpees",
        "wall balls", "flexiones"). Only whole names match: "5 burpees" does not.
        """
        key = normalize_name(written)
        if not key:
            return None
        for candidate in _plural_variants(key):
            movement_id = self._index.get(candidate)
            if movement_id is not None:
                return self._by_id[movement_id]
        return None


def _plural_variants(key: str) -> list[str]:
    """``key`` itself, then singular guesses for its last word (English and Spanish)."""
    variants = [key]
    head, _, last = key.rpartition(" ")
    prefix = f"{head} " if head else ""
    if len(last) > 3 and last.endswith("es"):
        variants.append(prefix + last[:-2])
    if len(last) >= 3 and last.endswith("s"):
        variants.append(prefix + last[:-1])
    return variants


def load_movements(path: Path = MOVEMENTS_PATH) -> MovementCatalog:
    """Read and validate the movement catalog."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path.name}: invalid YAML: {exc}") from exc
    try:
        parsed = _CatalogFile.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    return MovementCatalog(parsed.movements)


@lru_cache(maxsize=1)
def default_movements() -> MovementCatalog:
    """The shipped catalog, loaded once per process."""
    return load_movements()
