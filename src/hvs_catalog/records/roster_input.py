"""The fixed scientific submission for roster extraction."""

from pydantic import Field

from .literature_contribution import (
    LiteratureContributionType,
    LiteratureIdentifier,
    _ScientificContent,
)
from .submission_schema import submission_schema


class RosterStar(_ScientificContent):
    identifiers: list[LiteratureIdentifier] = Field(
        min_length=1, description="Star identifiers for this star."
    )
    contribution_type: LiteratureContributionType


class RosterSubmission(_ScientificContent):
    stars: list[RosterStar] = Field(
        description="One entry per star. Use an empty array if no star meets the scientific rules."
    )
    notes: str = Field(
        description=(
            "For each star discussed, include one star identifier. "
            "If a note discusses a passage or image, include its complete location. "
            "Use an empty string if no note is necessary."
        )
    )


def roster_input_schema() -> dict:
    schema = submission_schema(
        RosterSubmission,
        {
            "RosterStar": "Star",
            "LiteratureIdentifier": "Identifier",
            "LiteratureContributionType": "ContributionType",
        },
    )
    identifier = schema["$defs"]["Identifier"]["properties"]
    identifier["value"]["description"] = "The star identifier."
    identifier["evidence"]["description"] = "Evidence for this star identifier."
    contribution = schema["$defs"]["ContributionType"]["properties"]
    contribution["value"]["description"] = (
        "The contribution category defined in the scientific rules."
    )
    contribution["evidence"]["description"] = "Evidence for this contribution category."
    return schema
