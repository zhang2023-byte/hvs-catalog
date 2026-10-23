"""Nonblocking quote observations and their exact scientific result binding."""

import re
from typing import Literal

from pydantic import Field

from .common import ArtifactRef, Config

QUOTE_WARNING_POLICY = "quote_warning_v1"
EvidencePolicy = Literal["quote_warning_v1"]


def uses_quote_warnings(policy):
    if policy not in (None, QUOTE_WARNING_POLICY):
        raise ValueError("Unsupported evidence acceptance policy.")
    return policy == QUOTE_WARNING_POLICY


class QuoteMismatch(Config):
    result_path: str = Field(
        description="JSON Pointer to the evidence in the bound scientific result."
    )
    location: str
    quote: str = Field(description="The submitted quote, preserved without changes.")
    reason: Literal["whitespace_difference", "exact_match_elsewhere", "unknown"]


class AuditMetadata(Config):
    arxiv_id: str
    arxiv_version: str = Field(description="The source paper version, such as v1.")
    result_ref: ArtifactRef


class EvidenceAudit(Config):
    metadata: AuditMetadata
    quote_mismatches: list[QuoteMismatch]


def quote_mismatches(payload, locations):
    """Classify observable matches without changing text or judging its meaning."""
    result = []
    texts = {
        location: bound.get("text", "")
        for location, bound in locations.items()
        if location.startswith("T:")
    }
    normalized = {}

    def normalize(text):
        return re.sub(r"\s+", " ", text).strip()

    def walk(value, path):
        if isinstance(value, dict):
            location, quote = value.get("location"), value.get("quote")
            if (
                isinstance(location, str)
                and location in texts
                and isinstance(quote, str)
                and quote
                and quote not in texts[location]
            ):
                if location not in normalized:
                    normalized[location] = normalize(texts[location])
                candidate = normalize(quote)
                if candidate and candidate in normalized[location]:
                    reason = "whitespace_difference"
                elif any(
                    quote in text for key, text in texts.items() if key != location
                ):
                    reason = "exact_match_elsewhere"
                else:
                    reason = "unknown"
                result.append(
                    QuoteMismatch(
                        result_path=path, location=location, quote=quote, reason=reason
                    )
                )
            for key, child in value.items():
                part = key.replace("~", "~0").replace("/", "~1")
                walk(child, path + "/" + part)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))

    walk(payload, "")
    return result
