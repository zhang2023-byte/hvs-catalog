"""Exact-version bibliographic facts are frozen before extraction starts."""

from dataclasses import replace

import pytest
from materials_helpers import PAPER
from test_acquisition_lifecycle import FakeArxivService, RaisingService, make_meta

from hvs_catalog.materials import MaterialsService
from hvs_catalog.materials.metadata import PREFIX, archive_metadata, fixed_metadata
from hvs_catalog.pipeline import start_extraction
from hvs_catalog.pipeline.acquisition import AcquisitionService
from hvs_catalog.pipeline.acquisition.lifecycle import AcquisitionError
from hvs_catalog.records.common import RecordRef
from hvs_catalog.workspace import (
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)


def test_archive_exact_version_without_pdf_and_restore(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    api = FakeArxivService(metadata={PAPER: make_meta(PAPER, 2)})
    ref = AcquisitionService(space, api).acquire_metadata(PAPER + "v2")
    assert api.metadata_calls == [[PAPER + "v2"]]
    assert api.download_calls == []
    assert (
        AcquisitionService(space, RaisingService()).acquire_metadata(PAPER + "v2")
        == ref
    )
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        assert (
            AcquisitionService(restored, RaisingService()).acquire_metadata(
                PAPER + "v2"
            )
            == ref
        )
    finally:
        restored.close()
        space.close()


@pytest.mark.parametrize("metadata", [{}, {PAPER: make_meta(PAPER, 2)}])
def test_acquisition_rejects_missing_or_wrong_version(tmp_path, metadata):
    space = initialize_workspace(tmp_path / "space")
    try:
        with pytest.raises(AcquisitionError, match="版本不符"):
            AcquisitionService(
                space, FakeArxivService(metadata=metadata)
            ).acquire_metadata(PAPER + "v1")
        assert space.store().get_meta(PREFIX + PAPER + "v1") is None
    finally:
        space.close()


def test_source_conflict_does_not_replace_fixed_metadata(prepared):
    space, prep, _ = prepared
    source = space.store().read_record(
        RecordRef.model_validate(
            MaterialsService(space).get_prepared_input(prep)["prepared_source_ref"]
        )
    )
    original = fixed_metadata(space, source)
    with pytest.raises(ValueError, match="Conflicting"):
        archive_metadata(
            space,
            replace(make_meta(PAPER, 1), title="Conflicting title"),
            {"source": "synthetic"},
        )
    assert fixed_metadata(space, source) == original


def test_wrong_version_binding_blocks_extraction(prepared):
    space, prep, config = prepared
    other = archive_metadata(space, make_meta(PAPER, 2), {"source": "synthetic"})
    space.store().set_meta(PREFIX + PAPER + "v1", other.model_dump_json())
    with pytest.raises(ValueError, match="fixed paper version"):
        start_extraction(
            space,
            preparation_execution_id=prep,
            request_key="wrong",
            configuration=config,
        )


@pytest.mark.parametrize("version,expected", [(1, 0), (2, 1)])
def test_metadata_cli_closes_workspace_and_never_downloads(
    tmp_path, monkeypatch, capsys, version, expected
):
    from unittest.mock import Mock

    import hvs_catalog.cli_acquisition as cli
    from hvs_catalog.cli import main

    space = initialize_workspace(tmp_path / "space")
    api = FakeArxivService(metadata={PAPER: make_meta(PAPER, version)})
    closed = Mock(wraps=space.close)
    monkeypatch.setattr(space, "close", closed)
    monkeypatch.setattr(cli, "_open_space", lambda path: space)
    monkeypatch.setattr(
        cli, "_build_acquisition_service", lambda s: AcquisitionService(s, api)
    )
    assert (
        main(
            [
                "acquisition",
                "metadata",
                "--space",
                str(space.root),
                "--id",
                PAPER + "v1",
            ]
        )
        == expected
    )
    closed.assert_called_once()
    assert api.metadata_calls == [[PAPER + "v1"]]
    assert api.download_calls == []
    if expected:
        assert "版本不符" in capsys.readouterr().err
    else:
        import json

        from hvs_catalog.records.common import ArtifactRef
        from hvs_catalog.workspace import open_workspace
        from hvs_catalog.workspace.archives import read_json

        ref = ArtifactRef.model_validate(json.loads(capsys.readouterr().out))
        saved = open_workspace(space.root, read_only=True)
        try:
            assert read_json(saved, ref)["metadata"]["version"] == "v1"
        finally:
            saved.close()
