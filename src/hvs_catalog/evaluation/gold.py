"""Single-file Gold serialization and checks against the original PDF."""

import hashlib
import json
from pathlib import Path

from pydantic import Field

from ..records.common import Config
from ..records.pdf_contribution import GoldAnnotation, pdf_evidence
from ..records.roster_validation import strict_json
from ..records.rules import current_rules_ref
from .pdfcheck import analyze_pdf


def annotation_to_json(annotation):
    annotation = GoldAnnotation.model_validate(annotation.model_dump(mode="json"))
    return (
        json.dumps(annotation.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n"
    )


def annotation_path(path):
    path = Path(path)
    return path / "annotation.json" if path.is_dir() else path


def read_annotation(path):
    path = annotation_path(path)
    if path.is_symlink():
        raise ValueError("Annotation must not be a symbolic link.")
    return GoldAnnotation.model_validate(strict_json(path.read_text(encoding="utf-8")))


def write_annotation(annotation, output):
    """Write a new working file. Publication is a separate, explicit action."""
    target = Path(output)
    text = annotation_to_json(annotation)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(text)


class GoldCheckReport(Config):
    errors: list[str] = Field(default_factory=list)

    @property
    def ready(self):
        return not self.errors


def check_annotation(annotation, *, material_root):
    """Check rules, fixed bytes and every evidence page without extracting text."""
    annotation = GoldAnnotation.model_validate(annotation.model_dump(mode="json"))
    report = GoldCheckReport()
    if annotation.metadata.rules.model_dump() != current_rules_ref():
        report.errors.append("Gold rules differ from the current scientific rules.")
    try:
        root = Path(material_root).resolve(strict=True)
        path = root / annotation.metadata.material.path
        if path.is_symlink():
            raise ValueError("PDF must not be a symbolic link.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != annotation.metadata.material.sha256:
            raise ValueError("PDF fingerprint differs from the registered content.")
        analysis = analyze_pdf(data, extract_text=False)
        if not analysis.openable or not analysis.page_count:
            raise ValueError("PDF cannot be opened: " + "; ".join(analysis.notes))
        for location, evidence in pdf_evidence(annotation):
            if evidence.page > analysis.page_count:
                report.errors.append(
                    f"{location}: page {evidence.page} exceeds {analysis.page_count} PDF pages."
                )
    except (OSError, ValueError) as exc:
        report.errors.append(str(exc))
    return report
