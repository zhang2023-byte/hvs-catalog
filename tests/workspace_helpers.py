"""工作空间测试辅助：发布合成夹具材料并绑定来源与外部响应路径。"""

from __future__ import annotations

from records_helpers import FIXTURES

from hvs_catalog.workspace import Workspace


def publish_fixture_collection(space: Workspace, records: list) -> list:
    """把夹具记录集合的材料发布到 sources/ 下并改写路径，返回可提交记录。

    覆盖来源包文件与外部资料原始响应两类材料引用。
    """
    from hvs_catalog.records.objects import ExternalRecord

    out = []
    for record in records:
        if record.RECORD_NAME == "source_package":
            files = []
            for file in record.data.files:
                data = (FIXTURES / file.path).read_bytes()
                ref = space.publish_source_file(file.path, data)
                files.append(file.model_copy(update={"path": ref.path}))
            record = record.model_copy(
                update={"data": record.data.model_copy(update={"files": files})}
            )
        elif isinstance(record, ExternalRecord):
            raw = record.data.raw_response
            ref = space.publish_source_file(
                raw.path, (FIXTURES / raw.path).read_bytes()
            )
            record = record.model_copy(
                update={"data": record.data.model_copy(update={"raw_response": ref})}
            )
        out.append(record)
    return out
