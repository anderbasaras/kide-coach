"""Shared test data. Every athlete here is invented."""

import copy
from typing import Any

import pytest

_BASE: dict[str, Any] = {
    "identity": {"name": "Alex", "language": "en", "timezone": "Europe/Berlin"},
    "body": {"age": 31, "sex": "female", "height_cm": 170, "weight_kg": 64},
    "goals": {"primary": "hypertrophy", "secondary": ["hybrid_race"]},
    "training": {
        "defaults": {
            "scheme": "straight_sets",
            "warmup_sets": 0,
            "sets": 3,
            "reps": "10-12",
            "rir": {"sets": "1-2", "last_set": 0},
            "load_increment_kg": 2.5,
        },
        "rotation": [
            {
                "id": "upper",
                "name": "Upper",
                "focus": "upper",
                "exercises": [
                    {
                        "id": "bench_press",
                        "name": "Bench press",
                        "scheme": "top_set_backoff",
                        "warmup_sets": 2,
                        "top_set_reps": "6-8",
                        "backoff_sets": 2,
                        "backoff_reps": "8-10",
                        "backoff_drop_pct": 10,
                        "rir": {"top_set": "0-1", "backoff": "1-2"},
                        "loading": {"mode": "per_side", "base_weight_kg": 20},
                        "current_load": {"top_kg": 25, "backoff_kg": 20},
                    },
                    {
                        "id": "lat_pulldown",
                        "name": "Lat pulldown",
                        "loading": {"mode": "machine_stack", "stack_kg": [50, 45, 55]},
                        "current_load": {"working_kg": 45},
                    },
                ],
            },
            {
                "id": "lower",
                "name": "Lower",
                "focus": "lower",
                "exercises": [
                    {"id": "goblet_squat", "name": "Goblet squat", "loading": {"mode": "total"}},
                ],
            },
        ],
    },
}


@pytest.fixture
def anyio_backend() -> str:
    """Async tests (``@pytest.mark.anyio``) run on asyncio, like python-telegram-bot."""
    return "asyncio"


@pytest.fixture
def profile_data() -> dict[str, Any]:
    """A valid profile as a plain dict; tests mutate their own copy."""
    return copy.deepcopy(_BASE)


def exercise(data: dict[str, Any], session: int = 0, index: int = 0) -> dict[str, Any]:
    return data["training"]["rotation"][session]["exercises"][index]
