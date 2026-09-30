"""工作空间对外错误类型。

按裁决接口设计至少区分：空间忙、未知空间/格式、记录或材料损坏、
预期修订过期、决定冲突、裁决配置不允许和恢复不一致；另为同操作键
异内容提供专门类型。错误信息应携带执行、步骤或记录引用，由各调用
位置补充。
"""

from __future__ import annotations


class WorkspaceError(Exception):
    """工作空间相关错误的基础类型。"""


class WorkspaceBusyError(WorkspaceError):
    """另一执行者持有空间锁；本次操作未执行。"""


class UnknownWorkspaceError(WorkspaceError):
    """目录不是已初始化的工作空间，或格式标记未知。"""


class WorkspaceConflictError(WorkspaceError):
    """与空间内既有内容冲突，例如预留文件名、目标目录已存在或覆盖不同内容。"""


class WorkspaceIntegrityError(WorkspaceError):
    """记录、材料或空间结构损坏，不能继续按已保存内容推进。"""


class RevisionStaleError(WorkspaceError):
    """预期当前修订与实际不符，本次修改未提交。"""


class DecisionConflictError(WorkspaceError):
    """同一问题版本已保存不同决定，本次提交被拒绝。"""


class AdjudicationNotAllowedError(WorkspaceError):
    """当前执行的裁决配置不允许该专家决定。"""


class RecoveryInconsistentError(WorkspaceError):
    """固定输入、检查点与业务记录不一致，恢复受阻。"""


class OperationKeyConflictError(WorkspaceError):
    """同一操作键已用于不同内容；重试必须携带相同内容或更换操作键。"""
