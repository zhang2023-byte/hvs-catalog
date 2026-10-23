"""Fixed source packages, preparation diagnostics, and material locators."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Config, Record, RecordRef

ReadState = Literal["ready", "unparsed", "unreadable"]


class SyntheticOrigin(Config):
    kind: Literal["synthetic"]


class ArxivOrigin(Config):
    kind: Literal["arxiv"]
    arxiv_id: str = Field(min_length=1)
    retrieval: str = Field(
        min_length=1,
        description="How and when the source package was retrieved, e.g. the download basis.",
    )


class LocalPdfOrigin(Config):
    kind: Literal["local_pdf"]
    provenance: str = Field(min_length=1)
    doi: str | None = None


SourceOrigin = Annotated[
    SyntheticOrigin | ArxivOrigin | LocalPdfOrigin, Field(discriminator="kind")
]


class SourceFile(Config):
    file_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    role: str = Field(min_length=1)
    read_state: ReadState
    derived_from: list[str]

    @model_validator(mode="after")
    def _relative_path(self) -> SourceFile:
        parts = self.path.replace("\\", "/").split("/")
        if self.path.startswith(("/", "~")) or any(
            part in {"", ".."} for part in parts
        ):
            raise ValueError(f"包内文件路径越界或不是相对路径：{self.path}")
        return self


class SourcePackageData(Config):
    paper_key: str = Field(min_length=1)
    origin: SourceOrigin
    source_version: str = Field(min_length=1)
    files: list[SourceFile] = Field(min_length=0)
    preparation_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_ids(self) -> SourcePackageData:
        file_ids = [f.file_id for f in self.files]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("来源包内 file_id 重复")
        known = set(file_ids)
        for file in self.files:
            unknown = [item for item in file.derived_from if item not in known]
            if unknown:
                raise ValueError(f"derived_from 引用了不存在的文件：{unknown}")
        return self


class TextLocator(Config):
    kind: Literal["text"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered_lines(self) -> TextLocator:
        if self.end_line < self.start_line:
            raise ValueError("end_line 不能小于 start_line")
        return self


class TableCellLocator(Config):
    kind: Literal["table_cell"]
    row_key: dict[str, object] = Field(min_length=1)
    column: str = Field(min_length=1)
    row_number: int | None = Field(ge=1)


class DataElementSelector(Config):
    kind: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_selector(self) -> DataElementSelector:
        if self.kind == "json_pointer" and not self.value.startswith("/"):
            raise ValueError("JSON Pointer 定位必须以 / 开头")
        return self


class DataElementDecoder(Config):
    name: str = Field(min_length=1)
    encoding: str | None


class DataElementLocator(Config):
    kind: Literal["data_element"]
    selector: DataElementSelector
    decoder: DataElementDecoder


class PdfLocator(Config):
    kind: Literal["pdf"]
    page: int = Field(ge=1, description="One-based page in the original PDF.")
    block_id: str = Field(
        min_length=1, description="Supplied content block on that page."
    )


class PdfImageLocator(PdfLocator):
    """An image-bound transcription; only the location and image bytes are validated."""

    kind: Literal["pdf_image"]


Locator = Annotated[
    TextLocator | TableCellLocator | DataElementLocator | PdfLocator | PdfImageLocator,
    Field(discriminator="kind"),
]


class EvidenceData(Config):
    paper_key: str = Field(min_length=1)
    source_ref: RecordRef
    file_id: str = Field(min_length=1)
    locator: Locator
    excerpt: str = Field(min_length=1)


class SourcePackageRecord(Record[SourcePackageData]):
    RECORD_NAME = "source_package"


class EvidenceRecord(Record[EvidenceData]):
    RECORD_NAME = "evidence"
