"""Explicit monthly discovery, download, and PDF preparation batches.

Only scope, version targets, and orchestration diagnostics belong to the batch.
Discovery pages, acquisition facts, and preparation executions remain authoritative.
Reports are derived snapshots; reading or exporting them never calls external tools.
"""

from __future__ import annotations

import calendar
import csv
import io
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..materials import MaterialsService, OfficialMinerU
from ..materials.flow import parser_fingerprint
from ..records.common import ArtifactRef, Config, canonical_json, sha256_of
from ..records.pdf import pdf_page_count
from ..workspace import Workspace, WorkspaceConflictError
from .acquisition import AcquisitionService, ArxivSearchRequest, parse_arxiv_identifier
from .acquisition.lifecycle import AcquisitionError, discovery_round_id
from .acquisition.protocol import ArxivApiService
from .acquisition.transport import ArxivAuthError

Progress = Callable[[dict], None]


class BatchIssue(Config):
    stage: Literal["download", "parse"]
    message: str
    attempted_at: datetime


class BatchDefinition(Config):
    kind: Literal["hvs.preparation-batch"] = "hvs.preparation-batch"
    version: Literal[1] = 1
    batch_id: str = Field(min_length=1)
    created_at: datetime
    submitted_from: date
    submitted_to: date
    terms: tuple[str, ...]
    round_seq: int = Field(default=1, ge=1)
    selected_versions: dict[str, str] = Field(default_factory=dict)
    preparation_attempts: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    issues: dict[str, BatchIssue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope(self):
        if not self.terms or len(set(self.terms)) != len(self.terms):
            raise ValueError("批次检索词必须非空且不重复")
        for term in self.terms:
            request = ArxivSearchRequest(self.submitted_from, self.submitted_to, term)
            if not request.terms or request.terms != term:
                raise ValueError("批次检索词必须使用规范化的非空短语")
        for paper, versioned in self.selected_versions.items():
            ident = parse_arxiv_identifier(versioned)
            if ident.canonical_id != paper or ident.version is None:
                raise ValueError("批次固定版本身份不符")
        if any(
            paper not in self.selected_versions
            or not keys
            or any(not key for key in keys)
            for paper, keys in self.preparation_attempts.items()
        ):
            raise ValueError("解析重试必须关联批次已固定的论文版本")
        return self


def monthly_requests(batch: BatchDefinition) -> Iterator[ArxivSearchRequest]:
    """Intersect calendar months with inclusive UTC first-submission boundaries."""
    month = batch.submitted_from.replace(day=1)
    while month <= batch.submitted_to:
        last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
        for term in batch.terms:
            yield ArxivSearchRequest(
                max(month, batch.submitted_from), min(last, batch.submitted_to), term
            )
        if last >= batch.submitted_to:
            break
        month = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else date(month.year, month.month + 1, 1)
        )


def _key(batch_id: str) -> str:
    return "preparation-batch:" + sha256_of(batch_id)


def _preparation_key(batch_id: str, versioned_id: str) -> str:
    return "batch-prepare:" + sha256_of([batch_id, versioned_id])


def _utc_day(value: str) -> date | None:
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return None
        return timestamp.astimezone(UTC).date()
    except (TypeError, ValueError):
        return None


class PreparationBatchService:
    def __init__(
        self, space: Workspace, *, api=None, runner: OfficialMinerU | None = None
    ):
        self.space = space
        self.api = api if api is not None else ArxivApiService()
        self.acquisition = AcquisitionService(space, self.api)
        self.runner = runner or OfficialMinerU(space)
        self.materials = MaterialsService(space, runner=self.runner)

    def _load(self, batch_id: str) -> BatchDefinition:
        raw = self.space.store().get_meta(_key(batch_id))
        if raw is None:
            raise AcquisitionError(f"批次不存在：{batch_id}")
        batch = BatchDefinition.model_validate_json(raw)
        if batch.batch_id != batch_id:
            raise AcquisitionError("批次身份不符")
        return batch

    def _save(self, batch: BatchDefinition) -> None:
        self.space.store().set_meta(_key(batch.batch_id), batch.model_dump_json())

    def create_batch(
        self,
        batch_id: str,
        *,
        submitted_from: date,
        submitted_to: date,
        terms: Sequence[str],
        round_seq: int = 1,
    ) -> BatchDefinition:
        normalized = tuple(
            dict.fromkeys(
                ArxivSearchRequest(submitted_from, submitted_to, term).terms
                for term in terms
            )
        )
        wanted = BatchDefinition(
            batch_id=batch_id,
            created_at=datetime.now(UTC),
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            terms=normalized,
            round_seq=round_seq,
        )
        with self.space.lock():
            if self.space.store().get_meta(_key(batch_id)) is not None:
                existing = self._load(batch_id)
                fields = ("submitted_from", "submitted_to", "terms", "round_seq")
                if any(getattr(existing, f) != getattr(wanted, f) for f in fields):
                    raise WorkspaceConflictError("同一批次不能改变检索范围；请另建批次")
                return existing
            self._save(wanted)
        return wanted

    def discover(
        self,
        batch_id: str,
        *,
        max_queries: int | None = None,
        progress: Progress | None = None,
    ) -> None:
        """Run/resume discovery only; do not resolve versions, download, or parse."""
        if max_queries is not None and max_queries < 1:
            raise ValueError("max_queries 必须是正整数")
        with self.space.lock():
            batch = self._load(batch_id)
            requests = list(monthly_requests(batch))
            advanced = 0
            for number, request in enumerate(requests, 1):
                round_id = discovery_round_id(request, batch.round_seq)
                existing = self.acquisition.get_discovery_round(round_id)
                if existing is not None and existing.state == "complete":
                    continue
                event = {
                    "stage": "discover",
                    "current": number,
                    "total": len(requests),
                    "terms": request.terms,
                    "from": request.submitted_from.isoformat(),
                    "to": request.submitted_to.isoformat(),
                    "round_id": round_id,
                }
                if progress:
                    progress({**event, "state": "started"})
                result = self.acquisition.run_discovery(
                    request, round_seq=batch.round_seq
                )
                if progress:
                    progress(
                        {**event, "state": result.state, "papers": len(result.papers)}
                    )
                advanced += 1
                if result.state != "complete" or (
                    max_queries is not None and advanced >= max_queries
                ):
                    break

    def get_manifest(self, batch_id: str) -> dict:
        """Derive a consistent report from saved pages, exact versions, and executions."""
        with self.space.lock():
            batch = self._load(batch_id)
            rounds, excluded, papers = [], [], {}
            accepted_entries = 0
            term_ids = {term: set() for term in batch.terms}
            for request in monthly_requests(batch):
                round_id = discovery_round_id(request, batch.round_seq)
                status = self.acquisition.get_discovery_round(round_id)
                row = {
                    "round_id": round_id,
                    "terms": request.terms,
                    "from": request.submitted_from.isoformat(),
                    "to": request.submitted_to.isoformat(),
                    "query": request.search_query(),
                    "state": status.state if status else "pending",
                    "reason": status.reason if status else None,
                    "pages": list(status.pages) if status else [],
                    "accepted_entries": 0,
                    "raw_entries": 0,
                }
                rounds.append(row)
                if status is None:
                    continue
                for page in status.pages:
                    document = json.loads(
                        self.space.read_file(
                            ArtifactRef(
                                path=page["raw_path"], sha256=page["raw_sha256"]
                            )
                        )
                    )
                    for meta in document["entries"]:
                        row["raw_entries"] += 1
                        source = {
                            "round_id": round_id,
                            "terms": request.terms,
                            "page_record": {
                                "record_id": page["record_id"],
                                "revision": page["revision"],
                            },
                            "page_file": {
                                "path": page["raw_path"],
                                "sha256": page["raw_sha256"],
                            },
                            "fetched_at": page["fetched_at"],
                            "observed_version": meta["version"],
                        }
                        published = _utc_day(meta["published"])
                        if (
                            published is None
                            or not request.submitted_from
                            <= published
                            <= request.submitted_to
                        ):
                            excluded.append(
                                {
                                    "arxiv_id": meta["canonical_id"],
                                    "published": meta["published"],
                                    "reason": "invalid_date"
                                    if published is None
                                    else "outside_window",
                                    "source": source,
                                }
                            )
                            continue
                        row["accepted_entries"] += 1
                        accepted_entries += 1
                        canonical = meta["canonical_id"]
                        term_ids[request.terms].add(canonical)
                        if canonical not in papers:
                            papers[canonical] = {
                                "arxiv_id": canonical,
                                "metadata": meta,
                                "matched_terms": [],
                                "discovery_sources": [],
                            }
                        paper = papers[canonical]
                        if meta["version"] > paper["metadata"]["version"]:
                            paper["metadata"] = meta
                        if request.terms not in paper["matched_terms"]:
                            paper["matched_terms"].append(request.terms)
                        paper["discovery_sources"].append(source)
            for canonical, paper in papers.items():
                target = batch.selected_versions.get(canonical)
                selection = self.acquisition.get_version(target) if target else None
                paper["download"] = {
                    "state": selection.state if selection else "pending",
                    "versioned_id": target,
                    "source_ref": selection.source_record.model_dump(mode="json")
                    if selection and selection.source_record
                    else None,
                    "error": selection.error if selection else None,
                }
                keys = (
                    [
                        _preparation_key(batch_id, target),
                        *batch.preparation_attempts.get(canonical, ()),
                    ]
                    if target
                    else []
                )
                executions = [
                    self.space.store().find_execution_by_request(key) for key in keys
                ]
                execution = executions[-1] if executions else None
                saved = (
                    self.materials.get_preparation(execution.execution_id)
                    if execution
                    else None
                )
                bundle = (
                    self.materials.get_prepared_input(execution.execution_id)
                    if saved
                    and saved["state"] == "finished"
                    and saved["summary"] is not None
                    else None
                )
                paper["preparation"] = {
                    "state": saved["state"] if saved else "pending",
                    "execution_id": execution.execution_id if execution else None,
                    "summary": saved["summary"] if saved else None,
                    "errors": saved["errors"] if saved else [],
                    "prepared_source_ref": bundle["prepared_source_ref"]
                    if bundle
                    else None,
                    "prepared_input_ref": bundle["bundle_ref"] if bundle else None,
                    "previous_execution_ids": list(
                        dict.fromkeys(
                            e.execution_id for e in executions[:-1] if e is not None
                        )
                    ),
                }
                paper["issue"] = (
                    batch.issues[canonical].model_dump(mode="json")
                    if canonical in batch.issues
                    else None
                )
            ordered = sorted(
                papers.values(),
                key=lambda p: (p["metadata"]["published"], p["arxiv_id"]),
            )
            return {
                "kind": "hvs.preparation-manifest",
                "version": 1,
                "batch_id": batch_id,
                "space": str(self.space.root),
                "created_at": batch.created_at.isoformat(),
                "generated_at": datetime.now(UTC).isoformat(),
                "scope": {
                    "submitted_from": batch.submitted_from.isoformat(),
                    "submitted_to": batch.submitted_to.isoformat(),
                    "timezone": "UTC",
                    "terms": list(batch.terms),
                    "categories": [],
                    "search_field": "all",
                    "date_policy": "first_submission_inclusive",
                    "pdf_version_policy": "latest_at_acquisition",
                    "round_seq": batch.round_seq,
                },
                "discovery_complete": all(r["state"] == "complete" for r in rounds),
                "summary": {
                    "queries": len(rounds),
                    "completed_queries": sum(r["state"] == "complete" for r in rounds),
                    "unique_papers": len(ordered),
                    "accepted_entries": accepted_entries,
                    "duplicate_entries": accepted_entries - len(ordered),
                    "excluded_entries": len(excluded),
                    "terms": {term: len(ids) for term, ids in term_ids.items()},
                    "downloaded": sum(
                        p["download"]["state"] in {"downloaded", "imported"}
                        for p in ordered
                    ),
                    "prepared": sum(
                        p["preparation"]["state"] == "finished" for p in ordered
                    ),
                },
                "rounds": rounds,
                "papers": ordered,
                "excluded": excluded,
            }

    def _scope(self, batch_id: str, identifiers: Sequence[str] | None) -> list[dict]:
        manifest = self.get_manifest(batch_id)
        if not manifest["discovery_complete"]:
            raise AcquisitionError("检索未完整结束，不能开始下载或解析")
        papers = {p["arxiv_id"]: p for p in manifest["papers"]}
        wanted = (
            list(dict.fromkeys(identifiers))
            if identifiers is not None
            else list(papers)
        )
        if set(wanted) - papers.keys():
            raise ValueError("指定论文不在批次去重清单中")
        return [papers[paper] for paper in wanted]

    def select_preparation(
        self, batch_id: str, paper_id: str, preparation_id: str
    ) -> None:
        """Select a finished offline revision of the current fixed preparation.

        Append its request key to the existing history. A stale predecessor or
        another PDF cannot replace the current batch selection.
        """
        with self.space.lock():
            paper = self._scope(batch_id, [paper_id])[0]
            if paper["preparation"]["execution_id"] == preparation_id:
                self.materials.consume_prepared_input(preparation_id)
                return
            self.materials.consume_prepared_input(preparation_id)
            saved = self.materials.get_preparation(preparation_id)["config"]["request"]
            if (
                saved.get("rebuild", {}).get("preparation_id")
                != paper["preparation"]["execution_id"]
                or saved["source_ref"] != paper["download"]["source_ref"]
            ):
                raise ValueError("材料版本必须派生自本批次当前准备及同一固定 PDF")
            from ..runtime import get_execution

            execution = get_execution(self.space, preparation_id)
            batch = self._load(batch_id)
            keys = batch.preparation_attempts.get(paper_id, ())
            batch.preparation_attempts[paper_id] = (*keys, execution.request_key)
            self._save(batch)

    def download(
        self, batch_id: str, *, identifiers=None, progress: Progress | None = None
    ) -> None:
        """Explicitly resolve the latest version once, save it, then acquire exact PDFs."""
        with self.space.lock():
            batch = self._load(batch_id)
            papers = self._scope(batch_id, identifiers)
            for number, paper in enumerate(papers, 1):
                canonical = paper["arxiv_id"]
                try:
                    if canonical not in batch.selected_versions:
                        lookup = self.api.metadata_by_ids([canonical])
                        found = [
                            item
                            for item in lookup.lookups
                            if item.requested.canonical_id == canonical
                        ]
                        if (
                            lookup.anomalies
                            or len(found) != 1
                            or found[0].status != "found"
                            or found[0].meta is None
                        ):
                            raise AcquisitionError(f"未能核实 {canonical} 的最新版")
                        meta = found[0].meta
                        if meta.canonical_id != canonical:
                            raise AcquisitionError("最新版元数据身份不符")
                        if self.space.is_literature:
                            from ..materials.metadata import archive_metadata

                            archive_metadata(
                                self.space,
                                meta,
                                {
                                    "response_url": lookup.response_url,
                                    "fetched_at": lookup.fetched_at,
                                },
                            )
                        target = parse_arxiv_identifier(f"{canonical}v{meta.version}")
                        batch.selected_versions[canonical] = target.versioned_id
                        self._save(batch)
                    acquired = self.acquisition.acquire(
                        batch.selected_versions[canonical]
                    )
                    if self.space.is_literature:
                        from ..materials.metadata import fixed_metadata
                        from ..workspace.archives import read_json

                        source = self.space.store().read_record(acquired.material_ref)
                        metadata_ref, _ = fixed_metadata(self.space, source)
                        metadata = read_json(self.space, metadata_ref)
                        pdf = next(f for f in source.data.files if f.role == "pdf")
                        metadata.update(
                            metadata_ref=metadata_ref.model_dump(mode="json"),
                            pdf={"path": pdf.path, "sha256": pdf.sha256},
                        )
                        self.space.publish_file(
                            self.space.paper_path(canonical, "metadata.json"),
                            (canonical_json(metadata) + "\n").encode(),
                        )
                    batch.issues.pop(canonical, None)
                except ArxivAuthError as exc:
                    self._issue(batch, canonical, "download", exc)
                    self._save(batch)
                    raise
                except Exception as exc:  # noqa: BLE001 -- Retain each failed paper while other papers continue.
                    # Transport/runtime failures are per-paper diagnostics, never empty success.
                    self._issue(batch, canonical, "download", exc)
                self._save(batch)
                if progress:
                    progress(
                        {
                            "stage": "download",
                            "current": number,
                            "total": len(papers),
                            "paper": canonical,
                            "state": "failed"
                            if canonical in batch.issues
                            else "finished",
                        }
                    )

    def parse(
        self,
        batch_id: str,
        *,
        identifiers=None,
        retry_failed=False,
        progress: Progress | None = None,
    ) -> None:
        """Explicitly prepare downloaded fixed sources; reuse saved executions on recovery."""
        with self.space.lock():
            batch = self._load(batch_id)
            papers = self._scope(batch_id, identifiers)
            if retry_failed:
                if identifiers is None:
                    raise ValueError("解析重试必须用 --id 显式指定论文")
                if any(
                    p["preparation"]["state"] not in {"finished", "failed"}
                    for p in papers
                ):
                    raise ValueError("--retry-failed 不得处理尚未完成首次解析的论文")
            if hasattr(self.runner, "prepare_batch"):
                inputs = []
                for paper in papers:
                    if paper["preparation"]["state"] == "finished":
                        continue
                    ref = paper["download"]["source_ref"]
                    if ref is None:
                        continue
                    from ..records.common import RecordRef

                    source = self.space.store().read_record(
                        RecordRef.model_validate(ref)
                    )
                    pdf = next(f for f in source.data.files if f.role == "pdf")
                    payload = self.space.read_file(
                        ArtifactRef(path=pdf.path, sha256=pdf.sha256)
                    )
                    inputs.append((self.space.root / pdf.path, pdf_page_count(payload)))
                self.runner.prepare_batch(inputs)
            for number, paper in enumerate(papers, 1):
                canonical = paper["arxiv_id"]
                try:
                    target = batch.selected_versions.get(canonical)
                    selection = self.acquisition.get_version(target) if target else None
                    if (
                        selection is None
                        or selection.state not in {"downloaded", "imported"}
                        or selection.source_record is None
                    ):
                        raise AcquisitionError("固定版本 PDF 尚未下载完成")
                    if paper["preparation"]["state"] == "finished":
                        # Completed material facts remain immutable across parser changes.
                        if progress:
                            progress(
                                {
                                    "stage": "parse",
                                    "current": number,
                                    "total": len(papers),
                                    "paper": canonical,
                                    "state": "finished",
                                }
                            )
                        continue
                    base_key = _preparation_key(batch_id, target)
                    keys = batch.preparation_attempts.get(canonical, ())
                    request_key = keys[-1] if keys else base_key
                    if retry_failed:
                        request_key = (
                            base_key
                            + ":retry:"
                            + sha256_of(
                                [parser_fingerprint(), self.runner.fingerprint()]
                            )
                        )
                        if not keys or keys[-1] != request_key:
                            batch.preparation_attempts[canonical] = (*keys, request_key)
                            # Save the association before starting a new durable execution.
                            self._save(batch)
                    existing = self.space.store().find_execution_by_request(request_key)
                    if existing:
                        report = self.materials.resume_preparation(
                            existing.execution_id
                        )
                    else:
                        _, report = self.materials.prepare_materials(
                            request_key=request_key, source_ref=selection.source_record
                        )
                    if report.outcome != "finished":
                        raise AcquisitionError(
                            "; ".join(report.errors) or report.outcome
                        )
                    batch.issues.pop(canonical, None)
                except Exception as exc:  # noqa: BLE001 -- Retain each failed paper while other papers continue.
                    self._issue(batch, canonical, "parse", exc)
                self._save(batch)
                if progress:
                    progress(
                        {
                            "stage": "parse",
                            "current": number,
                            "total": len(papers),
                            "paper": canonical,
                            "state": "failed"
                            if canonical in batch.issues
                            else "finished",
                        }
                    )

    @staticmethod
    def _issue(
        batch: BatchDefinition, canonical: str, stage: str, exc: Exception
    ) -> None:
        batch.issues[canonical] = BatchIssue(
            stage=stage,
            message=f"{type(exc).__name__}: {exc}",
            attempted_at=datetime.now(UTC),
        )


def _write_snapshot(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_manifest(manifest: dict, output: Path) -> tuple[Path, Path]:
    """Export replaceable JSON/CSV snapshots from one consistent manifest."""
    json_path, csv_path = (
        output / "preparation-manifest.json",
        output / "preparation-list.csv",
    )
    _write_snapshot(
        json_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "arxiv_id",
            "title",
            "first_submitted_utc",
            "matched_terms",
            "observed_version",
            "fixed_pdf_version",
            "download_state",
            "parse_state",
            "preparation_execution",
            "materials_complete",
            "issue",
        ]
    )
    for paper in manifest["papers"]:
        summary = paper["preparation"]["summary"] or {}
        writer.writerow(
            [
                paper["arxiv_id"],
                paper["metadata"]["title"],
                paper["metadata"]["published"],
                "; ".join(paper["matched_terms"]),
                paper["metadata"]["version"],
                paper["download"]["versioned_id"],
                paper["download"]["state"],
                paper["preparation"]["state"],
                paper["preparation"]["execution_id"],
                summary.get("materials_complete"),
                canonical_json(paper["issue"]) if paper["issue"] else "",
            ]
        )
    _write_snapshot(csv_path, buffer.getvalue())
    return json_path, csv_path
