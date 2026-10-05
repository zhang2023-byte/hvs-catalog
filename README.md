# hvs-catalog

从高速星研究文献中整理对象、作者判断和测量值，保留原文依据，最终形成可回查来源与修订的星表。研究范围和处理限制见 [科学边界](docs/design.md#产品目标与科学边界)。

## 当前可用能力

| 能力 | 当前范围与入口 |
|---|---|
| Python 包与 CLI | 可独立安装；支持帮助、版本查询和 `python -m hvs_catalog`。 |
| 共同记录格式 | 十类记录的 JSON 读写、准确修订引用、原文证据校验与步骤填写格式；候选发现使用共同字段的临时清单，名单／候选判断／数值支持简化填写展开。见 [记录设计](docs/design/records.md) 和 [生成合同](contracts/generated/README.md)。 |
| 工作空间、运行与裁决 | Python 接口支持固定输入、事务保存、暂停恢复和整体备份恢复；见 [接口与支持边界](docs/design/architecture.md#工作空间与恢复接口方案)。 |
| arXiv 获取 | CLI／Python 支持按条件发现、固定版本源码包获取、本地源码包导入、修订检查和状态查询；当前没有PDF下载或关闭LaTeX的选项。见 [操作说明](docs/operations.md#arxiv-材料获取)。 |
| 材料准备 | 固定来源包的正文组织、完整作者上下文表格转换、明确对象编号范围展开及统一缺口交付；见 [操作说明](docs/operations.md#材料准备正文与表格) 和 [支持范围](docs/design/workflow.md#提取材料的准备)。 |
| 模型接口 | Token Dance 四个固定 Flash 模型及官方上游；支持参数检查、HTTP／SSE 工具输出、账户共享限流、调用记录和恢复；见 [操作说明](docs/operations.md#模型配置与调用)。 |
| 提取配置与提示 | 默认DeepSeek Flash、专家求助；支持逐步模型配置、英文提示组装、离线检查与完整请求预览，以及固定输入的模拟运行／恢复。见 [操作说明](docs/operations.md#提取配置与提示预览)。 |
| 名单提取（当前源码输入） | 从已完成的固定源码准备输入调用真实模型，保存对象、研究摘要、贡献类型、作者判断、证据与未决问题；使用共同运行、裁决与恢复接口。见 [操作说明](docs/operations.md#名单提取)。数值尚未检查，提取质量待真实评价。 |
| Gold 工具 | 旧 v4 只读迁移／比较、草稿检查、当前答案配对的获准保存与读取；见 [生成用法与人工恢复](contracts/generated/gold/README.md)。工具不执行 Git，不自动迁移真实私仓。 |
| 提取与评分基础 | 共享提取入口、专家裁决开关、L0/L1/L2 合成评分及两组报告；支持预声明材料／作者问题范围，保留默认分并同步排除 PDF／源码不对齐范围重算参考分。两种完整流程仍为 synthetic-mock；实际提取目前交付名单步骤。见 [离线演示](docs/operations.md#合成提取与重评)、[Gold 评分准备](docs/operations.md#gold-检查保存与人工恢复) 和 [评分设计](docs/design/evaluation.md#提取与评分的集成入口)。 |

实际数值提取、裁决网页与正式裁决CLI、对象关联与资料补全、星表网站及发布尚未提供；PDF解析也尚未接入。`hvs-catalog methods list --json` 列出当前代码中的完整流程实现及已交付的 `model_steps`，合成方法不代表真实提取能力。

目标流程以固定PDF为依据，依次完成名单和数值提取；LaTeX仅供手动下载存档。专家辅助与全自动共用科学正文，只在歧义处理策略上不同。上述PDF流程与两版规则尚未实现，当前纯AI配置仍允许保留解释未决。完整目标见 [产品设计](docs/design.md)，实施安排与接续事项见 [开发进度](.dev/progress.md)。

已有50篇经专家验收的私有Gold，尚未正式评分。正式评价前须核对当前材料与评分范围、验证辅助评分模型，并取得具体调用授权。合成演示和接口连通不代表真实论文提取质量。

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

人员与工具分工、开发方式及独立技术审核的启用条件统一见 [开发方案](.dev/plan.md#开发方案)。

主仓管理代码及获准的正式星表，参考答案由独立的 `hvs-catalog-gold` 私有仓库管理；资料归属见 [内容进入哪里](docs/design/architecture.md#内容进入哪里)。
