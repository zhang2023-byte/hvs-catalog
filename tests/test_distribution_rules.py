"""Distribution-content rule unit tests using synthetic archive member names for
tools/check_distribution.
"""

import pytest
from check_distribution import (
    RETIRED_MODULE_PATHS,
    check_sdist,
    check_wheel,
    find_forbidden_paths,
    top_levels,
)


def test_wheel_allows_only_package_and_dist_info():
    names = [
        "hvs_catalog/__init__.py",
        "hvs_catalog/cli.py",
        "hvs_catalog-0.1.0.dist-info/METADATA",
        "hvs_catalog-0.1.0.dist-info/RECORD",
    ]
    assert check_wheel(names) == []


def test_wheel_rejects_development_material():
    names = [
        "hvs_catalog/cli.py",
        "tests/test_cli.py",
        ".dev/plan.md",
        "docs/design.md",
    ]
    assert sorted(check_wheel(names)) == [
        ".dev/plan.md",
        "docs/design.md",
        "tests/test_cli.py",
    ]


def test_wheel_rejects_unexpected_top_level():
    assert check_wheel(["unexpected_root/file.txt"]) == [
        "wheel 顶层出现意外内容：unexpected_root"
    ]


def test_sdist_allows_build_inputs_only():
    names = [
        "hvs_catalog-0.1.0/PKG-INFO",
        "hvs_catalog-0.1.0/pyproject.toml",
        "hvs_catalog-0.1.0/README.md",
        "hvs_catalog-0.1.0/.gitignore",
        "hvs_catalog-0.1.0/src/hvs_catalog/cli.py",
    ]
    assert check_sdist(names, "0.1.0") == []


def test_sdist_rejects_other_top_level_and_members():
    names = [
        "hvs_catalog-0.1.0/tests/test_cli.py",
        "extra-root/README.md",
    ]
    violations = check_sdist(names, "0.1.0")
    assert "hvs_catalog-0.1.0/tests/test_cli.py" in violations
    assert "源码包顶层出现意外内容：extra-root/README.md" in violations


def test_find_forbidden_paths_checks_whole_segments():
    # A package module named catalog.py is allowed; exclude only the official catalog data directory.
    assert find_forbidden_paths(["hvs_catalog/catalog.py", "catalog/data.ecsv"]) == [
        "catalog/data.ecsv"
    ]


@pytest.mark.parametrize("module_path", sorted(RETIRED_MODULE_PATHS))
def test_retired_modules_rejected_in_wheel_and_sdist(module_path):
    assert check_wheel([module_path]) == [module_path]
    sdist_path = f"hvs_catalog-0.1.0/src/{module_path}"
    assert check_sdist([sdist_path], "0.1.0") == [sdist_path]


def test_top_levels_returns_first_segments():
    assert top_levels(["a/b/c", "a/d", "e"]) == {"a", "e"}
