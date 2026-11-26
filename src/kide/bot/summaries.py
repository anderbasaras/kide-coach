"""Readable summaries of a parsed WOD or activity, shown before the user confirms.

Pure functions over the schemas: every word comes from i18n and every movement name from
the catalog, so the summary is deterministic, testable and costs no API call. Plain text
(Telegram is sent without parse mode), one line per block and per movement.
"""

from __future__ import annotations

from collections.abc import Callable

from kide.catalog import MovementCatalog
from kide.llm.schemas import (
    Activity,
    BlockFormat,
    Confidence,
    Wod,
    WodBlock,
    WodMovement,
)

Translate = Callable[..., str]  # t(key, **params) in the user's language


def format_number(value: float, t: Translate) -> str:
    """``42.5`` -> ``"42,5"`` in Spanish; whole numbers without decimals; at most 2 decimals."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", t("formats.decimal_separator"))


def format_duration_min(minutes: float, t: Translate) -> str:
    if minutes < 60:
        return t("parsed.minutes", value=format_number(minutes, t))
    total = round(minutes)
    return t("parsed.duration_hm", hours=total // 60, minutes=f"{total % 60:02d}")


def format_pace(seconds_per_km: int) -> str:
    return f"{seconds_per_km // 60}:{seconds_per_km % 60:02d}"


# --- WOD -------------------------------------------------------------------------------


def summarize_wod(wod: Wod, t: Translate, locale: str, catalog: MovementCatalog) -> str:
    form = t(f"parsed.wod_formats.{wod.format.value}")
    lines = [
        t("parsed.wod_header_titled", title=wod.title, format=form)
        if wod.title
        else t("parsed.wod_header", format=form)
    ]
    for block in wod.blocks:
        lines.append(_block_header(block, t))
        lines.extend(
            t("parsed.movement_line", text=_movement_text(movement, block, t, locale, catalog))
            for movement in block.movements
        )
        if block.notes:
            lines.append(t("parsed.notes", value=block.notes))

    footer = []
    if wod.scaling:
        footer.append(t("parsed.level", value=t(f"parsed.scaling.{wod.scaling.value}")))
    if wod.result:
        footer.append(t("parsed.result", value=wod.result))
    if footer:
        lines.append(" · ".join(footer))
    if wod.notes:
        lines.append(t("parsed.notes", value=wod.notes))
    if wod.unreadable:
        lines.append(t("parsed.unreadable", value="; ".join(wod.unreadable)))
    if wod.confidence is Confidence.LOW:
        lines.append(t("parsed.low_confidence"))
    return "\n".join(lines)


def _block_header(block: WodBlock, t: Translate) -> str:
    parts: list[str] = []
    if block.label:
        parts.append(t("parsed.block_label", label=block.label))
    head = t("parsed.warmup") if block.is_warmup else t(f"parsed.wod_formats.{block.format.value}")
    details: list[str] = [head]
    if block.duration_min:
        details.append(t("parsed.minutes", value=format_number(block.duration_min, t)))
    if block.every_min:
        details.append(t("parsed.every", value=format_number(block.every_min, t)))
    if block.time_cap_min:
        details.append(t("parsed.time_cap", value=format_number(block.time_cap_min, t)))
    if block.rounds:
        details.append(t("parsed.rounds", value=block.rounds))
    if block.sets and block.format is not BlockFormat.STRENGTH:
        details.append(t("parsed.sets", value=block.sets))
    if block.work_s:
        details.append(t("parsed.interval", work=block.work_s, rest=block.rest_s or 0))
    if block.load_pct_1rm:
        details.append(t("parsed.pct_1rm", value=format_number(block.load_pct_1rm, t)))
    parts.append(" · ".join(details))
    return " ".join(parts)


def movement_name(
    movement: WodMovement, t: Translate, locale: str, catalog: MovementCatalog
) -> str:
    known = catalog.get(movement.movement_id) if movement.movement_id else None
    if known is not None:
        return known.name(locale)
    return t("parsed.unknown_movement", name=movement.name)


def _movement_text(
    movement: WodMovement,
    block: WodBlock,
    t: Translate,
    locale: str,
    catalog: MovementCatalog,
) -> str:
    parts: list[str] = []
    if movement.reps_per_round:
        parts.append("-".join(str(reps) for reps in movement.reps_per_round))
    elif movement.reps is not None and block.format is BlockFormat.STRENGTH and block.sets:
        parts.append(t("parsed.sets_reps", sets=block.sets, reps=movement.reps))
    elif movement.reps is not None:
        parts.append(t("parsed.reps", value=movement.reps))
    if movement.distance_m is not None:
        parts.append(t("parsed.distance_m", value=format_number(movement.distance_m, t)))
    if movement.calories is not None:
        parts.append(t("parsed.calories", value=format_number(movement.calories, t)))
    if movement.duration_s is not None:
        parts.append(t("parsed.seconds", value=movement.duration_s))
    parts.append(movement_name(movement, t, locale, catalog))

    extras: list[str] = []
    if movement.load_kg is not None:
        value = format_number(movement.load_kg, t)
        extras.append(
            t("parsed.load_implements", count=movement.implements, value=value)
            if movement.implements and movement.implements > 1
            else t("parsed.load_kg", value=value)
        )
    elif movement.load_text:
        extras.append(movement.load_text)
    if movement.height_cm is not None:
        extras.append(t("parsed.height_cm", value=format_number(movement.height_cm, t)))
    if movement.notes:
        extras.append(movement.notes)
    text = " ".join(parts)
    return " · ".join([text, *extras]) if extras else text


# --- Activity --------------------------------------------------------------------------


def summarize_activity(activity: Activity, t: Translate) -> str:
    head = t(f"parsed.activity_types.{activity.type.value}")
    if activity.name:
        head = f"{head} · {activity.name}"
    details: list[str] = []
    if activity.distance_km is not None:
        details.append(t("parsed.distance_km", value=format_number(activity.distance_km, t)))
    details.append(
        format_duration_min(activity.duration_min, t)
        if activity.duration_min is not None
        else t("parsed.duration_unknown")
    )
    pace = activity.pace_s_per_km
    if pace is not None:
        details.append(t("parsed.pace", value=format_pace(pace)))
    if activity.elevation_gain_m:
        details.append(t("parsed.elevation", value=format_number(activity.elevation_gain_m, t)))
    if activity.avg_hr_bpm is not None:
        details.append(t("parsed.heart_rate", value=activity.avg_hr_bpm))
    if activity.rpe is not None:
        details.append(t("parsed.rpe", value=activity.rpe))
    lines = [head, " · ".join(details)]
    if activity.notes:
        lines.append(t("parsed.notes", value=activity.notes))
    if activity.confidence is Confidence.LOW:
        lines.append(t("parsed.low_confidence"))
    return "\n".join(lines)
