"""Freeze and verify the complete extraction input without acquiring new material."""

from ..llm.prompts import PromptInput
from ..records.common import ArtifactRef, RecordRef, sha256_of
from ..records.extraction_input import ExtractionInput
from ..runtime import build_input_snapshot
from ..workspace.archives import publish_json, read_json
from .configuration import freeze_configuration


def code_fingerprint():
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return sha256_of(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py"))
        }
    )


def freeze_input(space, config, preparation_execution_id, roster_ref, scope):
    from ..materials import MaterialsService
    from ..materials.metadata import fixed_metadata
    from .roster import material_prompt, prepared_bundle

    if scope not in {"full", "roster"} or (preparation_execution_id is None) == (
        roster_ref is None
    ):
        raise ValueError("Provide one preparation or one saved roster.")
    if roster_ref is not None:
        from ..records.roster import RosterRecord
        from ..records.validate import validate_collection
        from ..workspace.store import record_closure

        roster = space.store().read_record(roster_ref)
        if not isinstance(roster, RosterRecord):
            raise ValueError(
                "The roster reference does not identify a roster snapshot."
            )
        validate_collection(
            record_closure([roster], space.store().read_record), space.root
        )
        source = space.store().read_record(roster.data.source_ref)
        inputs = PromptInput.model_validate(read_json(space, roster.data.material_ref))
        material_ref = roster.data.material_ref
        bundle_ref = None
    else:
        bundle = MaterialsService(space).get_prepared_input(preparation_execution_id)
        bundle_ref = ArtifactRef.model_validate(bundle["bundle_ref"])
        source_ref = RecordRef.model_validate(bundle["prepared_source_ref"])
        bundle, source = prepared_bundle(space, bundle_ref, source_ref)
        inputs = material_prompt(space, bundle, source)
        material_ref = publish_json(
            space,
            f"runs/inputs/{sha256_of(inputs.model_dump(mode='json'))}/material.json",
            inputs.model_dump(mode="json"),
        )
    metadata_ref, metadata = fixed_metadata(space, source)
    frozen = freeze_configuration(space, config)
    frozen["options"].update(
        extraction_scope=scope,
        prepared_prompt=material_ref.model_dump_json(),
        extraction_code=code_fingerprint(),
    )
    if bundle_ref:
        frozen["options"]["prepared_input"] = bundle_ref.model_dump_json()
    if roster_ref:
        frozen["options"]["roster_ref"] = roster_ref.model_dump_json()
    snapshot = build_input_snapshot(
        source_refs=[source.ref()],
        method="extraction",
        models=[m.model_dump() for m in config.request([source.ref()]).models],
        expert_adjudication=False,
        **frozen,
    )
    document = {
        "format": "hvs.extraction-input",
        "version": 1,
        "metadata": metadata.model_dump(),
        "metadata_ref": metadata_ref.model_dump(),
        "material_ref": material_ref.model_dump(),
        "snapshot": snapshot.model_dump(mode="json"),
    }
    document = ExtractionInput.model_validate(document).model_dump(mode="json")
    ref = publish_json(space, f"runs/inputs/{sha256_of(document)}/input.json", document)
    snapshot.options["extraction_input"] = ref.model_dump_json()
    return snapshot


def read_input(space, snapshot):
    from ..records.literature_contribution import LiteratureMetadata
    from .configuration import validate_configuration_snapshot
    from .roster import material_prompt

    validate_configuration_snapshot(snapshot)
    if snapshot.options.get("extraction_code") != code_fingerprint():
        raise ValueError("Extraction code differs from the frozen input.")
    ref = ArtifactRef.model_validate_json(snapshot.options["extraction_input"])
    document = ExtractionInput.model_validate(read_json(space, ref)).model_dump(
        mode="json"
    )
    expected = snapshot.model_dump(mode="json")
    expected["options"].pop("extraction_input")
    if (
        document.get("format") != "hvs.extraction-input"
        or document.get("version") != 1
        or document["snapshot"] != expected
    ):
        raise ValueError("The extraction input differs from its frozen snapshot.")
    source = space.store().read_record(snapshot.source_refs[0])
    metadata = LiteratureMetadata.model_validate(document["metadata"])
    saved = read_json(space, ArtifactRef.model_validate(document["metadata_ref"]))
    if (
        saved.get("format") != "hvs.paper-metadata"
        or saved.get("version") != 1
        or saved["metadata"] != metadata.model_dump()
        or metadata.arxiv_id != source.data.paper_key
        or metadata.version != source.data.source_version
    ):
        raise ValueError("Metadata differs from the fixed source.")
    material = PromptInput.model_validate(
        read_json(space, ArtifactRef.model_validate(document["material_ref"]))
    )
    if material != material_prompt(space, {}, source):
        raise ValueError("The material differs from the fixed source.")
    return document, source, material
