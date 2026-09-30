# hvs-catalog

从高速星研究文献中整理对象、论文提出的判断和测量值，记录原文出处，识别不同论文中指向同一天体的记录，形成可以回查依据的星表。

计划支持本机文献处理、专家裁决、星表审核和获准发布。使用流程与社区修订方式见产品设计。

## 当前可用能力

目前提供可独立安装的最小 Python 包 `hvs-catalog`，支持命令行帮助、版本查询、`python -m hvs_catalog` 调用，以及 `hvs-catalog demo` 离线演示：用虚构论文展示流程暂停、进程退出后的重启恢复、已保存结果的复用、共享专家作者判断的保存规则，以及“收到响应但尚未保存就退出”造成的重复调用风险（只报告调用数、不模拟金额）。

共同数据记录格式由 `hvs_catalog.records` 定义，涵盖论文、来源包、证据、名单、对象、外部资料、疑问、决定、运行与发布共 10 类记录。支持 JSON 读写、准确修订引用与原文证据校验，并将三种提取步骤的简化填写展开为保存记录。记录职责见 [数据记录与填写格式](docs/design/records.md)，字段说明、JSON Schema 与英文填写模板见 [生成文档](contracts/generated/README.md)。

`hvs_catalog.workspace`、`runtime` 和 `review` 提供工作空间、运行与裁决的 Python 接口，支持固定输入、可靠保存记录和决定、中断后恢复，以及整体备份和恢复到新目录。这些能力已通过离线合成流程、独立进程和仓库外安装验证；保存与恢复规则见 [架构设计](docs/design/architecture.md#工作空间与恢复接口方案)，验收状态与验证范围见 [当前进度](.dev/progress.md)。正式提取、裁决网页与 CLI、星表和发布功能尚未提供。

初始化只接受新目录或空目录；兼容空间重复初始化读取原配置，非空未知目录拒绝且保持不变。完整空间在同级暂存目录准备后整体发布；崩溃残留不自动接管或清理，可另选空目录重建。空间锁仍为非阻塞排他锁，正常释放保留锁文件，进程退出后可重取；操作者不得删除／替换运行中的锁文件或复制活动空间，不提供 `repair_lock`。日常提交和执行恢复按准确修订核验本批／当前执行及依赖，整体备份和空间恢复仍检查全部历史。完成凭据只保存经匹配流程核验的最终检查点 ID；缺失或不符拒绝、不自动补记，不支持人工改写框架内部行或任意损坏自愈。旧开发空间不会自动迁移，真实资料操作须另行确认。

`hvs_catalog.evaluation` 提供 Gold 工具：只读解析旧 contribution Gold v4、生成迁移草稿与逐项比较报告，检查科学答案、PDF 材料与定位。新增与修订共用人类审定具体内容后的检查保存接口，每篇论文维护固定 `answer.json`／`metadata.json` 及内容绑定的 `approval.json`；历史由另获授权的 Git 提交保存，工具不执行 Git。旧草稿须显式整理为当前配对后走同一路径；旧逐版目标、批准与操作记录不会自动接管。只读 `check_save`／CLI `gold inspect-save` 返回待批准摘要；`approved_save`／`gold save` 锁内复核目标、预期旧配对指纹、规则与材料。`GoldTarget.read_current()`／`gold read` 核对当前授权，草稿格式读取不等于正式读取。保存中断留下完整本次回退副本并拒绝正式读取，按说明人工恢复，不提供自动重放；成功但未收到返回的相同请求核对当前内容，不创建第二版。格式、Python／CLI 用法与人工恢复步骤见 [Gold 格式与模板](contracts/generated/gold/README.md)，科学与授权边界见 [评价设计](docs/design/evaluation.md#gold-工具的格式对应方案)。本批未读取、迁移或更改真实 Gold，私仓说明同步须另行获准。

`hvs_catalog.pipeline` 提供日常与评测共用的提取入口：严格请求（不含 Gold、评分或答案通道）、可信方法与步骤登记、独立于方法与模型的专家裁决开关，以及固定输入冻结与恢复。当前只登记合成模拟实现，可用 `hvs-catalog methods` 查询实际可用方法与步骤；真实 direct/candidate_review 提取按计划在后续关卡接入，入口不会冒称它们可运行。命令行另提供两个合成演示：`hvs-catalog extract demo` 用虚构材料走名单→数值→收尾全链路，含开启专家组暂停、模拟决定与恢复，输出可保存；`hvs-catalog score demo` 读取保存的评分输入集，核对输入指纹后离线重评，可在新进程重复执行并得到相同结果，输入被改动即拒绝。

`hvs_catalog.evaluation` 在 Gold 工具之外提供评分集成：既有 Gold 共同定义到评分输入的纯内存适配（绑定校验与不读文件的准备检查作为程序化就绪门禁：缺源对应、在案材料差异、缺证据、缺条件、未决待办等缺口阻止就绪；answer-only 输入被严格入口拒绝、不构成正式就绪；部分覆盖的 Gold 须显式声明封闭子范围）、合成 Gold 与评分计划，以及专家裁决开/关两组的对照实验报告。报告逐论文给出 L0/L1/L2 分层质量与完成/未决/失败、问题与专家次数、调用用量与专家用时，不合成总分；科学完成状态与运行图终止状态分开报告，单组或两组失败仍生成完整两组报告并保留分母与原始错误，未知用量与用时为 null。以上均为合成离线材料，不代表真实模型能力；正式评分需要获准的私有评分空间与 Gold 核对，评分辅助仍为 protocol_only 模拟模式，真实辅助模型验证按计划另行获准进行；适配与两组报告的完整语义见 [评价设计](docs/design/evaluation.md#提取与评分的集成入口)。

运行与演示不计算、保存或比较货币费用：新记录不填写费用字段，用量只报告调用数与 tokens，未知用量为 null。历史运行记录中的有值费用字段仍可读取；退役演示空间与 SQLite 账本原样保留，不由新演示读取、迁移、追加或清理。旧 `SyntheticFlow`、独立演示业务库和 Python 辅助入口已退役；提取使用 `hvs_catalog.pipeline`，入门演示使用下述新命令。

`hvs-catalog acquisition` 提供 arXiv 源码包获取子系统：受限检索发现、版本冻结与固定版本获取、修订检查、本地导入与状态查询。发现轮次按首提日期窗口与检索词/类别分页推进，每页溯源（含归档页文档）持久化在工作空间；未带版本的获取先查元数据选定当时最新版本并在下载前持久化冻结，恢复绝不悄悄改选更新版本；修订检查独立于发现窗口，只记录待处理状态。首次使用前用公开 Python 接口初始化工作空间（状态命令不会隐式初始化）：

```bash
python -c "from hvs_catalog.workspace import initialize_workspace; initialize_workspace('./ws')"
```

命令示例：

```bash
hvs-catalog acquisition discover --space ./ws --from 2024-01-01 --to 2024-01-31 \
    --terms "dark matter" --category astro-ph.CO      # 发现（退出码 3 = 未完可恢复）
hvs-catalog acquisition fetch --space ./ws --id 2401.01234            # 获取（或恢复）
hvs-catalog acquisition revisions --space ./ws                       # 修订检查（新版→pending）
hvs-catalog acquisition import --space ./ws --file pkg.tar.gz \
    --id 2401.01234v2 --provenance "从镜像站获得"                      # 本地导入（身份与来源声明必填）
hvs-catalog acquisition status --space ./ws [--json]               # 当前选择与全部已登记版本
hvs-catalog acquisition status --space ./ws --id 2401.01234v1 --json # 回查旧版材料与来源，不切换选择
hvs-catalog acquisition material --space ./ws --id 2401.01234       # 选定版本材料引用（交付提取）
```

获取工程状态采用格式 v2：当前选择仅指向逐版本事实，材料与首登来源以保存记录为准；成员清单、来源日志和版本历史在查询时派生，不保存选择世代或多份历史快照。Python 可用 `get_version("2401.01234v1")` / `list_versions()` 回查材料；CLI `status --json` 的 `selections` 表示当前选择，`versions` 表示已登记版本。重复显式获取／导入可切回旧版，重复来源声明不改首登时间；历史按不同版本列出，不逐次记录选择动作。旧获取格式明确拒绝：请新建空间并重新获取／导入；不会自动迁移、重置或删除旧文件，真实数据迁移须另行确认。已提交的原包在重试时复用；发布后尚未提交就退出仍可能重复外部请求。

退出码区分结果：0 成功；1 工程错误；3 发现未完（可恢复）；4 远程材料形态不支持（原始字节已保留）；5 源码不可用（待处理，不是“科学上无关”）；6 无可用材料；7 修订检查未能全部核实（unknown，不能当成无新版）。工程边界：同一工作空间的发现、获取、修订检查与本地导入从操作开始到收尾持有空间排他锁，其他写调用立即返回“忙”（不排队、不自动重试；锁随进程退出释放，之后可重试）；网络等待不放在数据库事务内，网络限流仍为本机全局（跨工作空间共享，含发送间隔与崩溃在途标记），出现未确认在途状态时需人工协调恢复，CLI 不提供重置入口；限流状态文件版本不兼容时拒绝使用。包安全检查在解压前有界（解压输出额度在分配前限定，成员数逐项解析立即停止，总量/路径/链接等护栏，形态不支持与获取失败分开报告）；“形态不支持”分两种：**远程获取**已收到的原始字节在工作空间内保留（挂版本事实与获取记录，随整体备份恢复）；**本地导入**在发布前拒绝——文件未导入工作空间（无选择、尝试记录或备份引用），原文件不改动，CLI 以非零退出码报告。归档兼容范围（明确列出，特殊变体报告“不支持”而非仿真解析）：支持普通 tar、gzip 封装 tar、单个 TeX 与 gzip 单 TeX；tar 扩展头支持**每个成员至多一个**、负载能完整切分的局部 PAX（`x`）与 GNU 长名（`L`），且 PAX 记录名必须在**允许清单**内（纯元数据键：`path`/`linkpath`/`mtime`/`ctime`/`atime`/`comment`/`uid`/`gid`/`uname`/`gname`/`hdrcharset`）；明确拒绝（在进入解析前报告不支持、字节在限额内保留）：一切不在清单内的 PAX 字段——含 `size`（缩放/覆盖布局）、全部 `GNU.sparse.*`（稀疏布局）及其他未承诺命名空间、连续扩展头链（多级覆盖）、全局 PAX 头（`g`）、GNU 长链接（`K`）、链接/设备/管道成员、路径越界或别名冲突、截断或尾随数据；不把不支持的归档当普通文本处理，支持键上的元数据解析失败以可观察的完整性错误报告而非静默回退。特殊变体（如 PAX size）在真实 arXiv 材料中的出现率与受影响规模尚未验证，不支持是明确的能力边界而非对罕见性的判断。页级溯源是解析视图（含摘要等全部返回元数据，非原始 Atom 线路响应）；全部获取进度、溯源与保留字节都在工作空间内，随整体备份/恢复。生产 HTTP 传输使用 requests，关闭自动重定向与底层重试；每一跳仍由共享限流门管理，响应按原始字节有界读取。已完成小范围真实检索及单篇固定版本源码获取、重复获取与备份恢复验证，不代表全量检索覆盖或全部归档形态兼容；断电级耐久性与跨机协调未验证。

Gold 修订先在独立工作副本整理，保留原始标注身份及修订依据，获具体内容批准后以同一接口保存到固定路径；未提交副本不冒充 Git 历史。约定见 [Gold 维护](docs/design/evaluation.md#gold-的局部修正与-git-历史)。

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

实测平台为 macOS（Apple Silicon）；各阶段的 Python 版本与验证范围见 [当前进度](.dev/progress.md)。安装包为纯 Python wheel，运行时依赖 LangGraph（流程编排与检查点，含 SQLite 检查点后端）、Pydantic（记录格式定义与校验）、pypdf（Gold 材料的 PDF 解析）及 requests/urllib3（arXiv HTTP 传输）；其他平台未实测。

## 持久化与恢复演示

`hvs-catalog demo` 是共享 `pipeline`、`workspace`、`runtime`、`review` 的薄封装，全程 synthetic-mock、不联网、不调用真实模型。请指定全新独立目录 `SPACE`；不接管日常空间或旧三库演示，也不提供 reset 删除捷径。空间保存共同业务记录、固定输入及 LangGraph 检查点；同级 `SPACE.calls.jsonl` 代表外部服务的追加调用事实，不进入客户端事务或备份。保留这份日志才能跨进程核对已经发生的调用；不要把它当客户端缓存清理。

虚构论文 FIC-R05-A 报告 FIC-A-1 的径向速度 520 ± 10 km/s；作者在 M1/M2 下给出相反判断，未给总体结论。固定模拟专家决定把作者判断记为“无总体结论”，原值、条件与证据保留，不进行物理推算或对象合并。

```bash
hvs-catalog demo run --space SPACE        # 名单→数值，等待共享裁决；进程退出
hvs-catalog demo status --space SPACE     # 新进程查看 JSON：固定输入、结果、待办、调用数
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion  # 同一决定回放，不再生效
hvs-catalog demo decide --space SPACE --choice keep_pending          # 此时冲突，退出码 2
```

正常暂停与完成累计两次服务调用；恢复复用已存结果及归档响应。同一决定只保存、应用一次，不同决定拒绝。在另一个新目录可直接选 `keep_pending`，演示答复已保存但仍未决、继续暂停。输出中 `config`/`input_digest` 是固定输入，`steps`/`result_refs` 是已存成果，`pending` 是问题及证据，`external_calls` 是独立服务累计事实，不等于客户端成功保存的尝试数。

用另一个新目录演示“响应已到、尚未归档”的真实死亡窗口：

```bash
hvs-catalog demo run --space CRASH_SPACE --crash-after-response  # 进程退出码 70
hvs-catalog demo status --space CRASH_SPACE                    # 外部已调用一次
hvs-catalog demo run --space CRASH_SPACE                       # 重启，再调用；累计三次并暂停
hvs-catalog demo decide --space CRASH_SPACE --choice no_overall_conclusion
```

恢复沿用检查点中的原材料与固定输入，缺失／错版引用拒绝、不换新版。外部调用与本地保存不是同一事务，因此未保存窗口仍可能重复请求（真实服务可能重复计费），本演示不模拟金额。旧 `merge`/`keep-separate`、`reset` 和旧 Python 图／存储接口已移除；旧空间及账本不动，另选目录按新用法开始。

提取并保存可重评的合成评分输入（输出目录须在客户端空间外）：

```bash
hvs-catalog methods list --json
hvs-catalog extract demo --space EXTRACT_SPACE --paper FIC-R05-A \
    --method direct --adjudication on --output EXTRACT_OUTPUT
hvs-catalog score demo --inputs EXTRACT_OUTPUT/evaluation-inputs.json
```

`extract demo` 也支持 `candidate_review`、专家开／关、B/C 合成材料和 `--decision keep_pending` 的未决输出；`score demo` 在新进程重评，输入被篡改则拒绝。Gold 与人工固定评分预期仅由评分侧使用，不进入提取请求。保存恢复的详细职责见 [架构设计](docs/design/architecture.md#合成演示与回归验证)。

## 阅读入口

- [设计总览](docs/design.md)：共用科学边界和按任务阅读的入口；架构、工作流程、裁决、提取、数据格式与评价各有独立正文。
- [文献提取规则（英文）](docs/science-rules.md)：已获专家审定的七条主规则、定义与合成例子；中文说明在审阅时按需提供，正式提取功能按后续计划交付。
- [开发与文档维护](CONTRIBUTING.md)：怎样开发、检查和执行 Git 操作，以及怎样保持文档与实际工作一致。
- [Agent 入口](AGENTS.md)：执行仓库任务前的阅读要求。

主仓计划管理代码与正式星表。用于评价提取结果的参考答案称为 Gold，由独立的 `hvs-catalog-gold` 私有仓库管理。各类资料可以放在哪里、程序和数据如何提供下载，见产品设计。
