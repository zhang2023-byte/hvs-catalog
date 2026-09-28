"""评分组件的错误类型。

``ScoringInputError`` 表示调用方输入违反公开契约，整批拒绝、不默默覆盖；
辅助启用与冻结存储错误分别单独区分，便于测试与报告定位。
"""

from __future__ import annotations


class ScoringError(Exception):
    """评分组件错误基类。"""


class ScoringInputError(ScoringError):
    """评分计划、Gold 或预测输入违反公开契约，整批拒绝。"""


class AuxiliaryEnablementError(ScoringError):
    """辅助评分 Agent 未满足启用条件，默认拒绝。"""


class FrozenStoreError(ScoringError):
    """冻结判定存储的完整性或内容冲突错误。"""
