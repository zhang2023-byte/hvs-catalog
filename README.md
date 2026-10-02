# hvs-catalog

从高速星研究文献中整理对象、作者判断和测量值，保留原文依据，最终形成可回查来源与修订的星表。研究范围和处理限制见 [科学边界](docs/design.md#产品目标与科学边界)。

## 当前可用能力

| 能力 | 当前范围与入口 |
|---|---|
| Python 包与 CLI | 可独立安装；支持帮助、版本查询和 `python -m hvs_catalog`。 |
| 共同记录格式 | 十类记录的 JSON 读写、准确修订引用、原文证据校验与三种步骤的简化填写展开；见 [记录设计](docs/design/records.md) 和 [生成合同](contracts/generated/README.md)。 |
| 工作空间、运行与裁决 | Python 接口支持固定输入、事务保存、暂停恢复和整体备份恢复；见 [接口与支持边界](docs/design/architecture.md#工作空间与恢复接口方案)。 |
| arXiv 获取 | CLI／Python 支持按条件发现、固定版本获取、本地导入、修订检查和状态查询；见 [操作说明](docs/operations.md#arxiv-材料获取)。 |
| 材料准备 | 固定来源包的正文组织、完整作者上下文表格转换、明确对象编号范围展开及统一缺口交付；见 [操作说明](docs/operations.md#材料准备正文与表格) 和 [支持范围](docs/design/workflow.md#提取材料的准备)。 |
| Gold 工具 | 旧 v4 只读迁移／比较、草稿检查、当前答案配对的获准保存与读取；见 [生成用法与人工恢复](contracts/generated/gold/README.md)。工具不执行 Git，不自动迁移真实私仓。 |
| 提取与评分基础 | 共享提取入口、专家裁决开关、L0/L1/L2 合成评分及两组报告；**当前两种方法仅为 synthetic-mock，不是真实提取能力**。见 [离线演示](docs/operations.md#合成提取与重评) 和 [评分设计](docs/design/evaluation.md#提取与评分的集成入口)。 |

真实模型提取、裁决网页与正式裁决 CLI、对象关联与资料补全、星表网站及发布尚未提供；OCR／PDF／FITS 表格提取与包外获取不在当前范围。`hvs-catalog methods list --json` 如实列出已登记方法和步骤。

50 篇私有 Gold 已验收，尚未正式评分；材料与科学就绪核对、真实模型验证及具体调用授权仍须完成。关卡状态和实测范围见 [当前进度](.dev/progress.md)，不把合成测试当作真实模型质量证据。

运行只报告调用数与已知 tokens，未知用量为 `null`，不填报货币费用；历史读取边界见 [运行保存要求](docs/design/architecture.md#保存恢复与版本)。

## 安装与验证

需要 Python 3.11 或更高版本和 [uv](https://docs.astral.sh/uv/)。从仓库构建并安装到仓库外的新环境：

```bash
uv build
uv venv /path/to/env
uv pip install --python /path/to/env dist/hvs_catalog-*.whl
```

在仓库外验证，不设置 `PYTHONPATH`：

```bash
/path/to/env/bin/hvs-catalog --help
/path/to/env/bin/hvs-catalog --version
/path/to/env/bin/python -m hvs_catalog --version
```

实测平台为 macOS（Apple Silicon），其他平台未实测。运行依赖以 [包定义](pyproject.toml) 和锁文件为准；开发环境、测试和发行检查见 [验证要求](CONTRIBUTING.md#验证要求)。

## 持久化与恢复演示

用全新独立目录运行离线演示，不联网、不调用真实模型：

```bash
hvs-catalog demo run --space DEMO_SPACE
hvs-catalog demo status --space DEMO_SPACE
hvs-catalog demo decide --space DEMO_SPACE --choice no_overall_conclusion
```

第一步暂停等待固定模拟决定，第三步保存决定并恢复。重复决定、未决答复、进程终止与重复调用风险，以及需保留的空间外调用日志，见 [完整操作说明](docs/operations.md#持久化与恢复演示)。不自动接管、迁移或删除旧空间。

## 阅读入口

- **执行仓库任务**：[AGENTS](AGENTS.md) → 当前状态与任务章节 → 相关设计；不默认通读全部文档。
- **了解产品要求**：[设计总览](docs/design.md) 提供共用科学边界和按任务的专题入口。
- **使用已交付功能**：[操作说明](docs/operations.md)；Gold 的字段、保存与恢复用法从其 [生成入口](contracts/generated/gold/README.md) 查阅。
- **开发和维护**：[CONTRIBUTING](CONTRIBUTING.md)；科学正文见 [英文提取规则](docs/science-rules.md)，字段与模板见 [生成合同](contracts/generated/README.md)。

主仓管理代码及获准的正式星表，参考答案由独立的 `hvs-catalog-gold` 私有仓库管理；资料归属见 [内容进入哪里](docs/design/architecture.md#内容进入哪里)。
