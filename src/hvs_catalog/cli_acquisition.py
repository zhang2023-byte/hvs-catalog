"""hvs-catalog acquisition commands manage the arXiv source-package lifecycle.

Status/material queries neither access the network nor initialize implicitly; the
workspace must exist. Initialize first through the public Python
initialize_workspace(PATH) interface. Explicit discovery/fetch/revision checks make
network requests. Lane A global local-machine state shares throttling/in-flight
coordination across workspaces; CLI exposes no bypass/reset.

Exit codes: 0 success; 1 engineering error; 2 argparse error; 3 incomplete resumable
discovery; 4 unsupported format with bytes retained; 5 source unavailable/pending; 6 no
usable/ready material; 7 incomplete revision verification (unknown, never
no-new-version).

Unavailable material is not scientifically irrelevant. Remote retrieval retains received
bytes in the workspace, attached to version facts/acquisition records and included in
backup/restore. Explicitly rejected local imports, such as unsupported archives, publish
no files or selection state: original files stay unchanged outside workspace backup.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from hvs_catalog.pipeline.acquisition.lifecycle import (
    AcquisitionError,
    AcquisitionService,
    PackageRejectionError,
    SourceUnavailableError,
)
from hvs_catalog.pipeline.acquisition.protocol import (
    ArxivIdentifierError,
    ArxivProtocolError,
    ArxivQueryError,
    parse_arxiv_identifier,
)
from hvs_catalog.pipeline.acquisition.transport import (
    ArxivRateLimitedError,
    ArxivTransportError,
)
from hvs_catalog.workspace import WorkspaceError, open_workspace

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INCOMPLETE = 3
EXIT_UNSUPPORTED = 4
EXIT_UNAVAILABLE = 5
EXIT_NO_MATERIAL = 6
EXIT_REVISION_UNKNOWN = 7


def build_acquisition_parser(commands: argparse._SubParsersAction) -> None:
    """Register acquisition subcommands."""
    group = commands.add_parser(
        "acquisition",
        help="arXiv 获取：发现/获取/修订检查/本地导入与状态查询",
        description=(
            "arXiv PDF获取生命周期。发现轮次显式受限并持久化"
            "溯源；获取先冻结版本再下载；状态查询不触网。限流状态为本机"
            "全局（跨工作空间共享），出现未确认在途状态时需人工协调恢"
            "复，CLI 不提供重置入口。首次使用前需用 Python 公共接口初始"
            '化工作空间：python -c "from hvs_catalog.workspace import '
            "initialize_workspace; initialize_workspace('空间目录')\"（状态"
            "命令不会隐式初始化）。"
        ),
    )
    sub = group.add_subparsers(dest="acquisition_command", required=True)

    discover = sub.add_parser(
        "discover",
        help="执行（或恢复）一个显式受限的发现轮次",
        description=(
            "按首提日期窗口与检索词/类别分页检索并持久化每页溯源。同一"
            "请求要用新溯源再跑一次，用更大的 --round 开新轮次。"
            "退出码 3 表示轮次未完（页数界限/异常），可恢复。"
        ),
    )
    discover.add_argument("--space", required=True, type=Path, help="工作空间根目录")
    discover.add_argument(
        "--from",
        dest="submitted_from",
        required=True,
        type=date.fromisoformat,
        help="首提日期窗口起点（YYYY-MM-DD）",
    )
    discover.add_argument(
        "--to",
        dest="submitted_to",
        required=True,
        type=date.fromisoformat,
        help="首提日期窗口终点（YYYY-MM-DD）",
    )
    discover.add_argument("--terms", default=None, help="检索词（与类别至少其一）")
    discover.add_argument(
        "--category",
        action="append",
        default=None,
        help="类别过滤（可重复，如 astro-ph.CO；与检索词至少其一）",
    )
    discover.add_argument(
        "--max-pages", type=int, default=None, help="本轮最多推进的页数界限"
    )
    discover.add_argument(
        "--round",
        type=int,
        default=1,
        help="轮次序号（同一请求的新溯源轮次用更大的序号，默认 1）",
    )
    discover.set_defaults(handler=_run_discover)

    fetch = sub.add_parser(
        "fetch",
        help="按标识获取固定版本的PDF（或恢复既有目标）",
        description=(
            "未带版本时先查元数据选定当时最新版本并在下载前持久化冻结；"
            "恢复绝不悄悄改选更新版本。已完成目标只做哈希核对。"
        ),
    )
    fetch.add_argument("--space", required=True, type=Path, help="工作空间根目录")
    fetch.add_argument(
        "--id",
        required=True,
        help="arXiv 标识（2401.01234 或 2401.01234v2，旧式亦支持）",
    )
    fetch.add_argument(
        "--latex", action="store_true", help="额外下载LaTeX仅作存档；失败不影响PDF"
    )
    fetch.set_defaults(handler=_run_fetch)

    revisions = sub.add_parser(
        "revisions",
        help="检查已收录论文是否有新版（独立于发现窗口）",
        description=(
            "只查询元数据并持久化检查结果；发现新版记录待处理，不改"
            "动既有冻结选择与材料。未能核实（未返回/未找到/批次异常）的"
            "论文标 unknown，退出码 7——不能当成无新版。"
        ),
    )
    revisions.add_argument("--space", required=True, type=Path, help="工作空间根目录")
    revisions.add_argument(
        "--id",
        action="append",
        default=None,
        help="要检查的规范标识（可重复；缺省为全部已收录）",
    )
    revisions.set_defaults(handler=_run_revisions)

    local_import = sub.add_parser(
        "import",
        help="导入本地已下载的同类PDF（身份与版本必须显式声明）",
        description=(
            "不从归档内容猜测论文身份；同论文同版本不同字节拒绝覆盖，"
            "相同字节重复导入复用材料并保留全部来源声明。"
        ),
    )
    local_import.add_argument(
        "--space", required=True, type=Path, help="工作空间根目录"
    )
    local_import.add_argument("--file", required=True, type=Path, help="本地PDF路径")
    local_import.add_argument(
        "--id", required=True, help="arXiv 标识（不带版本时必须给 --version）"
    )
    local_import.add_argument("--version", type=int, default=None, help="声明的版本号")
    local_import.add_argument(
        "--provenance",
        required=True,
        help="来源声明（必填且非空白：从何处获得该文件，可审计的依据）",
    )
    local_import.set_defaults(handler=_run_import)

    journal = sub.add_parser("import-pdf", help="导入固定期刊PDF，不要求arXiv标识")
    journal.add_argument("--space", required=True, type=Path)
    journal.add_argument("--file", required=True, type=Path)
    journal.add_argument("--paper-key", required=True)
    journal.add_argument("--source-version", required=True)
    journal.add_argument("--provenance", required=True)
    journal.add_argument("--doi")
    journal.set_defaults(handler=_run_journal_import)

    status = sub.add_parser(
        "status",
        help="查看获取进度与状态（不触网、不隐式初始化）",
        description=(
            "列出发现轮次（含未完原因）、版本冻结选择（含原始包引用与"
            "错误）与修订检查结果（selected/current/pending 区分）。"
        ),
    )
    status.add_argument("--space", required=True, type=Path, help="工作空间根目录")
    status.add_argument("--round", default=None, help="只看指定轮次 id")
    status.add_argument(
        "--id", default=None, help="只看指定论文；带版本号可回查旧版材料与来源"
    )
    status.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    status.set_defaults(handler=_run_status)

    material = sub.add_parser(
        "material",
        help="输出选定版本的材料引用，供后续解析使用（不触网）",
        description=("打印选定版本的PDF记录引用与原始包字节位置；未就绪时退出码 6。"),
    )
    material.add_argument("--space", required=True, type=Path, help="工作空间根目录")
    material.add_argument("--id", required=True, help="论文规范标识（不带版本）")
    material.set_defaults(handler=_run_material)


def _build_acquisition_service(space) -> AcquisitionService:
    """Build the service; tests replace this factory with injected fakes, without a product
    offline backdoor.
    """
    return AcquisitionService(space)


def _open_space(root: Path):
    return open_workspace(root)


def _print_discovery_status(status) -> None:
    reason = f"（原因：{status.reason}）" if status.reason else ""
    print(
        f"[轮次] {status.round_id} state={status.state}{reason} "
        f"pages={len(status.pages)} papers={len(status.papers)}"
    )
    for canonical_id, info in sorted(status.papers.items()):
        pages = ",".join(str(start) for start in info.get("pages", []))
        print(
            f"  [论文] {canonical_id} v{info['version']} "
            f"updated={info['updated']} pages=[{pages}]"
        )


def _print_selection(status) -> None:
    record = "-"
    if status.source_record is not None:
        record = f"{status.source_record.record_id}@r{status.source_record.revision}"
    print(
        f"[选择] {status.canonical_id} {status.versioned_id} state={status.state} "
        f"via={status.via} selected_at={status.selected_at} record={record}"
    )
    if status.raw_path:
        print(f"  [原始包] {status.raw_path} sha256={status.raw_sha256}")
    if status.retrieval:
        print(f"  [溯源] {status.retrieval}")
    for item in status.provenance_log:
        print(f"  [来源声明] {item.get('provenance')} at={item.get('at')}")
    if status.error:
        print(f"  [错误] {status.error}")


def _print_revision(status) -> None:
    latest = (
        f"v{status.latest_version}" if status.latest_version is not None else "unknown"
    )
    selected = (
        f"v{status.selected_version}" if status.selected_version is not None else "none"
    )
    print(
        f"[修订] {status.canonical_id} selected={selected} "
        f"latest={latest} state={status.state} "
        f"checked_at={status.checked_at}"
    )
    if status.latest_updated:
        print(f"  [新版时间] {status.latest_updated}")
    if status.error:
        print(f"  [说明] {status.error}")


def _selection_as_dict(status) -> dict:
    return {
        "canonical_id": status.canonical_id,
        "version": status.version,
        "versioned_id": status.versioned_id,
        "state": status.state,
        "via": status.via,
        "selected_at": status.selected_at,
        "source_record": (
            None
            if status.source_record is None
            else {
                "record_id": status.source_record.record_id,
                "revision": status.source_record.revision,
            }
        ),
        "raw_path": status.raw_path,
        "raw_sha256": status.raw_sha256,
        "retrieval": status.retrieval,
        "provenance_log": list(status.provenance_log),
        "error": status.error,
    }


def _run_discover(args: argparse.Namespace) -> int:
    from hvs_catalog.pipeline.acquisition.protocol import ArxivSearchRequest

    if not args.terms and not args.category:
        print("[错误] --terms 与 --category 至少提供其一", file=sys.stderr)
        return EXIT_ERROR
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        request = ArxivSearchRequest(
            submitted_from=args.submitted_from,
            submitted_to=args.submitted_to,
            terms=args.terms,
            categories=tuple(args.category or ()),
        )
        status = service.run_discovery(
            request, max_pages=args.max_pages, round_seq=args.round
        )
    except ArxivQueryError as exc:
        print(f"[错误] 检索条件非法：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except WorkspaceError as exc:
        print(f"[错误] 工作空间错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivRateLimitedError as exc:
        print(
            f"[限流] arXiv 服务限流（HTTP 429）：{exc}；进度已持久化，"
            "请遵守冷却后重试（限流状态不做重置）",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except ArxivTransportError as exc:
        print(f"[传输错误] arXiv 网络请求失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivProtocolError as exc:
        print(f"[错误] arXiv 协议错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except AcquisitionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    _print_discovery_status(status)
    if status.state != "complete":
        return EXIT_INCOMPLETE
    return EXIT_OK


def _run_fetch(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        result = service.acquire(args.id, include_latex=args.latex)
    except ArxivIdentifierError as exc:
        print(f"[错误] 标识畸形：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except SourceUnavailableError as exc:
        print(f"[待处理] {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except PackageRejectionError as exc:
        print(f"[拒绝] {exc}", file=sys.stderr)
        return EXIT_ERROR
    except WorkspaceError as exc:
        print(f"[错误] 工作空间错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivRateLimitedError as exc:
        print(
            f"[限流] arXiv 服务限流（HTTP 429）：{exc}；进度已持久化，"
            "请遵守冷却后重试（限流状态不做重置）",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except ArxivTransportError as exc:
        print(f"[传输错误] arXiv 网络请求失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivProtocolError as exc:
        print(f"[错误] arXiv 协议错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except AcquisitionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    record = "-"
    if result.material_ref is not None:
        record = f"{result.material_ref.record_id}@r{result.material_ref.revision}"
    replayed = "（重放/恢复）" if result.replayed else ""
    print(
        f"[获取] {result.versioned_id} state={result.state}{replayed} record={record}"
    )
    print(f"  [原始包] {result.raw_path} sha256={result.raw_sha256}")
    if result.state == "unsupported":
        print("  [说明] 形态不支持：原始字节已保留，等待人工处理")
        return EXIT_UNSUPPORTED
    return EXIT_OK


def _run_revisions(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        result = service.check_revisions(list(args.id) if args.id else None)
    except WorkspaceError as exc:
        print(f"[错误] 工作空间错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivRateLimitedError as exc:
        print(
            f"[限流] arXiv 服务限流（HTTP 429）：{exc}；进度已持久化，"
            "请遵守冷却后重试（限流状态不做重置）",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except ArxivTransportError as exc:
        print(f"[传输错误] arXiv 网络请求失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ArxivProtocolError as exc:
        print(f"[错误] arXiv 协议错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except AcquisitionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    has_unknown = False
    for entry in result.entries:
        _print_revision(entry)
        if entry.state == "unknown":
            has_unknown = True
    if has_unknown:
        print(
            "[未核实] 部分论文的修订状态未能核实（unknown），不能当成无新版",
            file=sys.stderr,
        )
        return EXIT_REVISION_UNKNOWN
    return EXIT_OK


def _run_import(args: argparse.Namespace) -> int:
    provenance = str(args.provenance or "")
    if not provenance.strip():
        print(
            "[错误] --provenance 必须是非空白来源声明（本地导入不是服务"
            "核实，需要可审计依据）",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        result = service.import_local_pdf(
            args.file,
            arxiv_id=args.id,
            version=args.version,
            provenance=provenance,
        )
    except ArxivIdentifierError as exc:
        print(f"[错误] 标识畸形：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except PackageRejectionError as exc:
        print(f"[拒绝] 导入文件不是可用的PDF形态：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except WorkspaceError as exc:
        print(f"[错误] 工作空间错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except AcquisitionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    replayed = "（重放：材料复用，来源声明已保留）" if result.replayed else ""
    print(
        f"[导入] {result.versioned_id} state=imported{replayed} "
        f"record={result.material_ref.record_id}@r{result.material_ref.revision}"
    )
    print(f"  [原始包] {result.raw_path} sha256={result.raw_sha256}")
    return EXIT_OK


def _run_status(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        rounds = (
            [service.get_discovery_round(args.round)]
            if args.round
            else service.list_discovery_rounds()
        )
        rounds = [item for item in rounds if item is not None]
        if args.id:
            ident = parse_arxiv_identifier(args.id)
            selections = [service.get_selection(ident.canonical_id)]
            selections = [item for item in selections if item is not None]
            versions = (
                [service.get_version(ident.versioned_id)]
                if ident.version is not None
                else service.list_versions(ident.canonical_id)
            )
            versions = [item for item in versions if item is not None]
            revisions = [service.get_revision_status(ident.canonical_id)]
            revisions = [item for item in revisions if item is not None]
        else:
            selections = service.list_selections()
            versions = service.list_versions()
            revisions = [
                service.get_revision_status(item.canonical_id) for item in selections
            ]
            revisions = [item for item in revisions if item is not None]
    except (WorkspaceError, AcquisitionError, ArxivIdentifierError) as exc:
        print(f"[错误] 获取状态不可读：{exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    if getattr(args, "json", False):
        payload = {
            "rounds": [
                {
                    "round_id": item.round_id,
                    "state": item.state,
                    "reason": item.reason,
                    "pages": [dict(page) for page in item.pages],
                    "papers": {key: dict(value) for key, value in item.papers.items()},
                }
                for item in rounds
            ],
            "selections": [_selection_as_dict(item) for item in selections],
            "versions": [_selection_as_dict(item) for item in versions],
            "revisions": [
                {
                    "canonical_id": item.canonical_id,
                    "selected_version": item.selected_version,
                    "latest_version": item.latest_version,
                    "latest_updated": item.latest_updated,
                    "checked_at": item.checked_at,
                    "state": item.state,
                    "error": item.error,
                }
                for item in revisions
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not rounds and not selections and not revisions:
        print("[状态] 尚无任何获取进度")
        return EXIT_OK
    for item in rounds:
        _print_discovery_status(item)
    for item in selections:
        _print_selection(item)
    for item in versions:
        print("[已登记版本]")
        _print_selection(item)
    for item in revisions:
        _print_revision(item)
    return EXIT_OK


def _run_material(args: argparse.Namespace) -> int:
    try:
        space = _open_space(args.space)
    except WorkspaceError as exc:
        print(f"[错误] 打开工作空间失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        service = _build_acquisition_service(space)
        material = service.selected_material(args.id)
    except WorkspaceError as exc:
        print(f"[错误] 工作空间错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except AcquisitionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        space.close()
    if material is None:
        print(
            f"[未就绪] {args.id} 没有已完成的选定版本材料；"
            "请先执行 fetch/import 或查看 acquisition status",
            file=sys.stderr,
        )
        return EXIT_NO_MATERIAL
    print(
        f"[材料] {material.versioned_id} "
        f"record={material.material_ref.record_id}@r{material.material_ref.revision}"
    )
    print(f"  [原始包] {material.raw_path} sha256={material.raw_sha256}")
    print(f"  [溯源] {material.origin_retrieval}")
    return EXIT_OK


def _run_journal_import(args):
    try:
        space = _open_space(args.space)
        ref = AcquisitionService(space).import_journal_pdf(
            args.file,
            paper_key=args.paper_key,
            source_version=args.source_version,
            provenance=args.provenance,
            doi=args.doi,
        )
        print(f"[来源] {ref.record_id}@{ref.revision}")
        return EXIT_OK
    except (OSError, ValueError, AcquisitionError, WorkspaceError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if "space" in locals():
            space.close()
