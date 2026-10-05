# hvs-catalog

从高速星研究文献中整理对象、作者判断和测量值，保留原文依据，最终形成可回查来源与修订的星表。研究范围和处理限制见 [科学边界](docs/design.md#产品目标与科学边界)。

## 当前可用能力

| 能力 | 当前范围与入口 |
|---|---|
| Python 包与 CLI | 可独立安装；支持帮助、版本查询和 `python -m hvs_catalog`。 |
| 共同记录格式 | 十类记录的 JSON 读写、修订引用、原文证据校验，以及名单／数值的简化填写格式。见 [记录设计](docs/design/records.md) 和 [生成合同](contracts/generated/README.md)。 |
| 工作空间、运行与裁决 | 固定输入、事务保存、部分结果查询、异步裁决与恢复、整体备份恢复；见 [接口与支持边界](docs/design/architecture.md#工作空间与恢复接口方案)。 |
| PDF 获取 | arXiv 发现、固定版本 PDF 下载和修订检查；支持本地 arXiv／期刊 PDF 导入。默认不下载 LaTeX，`--latex` 仅额外存档。见 [操作说明](docs/operations.md#arxiv-材料获取)。 |
| PDF 材料准备 | 通过本地 MinerU 4.0.10 advanced 解析全部页面，保存原 PDF、原始解析输出、Markdown、图片及页／块定位；提取输入排除解析器生成的读图描述。见 [准备操作](docs/operations.md#材料准备正文与表格)。 |
| 模型接口 | Token Dance 四个固定 Flash 模型及官方上游；参数检查、HTTP／SSE 工具输出、账户共享限流、调用记录和恢复。见 [模型操作](docs/operations.md#模型配置与调用)。 |
| 规则、配置与提示 | 专家辅助和全自动版共用科学正文，仅替换歧义策略；默认 DeepSeek Flash（max 推理）、专家辅助。支持逐步模型配置、离线检查和完整请求预览。见 [配置操作](docs/operations.md#提取配置与提示预览)。 |
| 名单提取 | 从固定 PDF 的文字与原始图片调用模型，以简短引用和固定科学字段提交，局部补正后保存对象、贡献、作者判断、文字／图片证据及未完成范围。专家版保存问题后释放调用，等待日后裁决；全自动版自行解释，不创建专家待办。见 [名单操作](docs/operations.md#名单提取)。 |
| Gold 工具 | 旧 v4 只读迁移／比较、草稿检查、当前答案配对的获准保存与读取；答案格式v2、元数据v3，仅绑定固定PDF；保留原科学审定身份和规则版本。见 [生成用法与人工恢复](contracts/generated/gold/README.md)。 |
| 提取与评分基础 | 单一流程的合成名单／数值演示、L0/L1/L2 评分及专家开／关两组报告。真实提取目前提供名单步骤；数值仍仅有合成流程。见 [离线演示](docs/operations.md#合成提取与重评) 和 [评分设计](docs/design/evaluation.md#提取与评分的集成入口)。 |

DeepSeek Flash的文字与图片输入已通过真实中转调用，并保存过可回查的图片证据；实际名单仍可能因输出格式错误而失败，或出现对象混并与遗漏。图片证据只核对位置和文件，不自动核验读图内容。

真实数值提取、裁决网页与正式裁决 CLI、对象关联与资料补全、星表网站及发布尚未提供。解析文本与原 PDF 之间可能存在表格、符号或数值失真；页面齐全、引文匹配及运行完成均不代表科学质量通过。

完整目标见 [产品设计](docs/design.md)，开发验证和接续事项见 [开发进度](.dev/progress.md)。

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
- **开发和维护**：[CONTRIBUTING](CONTRIBUTING.md)；科学正文见 [专家辅助规则](docs/science-rules.md)／[全自动规则](docs/science-rules-automatic.md)，字段与模板见 [生成合同](contracts/generated/README.md)。

人员与工具分工、开发方式及独立技术审核的启用条件统一见 [开发方案](.dev/plan.md#开发方案)。

主仓管理代码及获准的正式星表，参考答案由独立的 `hvs-catalog-gold` 私有仓库管理；资料归属见 [内容进入哪里](docs/design/architecture.md#内容进入哪里)。
