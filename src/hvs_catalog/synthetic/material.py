"""虚构 A/B 输入材料与来源证据；不包含提取或恢复实现。"""

from __future__ import annotations

import hashlib
from datetime import datetime

from ..records.common import build_record
from ..records.source import (
    EvidenceData,
    EvidenceRecord,
    SourceFile,
    SourcePackageData,
    SourcePackageRecord,
)

PAPER_KEYS = {"A": "FIC-R05-A", "B": "FIC-R05-B"}
SOURCE_IDS = {"A": "src-r05-a", "B": "src-r05-b"}
EVIDENCE_IDS = {
    "A": ["ev-r05-a-1", "ev-r05-a-2", "ev-r05-a-3"],
    "B": ["ev-r05-b-1", "ev-r05-b-2", "ev-r05-b-3"],
}
OBJECT_NAMES = {"A": "FIC-A-1", "B": "FIC-B-1"}
LINES = {
    "A": [
        (
            "We selected FIC-A-1 for follow-up spectroscopy because it had previously"
            " been identified as a Galactic unbound candidate."
        ),
        (
            "Our new spectrum gives a heliocentric radial velocity of 520 +/- 10 km/s"
            " for FIC-A-1."
        ),
        (
            "Under model M1 FIC-A-1 is consistent with being bound to the Milky Way,"
            " while under model M2 it remains unbound; we defer a definitive overall"
            " conclusion."
        ),
    ],
    "B": [
        (
            "We selected FIC-B-1 for follow-up spectroscopy because it had previously"
            " been identified as a Galactic unbound candidate."
        ),
        (
            "Our new spectrum gives a heliocentric radial velocity of 480 +/- 15 km/s"
            " for FIC-B-1."
        ),
        "We conclude that FIC-B-1 is unbound to the Milky Way.",
    ],
}


def tex_bytes(paper: str) -> bytes:
    return ("\n".join(LINES[paper]) + "\n").encode("utf-8")


def source_relative_path(paper: str) -> str:
    return f"sources/r05/{paper.lower()}/paper.tex"


def build_source_and_evidence(
    paper: str, *, created_at: datetime, execution_id: str | None
):
    source = build_record(
        SourcePackageRecord,
        record_id=SOURCE_IDS[paper],
        created_at=created_at,
        execution_id=execution_id,
        data=SourcePackageData(
            paper_key=PAPER_KEYS[paper],
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=source_relative_path(paper),
                    sha256=hashlib.sha256(tex_bytes(paper)).hexdigest(),
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    evidences = [
        build_record(
            EvidenceRecord,
            record_id=EVIDENCE_IDS[paper][index],
            created_at=created_at,
            execution_id=execution_id,
            data=EvidenceData(
                paper_key=PAPER_KEYS[paper],
                source_ref=source.ref().model_dump(mode="json"),
                file_id="paper",
                locator={
                    "kind": "text",
                    "start_line": index + 1,
                    "end_line": index + 1,
                },
                excerpt=line,
            ),
        )
        for index, line in enumerate(LINES[paper])
    ]
    return source, evidences
