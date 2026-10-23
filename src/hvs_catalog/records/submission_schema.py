"""Generate tool schemas from scientific models with shared filling instructions."""

from pydantic import BaseModel


def submission_schema(model: type[BaseModel], names: dict[str, str]) -> dict:
    """Preserve model constraints while naming and documenting tool definitions."""
    schema = model.model_json_schema()

    def clean(value):
        if isinstance(value, dict):
            value.pop("title", None)
            if "$ref" in value:
                name = value["$ref"].removeprefix("#/$defs/")
                value["$ref"] = "#/$defs/" + names.get(name, name)
            if "anyOf" in value and {
                x.get("$ref", "").split("/")[-1] for x in value["anyOf"]
            } == {"TextEvidence", "ImageEvidence"}:
                value["oneOf"] = value.pop("anyOf")
            for child in value.values():
                clean(child)
        elif isinstance(value, list):
            for child in value:
                clean(child)

    clean(schema)
    definitions = schema["$defs"] = {
        names.get(name, name): value for name, value in schema["$defs"].items()
    }
    evidence_list = None
    for definition in definitions.values():
        definition.pop("description", None)
        properties = definition.get("properties", {})
        if "evidence" not in properties:
            continue
        evidence = properties["evidence"].copy()
        description = evidence.pop("description")
        if evidence_list is not None and evidence != evidence_list:
            raise ValueError("Submission fields must share the evidence contract")
        evidence_list = evidence
        properties["evidence"] = {
            "$ref": "#/$defs/EvidenceList",
            "description": description,
        }
    if evidence_list is None:
        raise ValueError("A submission must contain evidence fields")
    definitions["EvidenceList"] = {
        **evidence_list,
        "description": "Use separate items for different locations.",
    }
    text = definitions["TextEvidence"]
    text["description"] = "Evidence from text or a caption."
    text["properties"]["location"]["description"] = (
        "Copy the complete T: location, such as T:p3-b5. "
        "Exclude LOCATION and the block type."
    )
    text["properties"]["quote"]["description"] = (
        "Copy a nonempty excerpt from the corresponding text block without changes. "
        "Keep its original language."
    )
    image = definitions["ImageEvidence"]
    image["description"] = "Evidence from an image. Do not include a quote field."
    image["properties"]["location"]["description"] = (
        "Copy the complete I: location, such as I:p5-b6. Exclude LOCATION."
    )
    return schema
