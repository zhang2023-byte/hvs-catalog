"""Build and verify audit observations from retained stages and their actual requests."""

from ..llm.prompts import PromptInput
from ..records.common import ArtifactRef
from ..records.evidence_audit import (
    EvidenceAudit,
    quote_mismatches,
    uses_quote_warnings,
)
from ..records.material import supplied_locations
from ..records.roster_validation import evidence_issues, strict_json
from ..records.validate import _check_submission_binding
from ..workspace.archives import read_json


def retained_mismatches(space, content, roster, quantities, locations):
    """Use each selected stage's input, including explicitly reused roster inputs."""
    mismatches = []

    def check(record, payload, prefix="", target=None):
        request = _check_submission_binding(record, space, roster=target)
        if request is None:
            # A failed stage without a response cannot retain evidence.
            if payload.get("stars") or payload.get("quantities"):
                raise ValueError("Retained evidence requires an archived request.")
            return
        actual = supplied_locations(
            locations, request["payload"]["messages"][1]["content"]
        )
        warning_mode = uses_quote_warnings(record.data.evidence_policy)
        if evidence_issues(payload, actual, quote_warnings=warning_mode):
            raise ValueError("Retained scientific evidence failed validation.")
        if warning_mode:
            for item in quote_mismatches(payload, actual):
                mismatches.append(
                    item.model_copy(update={"result_path": prefix + item.result_path})
                )

    if roster is None:
        if content.stars:
            raise ValueError("Scientific stars require a retained roster.")
        return mismatches
    if len(content.stars) != len(roster.data.stars):
        raise ValueError("Scientific stars differ from the retained roster.")
    stars = [
        star.model_dump(mode="json", exclude={"quantities"}) for star in content.stars
    ]
    if stars != [star.model_dump(mode="json") for star in roster.data.stars]:
        raise ValueError("Scientific identifiers differ from the retained roster.")
    check(roster, {"stars": stars})
    for position, (source_index, star) in enumerate(
        zip(roster.data.source_indices, content.stars, strict=True)
    ):
        record = quantities.get(source_index)
        payload = {"quantities": [q.model_dump(mode="json") for q in star.quantities]}
        if record:
            if star.quantities != record.data.quantities:
                raise ValueError("Scientific values differ from the retained snapshot.")
            check(record, payload, f"/stars/{position}", roster)
        elif star.quantities:
            raise ValueError("Scientific values require a retained snapshot.")
    return mismatches


def make_audit(content, result_ref, mismatches):
    return EvidenceAudit(
        metadata={
            "arxiv_id": content.metadata.arxiv_id,
            "arxiv_version": content.metadata.version,
            "result_ref": result_ref,
        },
        quote_mismatches=mismatches,
    )


def verify_result_audit(space, summary, document, content, roster, quantities):
    """Require a complete audit only for the frozen warning policy and a result."""
    options = document["snapshot"]["options"]
    warning_mode = uses_quote_warnings(options.get("evidence_policy"))
    for record in [roster, *quantities.values()]:
        if (
            record
            and record.execution_id == summary.execution_id
            and record.data.evidence_policy != options.get("evidence_policy")
        ):
            raise ValueError("The snapshot evidence policy differs from its execution.")
    if content is None:
        if summary.evidence_audit_ref:
            raise ValueError("An evidence audit requires a scientific result.")
        return []
    if content.metadata.model_dump() != document["metadata"]:
        raise ValueError("The result metadata differs from the frozen input.")
    material_ref = ArtifactRef.model_validate(document["material_ref"])
    for record in [roster, *quantities.values()]:
        if record and record.data.material_ref != material_ref:
            raise ValueError("The stage material differs from the fixed input.")
    locations = PromptInput.model_validate(
        read_json(space, material_ref)
    ).citation_locations
    mismatches = retained_mismatches(space, content, roster, quantities, locations)
    if warning_mode:
        if summary.evidence_audit_ref is None:
            raise ValueError("The scientific result has no evidence audit.")
        audit = EvidenceAudit.model_validate(
            strict_json(space.read_file(summary.evidence_audit_ref).decode())
        )
        if audit != make_audit(content, summary.result_ref, mismatches):
            raise ValueError(
                "The evidence audit differs from the retained scientific result."
            )
    elif summary.evidence_audit_ref:
        raise ValueError("A legacy result cannot acquire a new evidence audit.")
    return mismatches
