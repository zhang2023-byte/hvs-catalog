"""编号范围确定性展开、技术不支持与语义歧义分类的合成测试。"""

from __future__ import annotations

from hvs_catalog.materials.ranges import MAX_RANGE_MEMBERS, expand_range_notation


def test_clear_object_ranges_expand_deterministically():
    cases = {
        "HVS1-5": ["HVS1", "HVS2", "HVS3", "HVS4", "HVS5"],
        "HVS01-05": ["HVS01", "HVS02", "HVS03", "HVS04", "HVS05"],
        "HVS1,4-6": ["HVS1", "HVS4", "HVS5", "HVS6"],
        "HVS1--HVS5": ["HVS1", "HVS2", "HVS3", "HVS4", "HVS5"],
        "HVS 1 to HVS 5": ["HVS 1", "HVS 2", "HVS 3", "HVS 4", "HVS 5"],
        "CAT-X001-003": ["CAT-X001", "CAT-X002", "CAT-X003"],
    }
    for notation, members in cases.items():
        result = expand_range_notation(notation, object_context=True)
        assert result.expanded, f"{notation}: {result.reason}"
        assert result.identifiers == members
        assert result.notation == notation
        assert result.rule


def test_leading_zeros_and_bound_members_preserved():
    result = expand_range_notation("HVS007-010", object_context=True)
    assert result.identifiers == ["HVS007", "HVS008", "HVS009", "HVS010"]
    assert (
        expand_range_notation(f"HVS1-{MAX_RANGE_MEMBERS + 5}").status == "unsupported"
    )
    # remainder 只识别为尾注，绝不解释为成员
    result = expand_range_notation("HVS1-3 and others", object_context=True)
    assert result.expanded
    assert result.identifiers == ["HVS1", "HVS2", "HVS3"]
    assert result.remainder == "and others"


def test_technically_unsupported_notations():
    for notation, fragment in [
        ("HVS5-1", "升序"),
        ("HVS01-HVS5", "前导零"),
        ("HVS1-5000", "上限"),
        ("HVS1,,3", "空段"),
        ("400-500 km/s", "不支持"),
        ("HVS 1 to CAT 5", "前缀不一致"),
        ("", "非空"),
    ]:
        result = expand_range_notation(notation)
        assert result.status == "unsupported", notation
        assert fragment in result.reason
        assert result.identifiers == []


def test_bare_numeric_ranges_are_ambiguous_not_object_lists():
    for notation in ("1-5", "400-500", "1,2,3"):
        result = expand_range_notation(notation)
        assert result.status == "ambiguous", notation
        assert "对象编号" in result.reason or "物理量" in result.reason
        assert result.identifiers == []


def test_expansion_is_pure_function_of_notation():
    first = expand_range_notation("HVS1-3")
    second = expand_range_notation("HVS1-3")
    assert first == second
