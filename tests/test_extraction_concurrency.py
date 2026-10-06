"""Request overlap, pipeline dependencies and serialized archival ownership."""

from __future__ import annotations

import threading

import pytest
import test_pdf_quantities as numerical_fixtures
from materials_helpers import FakeMinerU, make_pdf
from pydantic import SecretStr

# Reuse the prepared numerical fixture and its independently fixed expressions.
from test_pdf_quantities import (
    LINES,
    Adapter,
    object_id,
    quantities,
)
from test_roster import ScriptedAdapter

from hvs_catalog.llm import ModelConfig, RuntimeConfig
from hvs_catalog.llm.concurrent import ConcurrentCalls
from hvs_catalog.llm.config import CallRequest, Message
from hvs_catalog.llm.managed import CallService
from hvs_catalog.llm.transport import TransportFailure
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline import (
    ExtractionTask,
    resume_extractions,
    start_pdf_extraction,
)
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.backup import backup_workspace, restore_workspace


@pytest.fixture
def prepared_for_parallel(tmp_path):
    yield from numerical_fixtures.prepared_pdf.__wrapped__(tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("concurrency", [1, 4, 100])
def test_worker_limit_and_archive_owner(tmp_path, concurrency):
    barrier = threading.Barrier(concurrency, timeout=30)
    guard = threading.Lock()
    active = peak = 0
    owner = threading.get_ident()

    class Overlap(ScriptedAdapter):
        def send(self, payload, runtime, credential):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                return ScriptedAdapter([{}]).send(payload, runtime, credential)
            finally:
                with guard:
                    active -= 1

    runtime = RuntimeConfig(quota_store=tmp_path / "quota.sqlite", max_attempts=1)
    service = CallService(
        runtime, credential=SecretStr("synthetic"), adapter=Overlap([])
    )
    request = CallRequest(
        model=ModelConfig(model="deepseek-v4.1-flash"),
        messages=[Message(role="user", content="Fictional transport overlap")],
        tool={"name": "submit", "parameters": {"type": "object"}},
    )
    prepared = service.adapter.prepare(request)
    archived = []

    def archive(envelope):
        assert threading.get_ident() == owner
        assert envelope["error"] is None
        archived.append(envelope)

    dispatcher = ConcurrentCalls(concurrency)
    try:
        for index in range(100):
            dispatcher.submit((index,), service, request, prepared, archive)
        while dispatcher.jobs:
            dispatcher.collect()
    finally:
        dispatcher.close()
    assert len(archived) == 100 and peak == concurrency


@pytest.mark.integration
@pytest.mark.parametrize("status", [401, 403])
def test_actual_http_authentication_category_stops_new_batch_sends(tmp_path, status):
    sent = []

    class Rejects(ScriptedAdapter):
        def send(self, payload, runtime, credential):
            sent.append(payload)
            raise TransportFailure("http_error", exchange={"status": status})

    service = CallService(
        RuntimeConfig(quota_store=tmp_path / "quota.sqlite", max_attempts=1),
        credential=SecretStr("synthetic"),
        adapter=Rejects([]),
    )
    request = CallRequest(
        model=ModelConfig(model="deepseek-v4.1-flash"),
        messages=[Message(role="user", content="Fictional authentication refusal")],
    )
    prepared = service.adapter.prepare(request)
    archived = []
    dispatcher = ConcurrentCalls(1)
    try:
        dispatcher.submit((1,), service, request, prepared, archived.append)
        while dispatcher.jobs:
            dispatcher.collect()
        dispatcher.submit((2,), service, request, prepared, archived.append)
        while dispatcher.jobs:
            dispatcher.collect()
    finally:
        dispatcher.close()
    assert len(sent) == 1 and len(archived) == 2
    assert all(
        row["error"] == "http_error" and not row["uncertain"] for row in archived
    )


@pytest.mark.integration
def test_shared_space_papers_and_objects_overlap_without_roster_barrier(
    prepared_for_parallel, tmp_path
):
    space, first_prep, config = prepared_for_parallel
    pdf = tmp_path / "second.pdf"
    pdf.write_bytes(make_pdf("\n".join([*LINES, "Second independent paper."])))
    source = AcquisitionService(space).import_local_pdf(
        pdf, arxiv_id="2501.01112v1", provenance="Independent fictional paper"
    )
    preparation, result = MaterialsService(
        space, runner=FakeMinerU()
    ).prepare_materials(
        request_key="second-prep", paper=source.canonical_id, version=source.version
    )
    assert result.outcome == "finished"
    first_quantity = threading.Event()
    barriers = [threading.Barrier(2, timeout=15), threading.Barrier(2, timeout=15)]
    events = []
    guard = threading.Lock()

    class Overlap(Adapter):
        def __init__(self, index):
            super().__init__(lambda tool, _: quantities(object_id(tool)))
            self.index = index

        def send(self, payload, runtime, credential):
            roster = payload["tools"][0]["function"]["name"] == "submit_roster"
            if roster and self.index == 1:
                assert first_quantity.wait(15), (
                    "first paper waited for unrelated roster"
                )
            with guard:
                events.append((self.index, roster))
            if not roster:
                first_quantity.set()
                barriers[self.index].wait()
            return super().send(payload, runtime, credential)

    handles = [
        start_pdf_extraction(
            space,
            preparation_execution_id=prep,
            request_key="parallel-" + str(i),
            configuration=config,
        )
        for i, prep in enumerate([first_prep, preparation.execution_id])
    ]
    adapters = [Overlap(0), Overlap(1)]
    tasks = [
        ExtractionTask(
            space,
            handle.execution_id,
            CallService(
                config.runtime, credential=SecretStr("synthetic"), adapter=adapter
            ),
        )
        for handle, adapter in zip(handles, adapters, strict=True)
    ]
    reports = resume_extractions(tasks, concurrency=4)
    assert all(r.outcome == "finished" for r in reports), [r.errors for r in reports]
    assert events.index((0, False)) < events.index((1, True))
    assert [len(a.seen) for a in adapters] == [3, 3]
    for handle in handles:
        saved = space.store().read_record_current(
            "pdf-" + handle.execution_id + "-paper"
        )
        assert len(saved.data.object_contributions) == 2
    backup_workspace(space, tmp_path / "backup")
    restore_workspace(tmp_path / "backup", tmp_path / "restored")
    restored = open_workspace(tmp_path / "restored")
    try:
        resumed = resume_extractions(
            [ExtractionTask(restored, t.execution_id, t.service) for t in tasks],
            concurrency=4,
        )
        assert all(r.outcome == "finished" for r in resumed)
        assert [len(a.seen) for a in adapters] == [3, 3]
    finally:
        restored.close()


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["http_401", "budget_insufficient"])
def test_stop_new_requests_still_saves_returned_independent_object(
    prepared_for_parallel, failure
):
    space, prep, config = prepared_for_parallel
    barrier = threading.Barrier(2, timeout=15)

    class Stops(Adapter):
        def send(self, payload, runtime, credential):
            tool = payload["tools"][0]["function"]
            if tool["name"] == "submit_quantities":
                barrier.wait()
                if tool["parameters"]["title"].endswith("1"):
                    raise TransportFailure(failure, uncertain=False)
            return super().send(payload, runtime, credential)

    adapter = Stops(lambda tool, _: quantities(object_id(tool)))
    handle = start_pdf_extraction(
        space,
        preparation_execution_id=prep,
        request_key="failed-peer",
        configuration=config,
    )
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    reports = resume_extractions(
        [ExtractionTask(space, handle.execution_id, service)], concurrency=2
    )
    assert reports[0].outcome == "failed"
    saved = space.store().read_record_current("pdf-" + handle.execution_id + "-paper")
    assert saved.data.object_contributions[0].quantities == []
    assert saved.data.object_contributions[1].quantities[0].values[0].value == "-430"
