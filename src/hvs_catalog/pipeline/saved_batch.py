"""Run an explicit list of fixed executions across independent workspaces."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..records.common import Config
from ..records.roster_validation import strict_json
from ..runtime import get_execution
from ..workspace import open_workspace
from .batch import ExtractionTask, resume_extractions
from .configuration import stored_configuration
from .inputs import read_input


class SavedExtractionTask(Config):
    arxiv_id: str = Field(min_length=1)
    workspace: Path
    execution_id: str = Field(min_length=1)


class SavedExtractionBatch(Config):
    format: Literal["hvs.extraction-batch"] = "hvs.extraction-batch"
    version: Literal[1] = 1
    tasks: list[SavedExtractionTask] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_papers(self):
        if len({t.arxiv_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("A paper appears more than once in the extraction batch.")
        return self


def run_saved_batch(path, *, check_only=False, concurrency=None, env_file=None):
    """Validate every task before issuing requests; checks never construct a service."""
    from ..llm.managed import CallService

    path = Path(path)
    manifest = SavedExtractionBatch.model_validate(strict_json(path.read_text()))
    spaces, tasks, summaries, configs = {}, [], [], []
    identities = set()
    try:
        for item in manifest.tasks:
            root = (path.parent / item.workspace).resolve()
            identity = (root, item.execution_id)
            if identity in identities:
                raise ValueError("An execution appears more than once in the batch.")
            identities.add(identity)
            if root not in spaces:
                spaces[root] = open_workspace(root, read_only=check_only)
            space = spaces[root]
            handle = get_execution(space, item.execution_id)
            with space.lock():
                document, _, _ = read_input(space, handle.input_snapshot)
            if document["metadata"]["arxiv_id"] != item.arxiv_id:
                raise ValueError("An execution identifies a different batch paper.")
            config = stored_configuration(handle.input_snapshot)
            configs.append(config)
            tasks.append(ExtractionTask(space, item.execution_id))
            summaries.append(
                {
                    "arxiv_id": item.arxiv_id,
                    "workspace": str(root),
                    "execution_id": item.execution_id,
                    "input_digest": handle.input_digest,
                }
            )
        limit = min(min(c.parallelism, c.runtime.concurrency) for c in configs)
        if concurrency is not None and (concurrency < 1 or concurrency > limit):
            raise ValueError("Batch concurrency exceeds a frozen execution limit.")
        if check_only:
            return {"outcome": "ready", "executions": summaries}
        for task, config in zip(tasks, configs, strict=True):
            task.service = CallService(config.runtime, env_file=env_file)
        reports = resume_extractions(tasks, concurrency=concurrency)
        return {
            "outcome": "failed"
            if any(r.outcome == "failed" for r in reports)
            else "paused"
            if any(r.outcome == "paused" for r in reports)
            else "finished",
            "executions": [
                {**summary, **report.to_dict()}
                for summary, report in zip(summaries, reports, strict=True)
            ],
        }
    finally:
        for space in spaces.values():
            space.close()
