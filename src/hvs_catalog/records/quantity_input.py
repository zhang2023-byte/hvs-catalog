"""The approved quantity submission shape and field descriptions."""

from pydantic import Field

from .literature_contribution import LiteratureQuantity, _ScientificContent
from .submission_schema import submission_schema


class QuantitySubmission(_ScientificContent):
    quantities: list[LiteratureQuantity] = Field(
        description=(
            "One entry per quantity with at least one retained value. "
            "Use an empty array if no value meets the scientific rules."
        )
    )
    notes: str = Field(
        description=(
            "If a note discusses a passage or image, include its complete location. "
            "Use an empty string if no note is necessary."
        )
    )


def quantity_input_schema() -> dict:
    schema = submission_schema(
        QuantitySubmission,
        {"LiteratureQuantity": "Quantity", "LiteratureValue": "Value"},
    )
    quantity = schema["$defs"]["Quantity"]["properties"]
    quantity["quantity"]["description"] = (
        "The quantity identifier from the scientific rules."
    )
    quantity["values"]["description"] = "The results for this quantity."
    value = schema["$defs"]["Value"]
    value["description"] = (
        "For each result, provide either a point value with any reported errors or one or two bounds. "
        "Include only the error sides that the paper reports. "
        "Do not combine symmetric and asymmetric errors. "
        "Use both bound fields for a pure interval.\n\n"
        "Preserve signs, precision, trailing zeros, coordinate notation, and unevaluated expressions.\n\n"
        "Omit optional fields when the paper does not report the corresponding information. "
        "Do not use null, empty strings, or default values."
    )
    properties = value["properties"]
    for side in ("lower", "upper"):
        properties[side + "_error"]["description"] = f"Reported {side} error."
        properties[side + "_inclusive"]["description"] = (
            f"Include only when the paper explicitly includes or excludes the {side} endpoint. "
            "Use true for inclusion and false for exclusion."
        )
    properties["unit"]["description"] = "Unit for this result."
    properties["condition"]["description"] = (
        "Relevant method, model, reference frame, or statistical meaning. "
        "Include applicable confidence levels and quantiles when the paper reports them."
    )
    properties["evidence"]["description"] = (
        "Evidence for this result, including its numerical components, unit, and condition."
    )
    return schema
