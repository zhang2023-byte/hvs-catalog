"""命令行入口，当前只提供帮助和版本查询。

命令行只列出已经实现的操作；其余命令随对应功能交付时补充。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def get_version() -> str:
    """返回已安装包的软件版本，版本只定义在 pyproject.toml 中。"""
    return version("hvs-catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hvs-catalog",
        description="高速星文献提取与星表系统（当前仅提供帮助与版本查询）。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"hvs-catalog {get_version()}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)  # --help 与 --version 在解析时直接退出
    # 尚未实现其他操作，先展示帮助
    parser.print_help()
    return 0
