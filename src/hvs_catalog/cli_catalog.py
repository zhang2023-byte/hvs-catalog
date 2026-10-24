"""Catalog selection, offline construction, and explicitly requested SIMBAD acquisition."""

import json
import sys
from pathlib import Path

from .catalogue import build_catalog, load_selection, verify_catalog
from .catalogue.build import prepare_groups
from .catalogue.delivery import inspect_results, selection_from_results
from .catalogue.inputs import json_bytes, selection_from_index
from .catalogue.simbad import AstroquerySimbad, collect_queries, queries_for, query_key
from .records.catalog import SimbadQuery
from .records.common import sha256_of
from .workspace import WorkspaceError


def add_catalog_commands(commands):
    catalog = commands.add_parser("catalog", help="固定JSON星表、身份关联与SIMBAD快照")
    sub = catalog.add_subparsers(dest="catalog_command", required=True)
    select = sub.add_parser("select", help="从最终results或历史索引固定本次构建输入")
    source = select.add_mutually_exclusive_group(required=True)
    source.add_argument("--results", type=Path, help="含runs目录的最终文献交付根")
    source.add_argument("--index", type=Path, help="旧执行索引（仅历史兼容）")
    select.add_argument("--space", type=Path, help="旧执行索引对应的业务空间")
    select.add_argument("--data-version", required=True)
    select.add_argument("--output", type=Path, required=True)
    inspect = sub.add_parser(
        "check-inputs", help="只读核验最终results与来源，不构造星表"
    )
    inspect.add_argument("--results", type=Path, required=True)
    build = sub.add_parser("build", help="离线构建新快照，不访问SIMBAD")
    build.add_argument("--selection", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--simbad", type=Path, help="已冻结的SIMBAD响应目录")
    plan = sub.add_parser("query-plan", help="离线列出可审阅的SIMBAD查询范围")
    plan.add_argument("--selection", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    query = sub.add_parser("query", help="对明确查询清单调用真实SIMBAD，保存响应")
    query.add_argument("--plan", type=Path, required=True)
    query.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify", help="离线核验星表、哈希、来源与科学内容")
    verify.add_argument("path", type=Path)


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(json_bytes(value))


def run_catalog_command(args):
    try:
        name = args.catalog_command
        if name == "select":
            if args.results:
                if args.space:
                    raise ValueError("--space is only valid with --index.")
                selection = selection_from_results(args.results, args.data_version)
            else:
                if not args.space:
                    raise ValueError("--index requires --space.")
                selection = selection_from_index(
                    args.index, args.space, args.data_version
                )
            _write_new(args.output, selection)
            result = {"papers": len(selection.papers), "selection": str(args.output)}
        elif name == "check-inputs":
            result = {"valid": True, **inspect_results(args.results)}
        elif name == "build":
            manifest = build_catalog(
                load_selection(args.selection),
                args.output,
                simbad=args.simbad,
            )
            result = {
                "papers": len(manifest.papers),
                "stars": len(manifest.stars),
                "output": str(args.output),
            }
        elif name == "verify":
            manifest = verify_catalog(args.path)
            result = {
                "valid": True,
                "papers": len(manifest.papers),
                "stars": len(manifest.stars),
            }
        elif name == "query-plan":
            selection = load_selection(args.selection)
            papers, groups = prepare_groups(selection)
            queries = {}
            for members in groups.groups():
                for query in queries_for(groups.scientific(members)):
                    queries[query_key(query)] = query.model_dump(mode="json")
            result = {
                "schema_version": 1,
                "input_fingerprint": sha256_of(
                    {k: p.source.model_dump(mode="json") for k, p in papers.items()}
                ),
                "queries": [queries[k] for k in sorted(queries)],
            }
            _write_new(args.output, result)
        else:
            plan = json.loads(args.plan.read_bytes())
            if (
                set(plan) != {"schema_version", "input_fingerprint", "queries"}
                or plan["schema_version"] != 1
            ):
                raise ValueError("Unsupported SIMBAD query plan.")
            queries = [SimbadQuery.model_validate(q) for q in plan["queries"]]
            result = collect_queries(queries, args.output, AstroquerySimbad())
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if "failed" in result.values() else 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, WorkspaceError) as exc:
        print("[拒绝] " + str(exc), file=sys.stderr)
        return 2
