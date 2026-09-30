"""Gold CLI thin wrappers: migration, checks, and one current-pair writer."""

import json

import pytest
from evaluation_helpers import build_legacy_v4_doc
from gold_save_helpers import flow, revise, write_approval

from hvs_catalog.cli import main


def test_gold_cli_migrate_check_compare(tmp_path, capsys):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(build_legacy_v4_doc()))
    draft = tmp_path / "draft"
    assert (
        main(
            [
                "gold",
                "migrate",
                "--legacy",
                str(legacy),
                "--draft-dir",
                str(draft),
                "--material-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "[迁移]" in out and "[待办]" in out
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "gold",
                "check",
                "--draft",
                str(draft / "draft.json"),
                "--material-root",
                str(tmp_path),
                "--report-json",
                str(report_path),
            ]
        )
        == 2
    )
    assert json.loads(report_path.read_text())["problems"]
    assert "[拒绝]" not in capsys.readouterr().out
    assert (
        main(
            [
                "gold",
                "compare",
                "--legacy",
                str(legacy),
                "--current",
                str(draft / "draft.json"),
            ]
        )
        == 0
    )
    assert "一致" in capsys.readouterr().out


def test_cli_inspect_save_read_new_and_revision(tmp_path, capsys):
    args = flow(tmp_path)
    common = [
        "--pair",
        str(args["pair_path"] / "answer.json"),
        "--material-root",
        str(args["material_root"]),
        "--target",
        str(args["target"].root),
    ]
    assert main(["gold", "inspect-save", *common]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["binding_sha256"] == args["approval"].binding_sha256
    assert not args["target"].root.exists()
    permission = write_approval(args, tmp_path / "permission.json")
    command = ["gold", "save", *common, "--approval", str(permission)]
    assert main(command) == 0
    assert not json.loads(capsys.readouterr().out)["already_present"]
    assert main(command) == 0
    assert json.loads(capsys.readouterr().out)["already_present"]
    assert main(["gold", "read", "--target", str(args["target"].root)]) == 0
    old = json.loads(capsys.readouterr().out)["pair_sha256"]
    changed = revise(args)
    assert main(["gold", "inspect-save", *common, "--expected-old", old]) == 0
    summary = json.loads(capsys.readouterr().out)
    payload = args["approval"].model_dump(mode="json")
    payload.update(summary)
    permission.write_text(json.dumps(payload))
    assert main(command) == 0
    capsys.readouterr()
    assert changed["target"].read_current()[0].metadata.revision is not None
    # Former separate protocols aren't exposed as misleading aliases.
    for obsolete in ("prepare", "query", "git-refresh"):
        with pytest.raises(SystemExit):
            main(["gold", obsolete])


def test_cli_rejects_changed_content_under_old_approval(tmp_path, capsys):
    args = flow(tmp_path)
    permission = write_approval(args, tmp_path / "permission.json")
    revise_without_current = json.loads(
        (args["pair_path"] / "metadata.json").read_text()
    )
    revise_without_current["paper"]["title"] += " changed"
    (args["pair_path"] / "metadata.json").write_text(json.dumps(revise_without_current))
    assert (
        main(
            [
                "gold",
                "save",
                "--pair",
                str(args["pair_path"]),
                "--material-root",
                str(args["material_root"]),
                "--target",
                str(args["target"].root),
                "--approval",
                str(permission),
            ]
        )
        == 2
    )
    assert "binding" in capsys.readouterr().err
    assert not args["target"].root.exists()


def test_gold_cli_migrate_rejects_unknown_version(tmp_path, capsys):
    raw = build_legacy_v4_doc()
    raw["schema"]["version"] = 3
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(raw))
    assert (
        main(
            [
                "gold",
                "migrate",
                "--legacy",
                str(legacy),
                "--draft-dir",
                str(tmp_path / "draft"),
            ]
        )
        == 2
    )
    assert capsys.readouterr().err
