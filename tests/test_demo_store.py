"""业务数据库单元测试：结果复用按输入摘要匹配，决定按问题版本保存。"""

import pytest

from hvs_catalog.demo import store as demo_store


@pytest.fixture()
def business(tmp_path):
    st = demo_store.BusinessStore(tmp_path / "business.db")
    yield st
    st.close()


def test_result_roundtrip_and_input_digest_miss(business):
    business.save_result(
        "extract_roster", "digest-a", '{"input": "a"}', {"objects": []}
    )
    assert business.find_result("extract_roster", "digest-a") == {"objects": []}
    # 输入摘要变化（新的执行）不复用旧行
    assert business.find_result("extract_roster", "digest-b") is None


def test_decision_same_version_same_choice_is_idempotent(business):
    question = {"question_id": "q1", "question_digest": "d1", "prompt": "…"}
    first = business.save_decision("q1", "d1", question, "merge", "理由一")
    again = business.save_decision("q1", "d1", question, "merge", "理由二")
    assert (first, again) == ("saved", "already-saved")
    stored = business.find_decision("q1", "d1")
    assert stored.choice == "merge"
    assert stored.reason == "理由一"  # 首次保存为准，重复提交不覆盖


def test_decision_same_version_conflicting_choice_rejected(business):
    business.save_decision("q1", "d1", {"question_id": "q1"}, "merge", None)
    with pytest.raises(demo_store.DecisionConflictError):
        business.save_decision("q1", "d1", {"question_id": "q1"}, "keep-separate", None)


def test_decision_for_changed_question_is_a_separate_version(business):
    """证据或规则变化后摘要不同，属于另一个版本的问题，新旧决定并存。"""
    business.save_decision("q1", "digest-old", {"question_id": "q1"}, "merge", None)
    # 对新版本问题可以另行决定，旧版本决定保留备查
    saved = business.save_decision(
        "q1", "digest-new", {"question_id": "q1"}, "merge", None
    )
    assert saved == "saved"
    assert business.find_decision("q1", "digest-old").choice == "merge"
    assert business.find_decision("q1", "digest-new").choice == "merge"
    assert {item.question_digest for item in business.list_decisions()} == {
        "digest-old",
        "digest-new",
    }


def test_summary_written_only_once(business):
    assert business.save_summary("demo-001", {"choice": "merge"}) == "saved"
    assert (
        business.save_summary("demo-001", {"choice": "keep-separate"})
        == "already-saved"
    )
    assert business.find_summary("demo-001") == {"choice": "merge"}
    assert business.find_summary("demo-002") is None
