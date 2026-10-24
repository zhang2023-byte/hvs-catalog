# hvs-catalog

从高速星文献中整理对象、作者判断和测量值，保留原文依据，建设可回查来源与修订的星表。

当前已完成文献获取、PDF解析、名单与数值提取、来源核验、跨论文关联及SIMBAD补充。仓库中的[catalog](catalog/manifest.json)是可独立读取的JSON星表包，包含单星科学数据、论文来源和统一追溯文件；网页、下载入口与Agent Skill留待R21。数据版本与数量以manifest为准，构建状态见[Progress](.dev/progress.md)，命令与字段见[生成合同](contracts/generated/catalog-format.md)。

## 安装

需要Python 3.11或更高版本和uv。开发环境按锁文件安装：

```bash
uv sync --frozen --python 3.12
uv run python -m hvs_catalog --help
```

目录搬迁后应重建虚拟环境，不能复用带旧绝对路径的启动脚本。独立安装使用wheel，在仓库外运行且不设置PYTHONPATH：

```bash
uv build
uv venv /path/to/env --python 3.12
uv pip install --python /path/to/env/bin/python dist/hvs_catalog-*.whl
/path/to/env/bin/hvs-catalog --help
```

## 文献处理最小流程

以下为命令示例，日期、检索词、路径与运行范围须由操作者确定。独立业务空间通过`--space`指定；当前本机已有材料与结果的入口见Progress。检索、下载、MinerU解析和模型提取分别触发外部服务；初始化、配置检查与状态查询离线执行。

```bash
hvs-catalog prepare init --space /path/to/literature --batch sample --from 2026-01-01 --to 2026-01-31 --term "hypervelocity star"
hvs-catalog prepare discover --space /path/to/literature --batch sample
hvs-catalog prepare download --space /path/to/literature --batch sample
hvs-catalog prepare parse --space /path/to/literature --batch sample --env-file /path/to/.env
hvs-catalog prepare status --space /path/to/literature --batch sample
```

解析使用MinerU官方v4的vlm接口，凭据为`MINERU_TOKEN`；提取通过Token Dance使用`TOKENDANCE_API_KEY`。凭据从进程环境或显式指定的私密环境文件读取，不放入Git。空间保留固定PDF、解析原件及索引；各阶段不会自动启动下一阶段。

先输出提取配置，并核对两阶段型号、供应商、输入模式、并发和超时：

```bash
hvs-catalog extract config > /path/to/extraction.json
hvs-catalog extract check --config /path/to/extraction.json
```

默认名单Max、数值Low，型号为`deepseek-v4.1-flash`别名。要使用最近文献批次的固定型号，应在配置中同时将`default_model.model`和`step_models.quantities.model`设为`deepseek-v4.1-flash-1017`，供应商均为`deepseek`；其他参数按获准运行配置明确设置，不把默认配置当成旧批次完整配置。

从`reports/preparation-manifest.json`取得目标论文的`preparation.execution_id`，替换下例准备编号；`--request-key`标识本次固定输入：

```bash
hvs-catalog extract run --space /path/to/literature --preparation PREPARATION_ID --request-key sample-paper --config /path/to/extraction.json --env-file /path/to/.env
hvs-catalog extract status --space /path/to/literature --execution EXECUTION_ID
```

`extract run`默认完成名单、数值和组装，返回执行编号及结果引用。最终文件为`run.json`、存在时的`literature_contribution.json`及同代`evidence_audit.json`；按准确引用读取，不从目录名猜最新结果。`complete/partial/failed`区分交付状态，不保证科学正确。未结束执行可用`extract resume`接续，已明确结束的执行不能重新请求模型。

多篇可用`extract batch`重复指定`--preparation`；已固定跨空间执行使用`--manifest`，加`--check-only`仅核验。当前CLI新建执行使用自动编号；按arXiv ID命名须通过共同Python接口`start_extraction(..., execution_id=arxiv_id)`在创建时绑定，不能事后改名。完整参数见对应`--help`，共同接口见[提取入口](src/hvs_catalog/pipeline/entry.py)。

## Benchmark与保存

实验入口为`hvs-catalog benchmark --root /path/to/benchmark --help`。材料显式选择`mineru_local`或`mineru_cloud`，不隐式回退；纯文本／含图模式另由配置固定。`import-materials`、`preview`和`prepare`不请求模型；`run --round execNN`执行或接续，重复实验另建编号，已结束实验不能续跑。`status/check/report`查看保存事实，`score`读取明确指定的参考答案，旧评分不覆盖。

文献空间及Benchmark均须整体保存，包含`.state/`及其共享证据和恢复记录；迁移使用共同备份恢复接口。`.work/`、凭据、PDF、私有Gold和原始运行结果不随Git克隆迁移；已纳入仓库的catalog随克隆交付，wheel只含程序。派生成果保留原执行与准确引用，当前本机输入入口见Progress。

## 开发入口

- [AGENTS](AGENTS.md)：模型阅读路由。
- [Plan](.dev/plan.md)：目标、开发规则、共同要求与后续任务。
- [Progress](.dev/progress.md)：当前交接、验证、结果与已知限制。

科学规则与格式说明从源定义生成，按Plan的[任务读取表](.dev/plan.md#按任务读取)进入；检查要求见[验证要求](.dev/plan.md#验证要求)。
