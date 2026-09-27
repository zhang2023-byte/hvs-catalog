# hvs-catalog

从高速星研究文献中整理对象、论文提出的判断和测量值，记录原文出处，识别不同论文中指向同一天体的记录，形成可以回查依据的星表。

计划支持本机文献处理、专家裁决、星表审核和获准发布。使用流程与社区修订方式见产品设计。

## 当前可用能力

目前提供可独立安装的最小 Python 包 `hvs-catalog`，支持命令行帮助、版本查询、`python -m hvs_catalog` 调用，以及 `hvs-catalog demo` 离线演示：用虚构论文展示流程暂停、进程退出后的重启恢复、已保存结果的复用、对象身份裁决的保存规则，以及“收到响应但尚未保存就退出”造成的重复调用与计费。

共同数据记录格式由 `hvs_catalog.records` 定义，涵盖论文、来源包、证据、名单、对象、外部资料、疑问、决定、运行与发布共 10 类记录。支持 JSON 读写、准确修订引用与原文证据校验，并将三种提取步骤的简化填写展开为保存记录。记录职责见 [数据记录与填写格式](docs/design/records.md)，字段说明、JSON Schema 与英文填写模板见 [生成文档](contracts/generated/README.md)。

`hvs_catalog.workspace`、`runtime` 和 `review` 提供工作空间、运行与裁决的 Python 接口，支持固定输入、可靠保存记录和决定、中断后恢复，以及整体备份和恢复到新目录。这些能力已通过离线合成流程、独立进程和仓库外安装验证；保存与恢复规则见 [架构设计](docs/design/architecture.md#工作空间与恢复接口方案)，验收状态与验证范围见 [当前进度](.dev/progress.md)。正式提取、裁决网页与 CLI、星表和发布功能尚未提供。

`hvs_catalog.evaluation` 提供 Gold 工具：读取旧 contribution Gold v4 生成迁移草稿与逐项报告，检查科学答案、PDF 材料与定位，并在具体内容获得人类批准后保存带版本入口的正式答案，支持操作查询与中断恢复。精简格式将科学答案和对应证据放在 `answer.json`，论文与材料版本、文件指纹、标注者和审阅依据放在 `metadata.json`；旧迁移草稿仍可读取。字段说明、JSON Schema 与英文填写模板由有类型定义生成，见 [Gold 格式与模板](contracts/generated/gold/README.md)，命令和保存绑定要求见 [评价设计](docs/design/evaluation.md#gold-工具的格式对应方案)。

已明确裁定的局部修正直接修改原有 Gold 文件，由 Git 保存历史；检查与修订依据要求见 [局部修正约定](docs/design/evaluation.md#gold-的局部修正与-git-历史)。

50 篇真实论文的私有 Gold 已通过验收，尚未评分，具体进度见 [当前交接](.dev/progress.md)。文件归属与工具目标见 [Gold 仓布局](docs/design/architecture.md#gold-仓目标布局)，保存授权见 [批准说明](contracts/generated/gold/templates/gold-approval.md)；格式转换和内容审阅不自动构成正式保存授权。

## 安装与验证

需要 Python 3.11 或更高版本和 [uv](https://docs.astral.sh/uv/)。

从仓库构建安装包，并安装到仓库外的全新虚拟环境：

```bash
uv build                                          # 在仓库根目录生成 dist/ 下的源码包与 wheel
uv venv /path/to/env                              # 在仓库外创建全新虚拟环境
uv pip install --python /path/to/env dist/hvs_catalog-*.whl
```

在仓库外验证安装，不依赖源码路径，也不设置 PYTHONPATH：

```bash
/path/to/env/bin/hvs-catalog --help
/path/to/env/bin/hvs-catalog --version
/path/to/env/bin/python -m hvs_catalog --version
```

开发环境与检查命令见 [验证要求](CONTRIBUTING.md#验证要求)。

实测平台为 macOS（Apple Silicon）；各阶段的 Python 版本与验证范围见 [当前进度](.dev/progress.md)。安装包为纯 Python wheel，运行时依赖 LangGraph（流程编排与检查点，含 SQLite 检查点后端）、Pydantic（记录格式定义与校验）和 pypdf（Gold 材料的 PDF 解析）；其他平台未实测。

## 持久化与恢复演示

`hvs-catalog demo` 演示 LangGraph 保存的流程进度与应用保存的业务数据各自负责什么，全程离线、不访问网络、不调用真实模型。演示空间是自选的独立目录（下文以 `SPACE` 代替），内含三个数据库：

- `checkpoint.db`：LangGraph 检查点，框架负责，保存流程执行到哪里、中断现场与本次运行固定的输入（规则、材料快照、模型）；
- `business.db`：业务数据库，应用负责，保存步骤结果、裁决决定与汇总；
- `service.db`：模拟服务账单，扮演远程服务端，只增不改，不随客户端进程退出回滚。

演示的裁决问题是对象身份：两名候选的测量完全相同且命名相邻，是否识别为同一天体（`--choice merge` 或 `--choice keep-separate`）。问题正文按当前证据生成，展示每条测量与来源。决定只影响演示摘要的对象分组，不挑选任何测量值；全部测量与来源原样保留。恢复一律沿用运行开始时固定的输入——材料以完整快照保存在运行状态中，即使响应尚未保存就退出，恢复重新调用也按快照应答，不读取当前数据；当前代码或材料与固定输入不一致时命令会给出提示。决定属于特定版本的问题（摘要覆盖实际证据与选项）：问题变化后旧决定不会被应用到新问题，而是就新问题重新等待决定。

场景一：暂停后实际重启进程，能找回待办、复用已保存结果：

```bash
hvs-catalog demo run --space SPACE      # 执行到裁决处暂停，进程结束
hvs-catalog demo status --space SPACE   # 新进程查看待办、固定输入、已保存结果与账单
hvs-catalog demo decide --space SPACE --choice merge   # 保存决定（先落盘）并恢复到完成
```

恢复时所在步骤会从头重跑：输入相同则直接复用业务库已保存结果，不再调用服务。同一决定重复提交只生效一次；换一个不同的决定会以退出码 2 被拒绝。

场景二：收到响应但尚未保存就退出，可能造成重复调用及计费。开始前先重置空间（或另选一个新目录），保证从全新的运行开始：

```bash
hvs-catalog demo reset --space SPACE                              # 先重置，保证全新运行
hvs-catalog demo run --space SPACE --crash-after-response   # 收到响应后、保存前终止进程（退出码 70）
hvs-catalog demo run --space SPACE   # 重启恢复：业务库没有该结果，重新调用服务，账单再次计费
```

保存与恢复的完整规则见[产品设计](docs/design/architecture.md#保存恢复与版本)。`hvs-catalog demo reset --space SPACE` 清空空间后可重新演示。

## 阅读入口

- [设计总览](docs/design.md)：共用科学边界和按任务阅读的入口；架构、工作流程、裁决、提取、数据格式与评价各有独立正文。
- [文献提取规则（英文）](docs/science-rules.md)：已获专家审定的七条主规则、定义与合成例子；中文说明在审阅时按需提供，正式提取功能按后续计划交付。
- [开发与文档维护](CONTRIBUTING.md)：怎样开发、检查和执行 Git 操作，以及怎样保持文档与实际工作一致。
- [Agent 入口](AGENTS.md)：执行仓库任务前的阅读要求。

主仓计划管理代码与正式星表。用于评价提取结果的参考答案称为 Gold，由独立的 `hvs-catalog-gold` 私有仓库管理。各类资料可以放在哪里、程序和数据如何提供下载，见产品设计。
