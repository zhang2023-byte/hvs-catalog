"""Gold 工具的错误类型：读取、检查、保存与恢复各自可辨别的失败。"""

from __future__ import annotations


class GoldError(ValueError):
    """Gold 工具操作失败的基础类型。"""


class GoldFormatError(GoldError):
    """Gold 文档结构不满足格式定义，例如未知版本、未知字段或字段形状不符。"""


class LegacyFormatError(GoldFormatError):
    """旧 contribution Gold 文档不属于受支持的读取范围。"""


class GoldCheckError(GoldError):
    """检查阶段发现阻断保存的问题（结构、材料或定位）。"""


class GoldApprovalError(GoldError):
    """批准记录与准备内容、目标前提或规则版本绑定不符。"""


class GoldTargetError(GoldError):
    """正式目标目录不满足保存前提，例如已有非托管资料或目标状态变化。"""


class GoldOperationConflictError(GoldError):
    """同一操作标识携带不同内容，或操作记录无法与现场对应。"""


class GoldOutputConflictError(GoldError):
    """迁移输出与只读输入或已有结果重合；拒绝写入以保护原始资料。"""


class GoldIntegrityError(GoldError):
    """保存现场出现不能解释的不一致；保留现场并停止，不覆盖宣称恢复。"""
