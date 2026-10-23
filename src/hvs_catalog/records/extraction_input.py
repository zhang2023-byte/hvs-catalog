"""The versioned extraction input binds metadata, material, and execution settings."""

from typing import Literal

from .common import ArtifactRef, Config
from .literature_contribution import LiteratureMetadata
from .runs import InputSnapshot


class ExtractionInput(Config):
    format: Literal["hvs.extraction-input"] = "hvs.extraction-input"
    version: Literal[1] = 1
    metadata: LiteratureMetadata
    metadata_ref: ArtifactRef
    material_ref: ArtifactRef
    snapshot: InputSnapshot
