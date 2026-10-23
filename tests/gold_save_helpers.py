"""Independent fictional complete annotations and fixed two-page PDFs."""

import hashlib

from reportlab.pdfgen import canvas

from hvs_catalog.evaluation.gold import write_annotation
from hvs_catalog.evaluation.store import inspect_save
from hvs_catalog.records.pdf_contribution import GoldAnnotation, PdfContribution
from hvs_catalog.records.rules import current_rules_ref


def make_annotation(tmp_path):
    material = tmp_path / "materials"
    material.mkdir(parents=True)
    pdf = material / "paper.pdf"
    document = canvas.Canvas(str(pdf))
    document.drawString(
        30, 700, "SYN-A is a candidate. Radial velocity 520 +/- 10 km/s."
    )
    document.showPage()
    document.drawString(30, 700, "Table 2. SYN-A distance < 8.0 kpc.")
    document.save()
    evidence = [
        {
            "page": 1,
            "location": "Candidate paragraph",
        }
    ]
    answer = PdfContribution.model_validate(
        {
            "metadata": {
                "arxiv_id": "2601.99999",
                "version": "v1",
                "title": "Fictional two-page paper",
                "authors": ["A. Example"],
            },
            "stars": [
                {
                    "identifiers": [{"value": "SYN-A", "evidence": evidence}],
                    "contribution_type": {
                        "value": "candidates_found",
                        "evidence": evidence,
                    },
                    "quantities": [
                        {
                            "quantity": "radial_velocity",
                            "values": [
                                {
                                    "value": "520",
                                    "error": "10",
                                    "unit": "km/s",
                                    "evidence": evidence,
                                }
                            ],
                        },
                        {
                            "quantity": "distance",
                            "values": [
                                {
                                    "upper_bound": "8.0",
                                    "upper_inclusive": False,
                                    "unit": "kpc",
                                    "condition": "Model A",
                                    "evidence": [
                                        {
                                            "page": 2,
                                            "location": "Table 2, SYN-A row",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
            "notes": [],
        }
    )
    raw = answer.model_dump(mode="json")
    raw["metadata"].update(
        rules=current_rules_ref(),
        material={
            "path": "paper.pdf",
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        },
    )
    annotation = GoldAnnotation.model_validate(raw)
    write_annotation(annotation, material / "annotation.json")
    return annotation, material


def flow(tmp_path):
    _, root = make_annotation(tmp_path)
    data = tmp_path / "repo/data"
    data.mkdir(parents=True)
    target = data / "2601.99999"
    args = {"annotation": root / "annotation.json", "target": target}
    args["expected_new"] = inspect_save(**args)["annotation_sha256"]
    return args


def revision(args, root):
    import shutil

    from hvs_catalog.evaluation.gold import read_annotation
    from hvs_catalog.evaluation.store import read_saved

    root.mkdir()
    shutil.copyfile(args["annotation"].parent / "paper.pdf", root / "paper.pdf")
    annotation = read_annotation(args["annotation"])
    annotation.notes.append("Revised synthetic note.")
    write_annotation(annotation, root / "annotation.json")
    result = {
        "annotation": root / "annotation.json",
        "target": args["target"],
        "expected_old": read_saved(args["target"])["annotation_sha256"],
    }
    result["expected_new"] = inspect_save(**result)["annotation_sha256"]
    return result
