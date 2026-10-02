"""Load quantities/author states from science sources without separate vocabularies."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

RULES_RESOURCE = ("science", "rules.toml")
EXPECTED_QUANTITY_COUNT = 18
EXPECTED_STATE_COUNT = 5


class RulesFormatError(ValueError):
    """Science source lacks structure required by record formats."""


@dataclass(frozen=True)
class QuantityDefinition:
    id: str
    name: str
    definition: str
    legacy_path: str


@dataclass(frozen=True)
class StateDefinition:
    id: str
    name: str
    definition: str


@dataclass(frozen=True)
class ScienceRules:
    version: str
    sha256: str
    quantities: tuple[QuantityDefinition, ...]
    states: tuple[StateDefinition, ...]

    @property
    def quantity_paths(self) -> tuple[str, ...]:
        return tuple(q.legacy_path for q in self.quantities)

    @property
    def state_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.states)


def rules_bytes() -> bytes:
    return (
        resources.files("hvs_catalog.resources") / RULES_RESOURCE[0] / RULES_RESOURCE[1]
    ).read_bytes()


def _load(data: dict[str, Any], sha256: str) -> ScienceRules:
    if data["instruction_language"] != "en":
        raise RulesFormatError("提取指令必须使用英文")
    quantities = tuple(
        QuantityDefinition(
            id=row["id"],
            name=row["name"],
            definition=row["definition"],
            legacy_path=row["legacy_path"],
        )
        for row in data["quantities"]
    )
    states = tuple(
        StateDefinition(id=row["id"], name=row["name"], definition=row["definition"])
        for row in data["states"]
    )
    if len(quantities) != EXPECTED_QUANTITY_COUNT:
        raise RulesFormatError(f"物理量必须恰好 {EXPECTED_QUANTITY_COUNT} 项")
    if len(states) != EXPECTED_STATE_COUNT:
        raise RulesFormatError(f"作者状态必须恰好 {EXPECTED_STATE_COUNT} 种")
    if len({q.legacy_path for q in quantities}) != len(quantities):
        raise RulesFormatError("物理量编码重复")
    version = data.get("version")
    if not version or not isinstance(version, str):
        raise RulesFormatError("规则版本必须是非空文本")
    return ScienceRules(
        version=version, sha256=sha256, quantities=quantities, states=states
    )


@lru_cache(maxsize=1)
def load_rules() -> ScienceRules:
    raw = rules_bytes()
    return _load(tomllib.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest())


def quantity_paths() -> tuple[str, ...]:
    return load_rules().quantity_paths


def author_state_ids() -> tuple[str, ...]:
    return load_rules().state_ids


# One rule-source author vocabulary serves displays, saved models, and submission models.
AuthorStatus = Literal[author_state_ids()]


def is_quantity_path(path: str) -> bool:
    return path in load_rules().quantity_paths


def current_rules_ref() -> dict[str, str]:
    rules = load_rules()
    return {"version": rules.version, "sha256": rules.sha256}
