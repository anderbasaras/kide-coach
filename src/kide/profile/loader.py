"""Load a profile from YAML and explain every problem in plain words.

Each problem is reported with the YAML line and the dotted path of the field,
for example::

    line 42 · training.rotation[0].exercises[1].top_set_reps: min (8) is greater than max (6)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from kide.profile.models import Profile

_FRIENDLY = {
    "extra_forbidden": "unknown field (check the spelling)",
    "missing": "required field is missing",
}


class ProfileError(Exception):
    """A profile that cannot be loaded. ``problems`` holds one line per issue."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(str(self))

    def __str__(self) -> str:
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        return f"Invalid profile ({self.source}):\n{lines}"


def load_profile(path: Path | str) -> Profile:
    """Read and validate the profile at ``path``."""
    path = Path(path)
    if not path.is_file():
        raise ProfileError(
            str(path),
            ["file not found. Copy profile.example.yaml to profile.yaml and edit it."],
        )
    return parse_profile(path.read_text(encoding="utf-8"), source=str(path))


def parse_profile(text: str, source: str = "<profile>") -> Profile:
    """Validate profile YAML given as text."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1} · " if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ProfileError(source, [f"{where}invalid YAML: {problem}"]) from None

    if data is None:
        raise ProfileError(source, ["the file is empty"])
    if not isinstance(data, dict):
        raise ProfileError(source, ["the top level must be a mapping of sections (identity: ...)"])

    duplicates = _duplicate_keys(root)
    if duplicates:
        raise ProfileError(source, duplicates)

    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        described = sorted(
            (_describe(error, root) for error in exc.errors()), key=lambda item: item[0]
        )
        raise ProfileError(source, [text for _, text in described]) from None


def _describe(error: Any, root: yaml.Node | None) -> tuple[int, str]:
    """Return ``(line, text)`` for one validation error; line 0 if unknown."""
    loc = tuple(error["loc"])
    path = _format_loc(loc) or "<profile>"
    message = _FRIENDLY.get(error["type"], error["msg"]).removeprefix("Value error, ")
    line = _line_of(root, loc)
    prefix = f"line {line} · " if line else ""
    if "\n" in message:  # several problems found while resolving exercises
        indented = "\n      ".join(message.splitlines())
        return line or 0, f"{prefix}{path}:\n      {indented}"
    return line or 0, f"{prefix}{path}: {message}"


def _format_loc(loc: tuple[Any, ...]) -> str:
    text = ""
    for part in loc:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else str(part)
    return text


def _line_of(root: yaml.Node | None, loc: tuple[Any, ...]) -> int | None:
    """1-based line of the deepest YAML node that matches ``loc``."""
    node = root
    if node is None:
        return None
    for part in loc:
        child = None
        if isinstance(node, yaml.MappingNode):
            child = next((value for key, value in node.value if key.value == str(part)), None)
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
            child = node.value[part] if 0 <= part < len(node.value) else None
        if child is None:
            break
        node = child
    return node.start_mark.line + 1


def _duplicate_keys(node: yaml.Node | None, path: str = "") -> list[str]:
    """YAML silently keeps the last duplicated key; report them instead."""
    problems: list[str] = []
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            name = str(key.value)
            child_path = f"{path}.{name}" if path else name
            if name in seen:
                problems.append(f"line {key.start_mark.line + 1} · {child_path}: duplicated key")
            seen.add(name)
            problems.extend(_duplicate_keys(value, child_path))
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            problems.extend(_duplicate_keys(item, f"{path}[{index}]"))
    return problems
