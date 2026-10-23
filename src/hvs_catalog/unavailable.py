"""Explicit guards for scientific workflows awaiting the new contract."""


class WorkflowUnavailable(ValueError):
    pass


def unavailable(*args, **kwargs):
    raise WorkflowUnavailable(
        "数值、Gold 和评分流程尚未适配新合同；当前仅支持名单提取及交接"
    )
