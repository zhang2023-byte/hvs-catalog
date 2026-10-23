"""CLI for single-PDF, single-annotation Gold."""

import json
import sys
from pathlib import Path


def add_gold_commands(commands):
    check = commands.add_parser(
        "check", help="Check annotation, science rules, PDF hash and evidence pages"
    )
    check.add_argument("--annotation", required=True, type=Path)
    compare = commands.add_parser(
        "compare", help="Compare two annotations, including evidence and notes"
    )
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    for name in ("inspect-save", "save"):
        command = commands.add_parser(
            name, help="Inspect or publish explicitly selected annotation content"
        )
        command.add_argument("--annotation", required=True, type=Path)
        command.add_argument("--target", required=True, type=Path)
        command.add_argument("--expected-old")
        if name == "save":
            command.add_argument("--expected-new", required=True)
    read = commands.add_parser(
        "read", help="Check saved layout and return the annotation fingerprint"
    )
    read.add_argument("--target", required=True, type=Path)


def run_gold_command(args):
    from .evaluation.gold import annotation_path, check_annotation, read_annotation
    from .evaluation.gold_compare import compare_annotations
    from .evaluation.store import data_lock, inspect_save, read_saved, save_annotation
    from .workspace import WorkspaceError

    try:
        command = args.gold_command
        if command == "check":
            source = annotation_path(args.annotation)
            report = check_annotation(
                read_annotation(source), material_root=source.parent
            )
            result = {**report.model_dump(), "ready_for_review": report.ready}
            print(json.dumps(result, indent=2))
            return 0 if report.ready else 2
        if command == "compare":
            result = compare_annotations(args.before, args.after)
        elif command == "read":
            result = read_saved(args.target)
        else:
            kwargs = {
                "annotation": args.annotation,
                "target": args.target,
                "expected_old": args.expected_old,
            }
            if command == "save":
                result = save_annotation(**kwargs, expected_new=args.expected_new)
            else:
                with data_lock(args.target.parent):
                    result = inspect_save(**kwargs)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, TypeError, OSError, WorkspaceError, KeyError) as exc:
        print(f"[rejected] {exc}", file=sys.stderr)
        return 2
