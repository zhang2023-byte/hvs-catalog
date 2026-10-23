"""Read the approved scientific source without a second vocabulary."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources


class RulesFormatError(ValueError):
    """The scientific source has an invalid structure."""


@dataclass(frozen=True)
class ScientificRule:
    id: str
    title: str
    text: str


@dataclass(frozen=True)
class QuantityDefinition:
    id: str
    definition: str
    common_units: str


@dataclass(frozen=True)
class ScienceRules:
    version: str
    sha256: str
    rules: tuple[ScientificRule, ...]
    quantities: tuple[QuantityDefinition, ...]


def rules_bytes() -> bytes:
    return (
        resources.files("hvs_catalog.resources") / "science" / "rules.toml"
    ).read_bytes()


def _load(data: dict, sha256: str) -> ScienceRules:
    if data.get("format_version") != 4 or data.get("instruction_language") != "en":
        raise RulesFormatError("Unsupported scientific source format")
    rules = tuple(ScientificRule(**row) for row in data["rules"])
    if tuple(r.id for r in rules) != ("S01", "S02", "S03", "S04"):
        raise RulesFormatError("The source must contain S01 through S04")
    quantities = tuple(
        QuantityDefinition(qid, row["definition"], row["common_units"])
        for row in data["quantities"]
        for qid in row["ids"]
    )
    if len({q.id for q in quantities}) != len(quantities) or not quantities:
        raise RulesFormatError("Quantity identifiers must be unique")
    if not isinstance(data.get("version"), str) or not data["version"]:
        raise RulesFormatError("The scientific version must be a nonempty string")
    return ScienceRules(data["version"], sha256, rules, quantities)


@lru_cache(maxsize=1)
def load_rules() -> ScienceRules:
    raw = rules_bytes()
    return _load(tomllib.loads(raw.decode()), hashlib.sha256(raw).hexdigest())


def current_rules_ref() -> dict[str, str]:
    rules = load_rules()
    return {"version": rules.version, "sha256": rules.sha256}
