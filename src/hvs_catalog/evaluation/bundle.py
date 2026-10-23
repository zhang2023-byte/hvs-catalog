"""Freeze scoring inputs and replay them without a workspace or Gold repository."""

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..records.common import Config, sha256_of
from ..records.roster_validation import strict_json
from .scoring import Fingerprint, GoldReference, Prediction, ScoringPlan, score_batch


def program_reference() -> Fingerprint:
    root = Path(__file__).resolve().parents[1]
    files = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".py", ".toml", ".json", ".yaml"}
    }
    return Fingerprint(version=version("hvs-catalog"), sha256=sha256_of(files))


class ScoringBundle(Config):
    format: Literal["hvs.scoring-inputs"] = "hvs.scoring-inputs"
    version: Literal[4] = 4
    plan: ScoringPlan
    gold: list[GoldReference]
    predictions: list[Prediction]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def make_bundle(plan, gold, predictions) -> ScoringBundle:
    report = score_batch(plan, gold, predictions)
    return ScoringBundle(
        plan=plan, gold=gold, predictions=predictions, sha256=report.input_sha256
    )


def save_scoring_bundle(path, *, plan, gold, predictions) -> ScoringBundle:
    bundle = make_bundle(plan, gold, predictions)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n"
        )
    return bundle


def load_scoring_bundle(path) -> ScoringBundle:
    bundle = ScoringBundle.model_validate(
        strict_json(Path(path).read_text(encoding="utf-8"))
    )
    if bundle.plan.program != program_reference():
        raise ValueError(
            "Scoring program differs from the frozen input. Use its recorded program version."
        )
    report = score_batch(bundle.plan, bundle.gold, bundle.predictions)
    if report.input_sha256 != bundle.sha256:
        raise ValueError("Frozen scoring input fingerprint differs from its content.")
    return bundle


def score_scoring_bundle(path):
    bundle = load_scoring_bundle(path)
    return score_batch(bundle.plan, bundle.gold, bundle.predictions)
