# 当前功能操作说明

仅说明已交付功能的用法，不代表后续提取、网页、星表或发布功能已可用。安装见 [README](../README.md#安装与验证)，产品要求见 [设计总览](design.md)。真实论文／服务、模型、Gold 和评分操作仍须取得 [具体范围授权](../CONTRIBUTING.md#开发工作约定)。

## 工作空间与恢复

初始化入口必须显式指定新目录或空目录；状态查询不会隐式初始化：

```bash
python -c "from hvs_catalog.workspace import initialize_workspace; initialize_workspace('./ws')"
```

兼容空间重复初始化保留原配置，非空未知目录拒绝且保持不变。中断残留不自动接管或清理，可另选空目录重建。旧开发空间不自动迁移，真实资料迁移另行确认。

同空间写入、推进和备份串行持有排他锁；忙时立即拒绝，不排队。不要删除／替换运行中的锁文件或复制活动空间，文件存在不等于正在持锁。原材料／固定输入缺失、错版或损坏时停止，不用新版补替，不提供 `repair_lock` 或任意损坏自愈。

备份和恢复当前通过 Python 的 `backup_workspace`／`restore_workspace` 使用，恢复只接受新目录。参数、完整性要求及中断处理见 [共同接口](design/architecture.md#模块与共同入口) 和 [备份恢复](design/architecture.md#备份与恢复验证)；不能直接复制活动数据库当作完整备份。

## arXiv 材料获取

发现、获取、导入和修订检查共用空间锁；不同空间也共享本机网络节流。首次使用先初始化空间，然后按获准范围执行。以下标识和查询仅展示命令形态，不构成真实服务调用授权。

```bash
hvs-catalog acquisition discover --space ./ws --from 2024-01-01 --to 2024-01-31 \
    --terms "dark matter" --category astro-ph.CO
hvs-catalog acquisition fetch --space ./ws --id 2401.01234
hvs-catalog acquisition revisions --space ./ws
hvs-catalog acquisition import --space ./ws --file pkg.tar.gz \
    --id 2401.01234v2 --provenance "从镜像站获得"
hvs-catalog acquisition status --space ./ws --json
hvs-catalog acquisition status --space ./ws --id 2401.01234v1 --json
hvs-catalog acquisition material --space ./ws --id 2401.01234
```

- `discover` 按首提日期、检索词和类别分页，保存每页及未完进度；`fetch` 可继续未完成的获取。未指定版本时先选定当时最新版并保存冻结，恢复不改选新版。
- `revisions` 独立检查已登记论文的新版本，记录待处理／未知，不受新文献发现日期窗口限制，不自动提取新版。
- 本地导入必须明确声明 arXiv 标识、版本和来源，不能从文件名猜身份；同版本异内容拒绝。
- `status --json` 的 `selections` 是当前选择，`versions` 是已登记的各版材料与来源。带版本查询不改变选择；重复显式获取／导入可以切回旧版，历史按版本列出，不逐次记录切换动作。`material` 交付当前选定版本的材料引用。
- 旧获取格式不兼容时须新建空间并重新获取／导入；程序不自动迁移、重置或删除旧数据。来源保存与格式 v2 的内部职责见 [获取设计](design/workflow.md#文献检索与材料获取)。

### 退出结果与故障

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 工程错误、空间忙或本地导入拒绝；被拒绝的本地文件未导入 |
| 2 | 命令参数错误 |
| 3 | 发现未完，可恢复；不能当作完整覆盖 |
| 4 | 远程材料形态不支持，收到的原始字节已保留 |
| 5 | 源码不可用，保留待处理；不表示科学上无关 |
| 6 | 无可用材料 |
| 7 | 修订检查未全部核实，状态为 unknown；不表示没有新版 |

网络临时失败有限重试，拒绝访问时停止。共享限流出现未确认在途状态时须人工协调恢复，CLI 不提供重置入口；状态版本不兼容时拒绝。已提交原包可在重试时复用，响应／文件尚未提交的中断仍可能造成重复请求。获取与网络等待不在长期数据库事务内。

页级溯源保存完整解析视图（含摘要等返回元数据），不是原始 Atom 线路响应。获取进度、页溯源和保留的原包均随空间备份恢复；HTTP 传输关闭底层自动重试／重定向，每一跳经过共享节流并有界读取。

### 支持的包形态

只支持普通 tar、gzip 封装 tar、单个 TeX 与 gzip 单 TeX，并在解压前限制尺寸、成员数、路径及解压输出。

- 每成员至多一个可完整切分的局部 PAX（`x`）或 GNU 长名（`L`）扩展头。PAX 只允许 `path`、`linkpath`、`mtime`、`ctime`、`atime`、`comment`、`uid`、`gid`、`uname`、`gname`、`hdrcharset`。
- 拒绝其他 PAX 字段（包括 `size`、全部 `GNU.sparse.*`）、连续扩展头链、全局 PAX（`g`）、GNU 长链接（`K`）、链接／设备／管道成员、路径越界／别名冲突、截断及尾随数据。不把不支持的归档当普通文本处理；支持键的元数据解析错误也不静默回退。
- **远程获取**：限额内已收到的原始字节保留在空间内，关联版本和获取记录，进入整体备份。**本地导入**：不支持的文件在发布前拒绝，原文件不改动，无空间选择、尝试记录或备份引用；不能误认为已保存到空间。

实际特殊归档形态的出现率、全量检索覆盖、跨机协调和断电耐久尚未验证；明确的“不支持”不代表该形态罕见。

## 材料准备（正文与表格）

当前能力见 [README](../README.md#当前可用能力)，验收与验证范围见 [任务总表](../.dev/progress.md#任务总表)。仍须核对输入的已知缺口，工程准备完成不代表材料完整或科学判断已确认。

从获取阶段已保存的源码包准备提取输入：安全展开成员、组织主稿正文／附录、转换常用表环境为 ECSV、确定性展开对象编号范围，并把已知缺口随输入一起交付。入口复用工作空间锁、runtime、事务与 R05 裁决接口；准备接口不接收 Gold。

```bash
hvs-catalog materials prepare --space ./ws --paper 2501.01111 --request-key rk-1 [--root-file main.tex] [--version N] [--encoding utf-8|latin-1] [--expert-adjudication] [--range "HVS1--HVS5|sec1.tex|1|1"] [--json]
hvs-catalog materials status --space ./ws [--execution exec-000001] [--json]
hvs-catalog materials resume --space ./ws --execution exec-000001 [--json]
hvs-catalog materials consume --space ./ws --execution exec-000001 [--json]
```

Python 共用入口：`MaterialsService.prepare_materials`／`resume_preparation`／`get_preparation`／`get_prepared_input`／`consume_prepared_input`；纯函数 `expand_range_notation` 供编号范围展开。日常与评测消费同一份输入；无模型合成消费者 `consume_prepared_input` 核对全部附件引用与 ECSV 回读；`ok` 仅表示工程完整性，`materials_complete` 另表示是否存在材料缺口，不能混同科学验收。

行为要点：

- 输入是获取阶段保存的准确来源修订与原包哈希，不重新下载；准备结果另存来源包新修订（成员、派生 UTF-8 文本与缺口），不覆盖旧修订。同请求同输入重放复用已保存结果；改输入（含主稿选择、范围记号、转换器版本、解析代码或观测绑定）须用新请求键；当前材料流程为版本2，不自动混用旧检查点。
- 主稿唯一时自动确定；多个有效主稿必须 `--root-file` 显式选择，不按名称猜。`\input`／`\include` 只跟随字面路径；缺件、循环、越界、动态或不支持的包含形式逐项记入缺口，不使有效主稿整体失败；未引用草稿保留在清单中，不断言科学无关。
- 正文去注释保持行位置，保护 verbatim。成员解码只按明确依据：UTF-8 直接解码；成员声明（`% !TeX encoding`）或操作者 `--encoding` 固定选择为 latin-1 时派生保留行数的 UTF-8 文本并登记映射；未声明且 UTF-8 失败的成员保持未处理，不猜编码。
- 支持 `tabular`／`tabular*`、`longtable`、`deluxetable`／`deluxetable*` 及可证明的 `pgfplotstable` backing CSV。包内 CSV／ECSV 按逻辑记录读取，JSON／普通文本按实际结构定位；FITS、自定义 DAT、图片／PDF 表格保留材料与提及，不做 OCR／PDF 提取，包外链接不获取。具体转换与回退条件见 [支持格式和对应要求](design/workflow.md#表格转换与原件对应)。
- 普通表在只读完整作者上下文中经一次沙箱 LaTeXML 转换，可以包含多张表；按原生来源位置和每次执行定位，不猜首表／表序。最低对应粒度为表块，不宣称每个单元格的精确 TeX 行号；无法证明对应的内容保留诊断和 Gap。联合核对进程、转换诊断、错误节点、表身份及完整网格，退出码为零不够，普通字面表不猜首行为表头。工具缺失或无法安全隔离时保留有效材料和缺口，不裸跑；实测隔离及系统依赖边界见 [技术职责](design/architecture.md#技术选择与责任)。
- 转换表以字符串保留原表达：中性列名 `col_001`、多层表头、单位、标题、脚注与合并格结构；ECSV 写出后回读逐单元格核对。对象行定位按命中行键处理：零命中失败，唯一命中可不填序号，多命中必须补键或明确序号，不默取第一行（保存／备份／消费共用同一接口）。
- 编号范围仅对含义明确的稳定前缀对象记法确定性有界展开（保留前导零、原写法、规则与成员）；物理量区间与坐标符号不参与展开。材料准备的 `--expert-adjudication` 默认关闭，独立于提取入口默认开启的裁决配置。语义不明的范围只有在开启此选项且明确提供候选解释时才经共同接口提问并等待专家；否则保留未决范围和缺口，不猜补成员。
- CLI 可用 `--range "1--5|main.tex|10|10|candidates:objects=HVS"` 声明待专家核对的对象前缀解释；Python 使用 `range_notations[].candidates`。这些候选不构成已确认成员，决定通过 `ReviewService.submit_decision` 保存，再用 `materials resume` 接续，决定只应用一次；正式裁决 CLI／网页尚未提供。
- 退出码：0 成功；1 工程错误；2 参数错误；3 暂停等待决定；4 无可用材料；5 已完成幂等重入（prepare 同请求同输入的重放与 resume 的已完成重入相同，均返回 5）。准备完成但存在缺口仍是成功，缺口在 `--json` 输出中如实呈现。

## 模型配置与调用

`hvs_catalog.llm` 提供 `CallRequest`、`ModelConfig`、`RuntimeConfig`、`start_call`／`resume_call`／`get_call`。当前接入 Token Dance 的 `deepseek-v4.1-flash`、`glm-5.3-flash`、`mimo-v2.6-flash` 和 `qwen3.8-flash`，固定各自官方上游，关闭回退。真实提取步骤尚未接入；`models probe` 只执行英文合成对象 `SYN-1`、数值 `42` 的连通验证，不证明提取质量。

模型列表给出默认值、各模型允许的参数及官方依据；配置检查不联网：

```bash
hvs-catalog models list
hvs-catalog models check --config model.json
```

非敏感配置使用 JSON。下例使用 DeepSeek 的默认参数；提取默认配置及逐步模型选择见 [提取配置操作](#提取配置与提示预览)：

```json
{
  "model": {"model": "deepseek-v4.1-flash", "stream": true},
  "runtime": {
    "base_url": "https://tokendance.space/gateway/v1",
    "credential_env": "TOKENDANCE_API_KEY",
    "quota_group": "tokendance-account",
    "account_rpm": 50000,
    "rpm": 50000,
    "account_concurrency": 100,
    "concurrency": 100,
    "connect_timeout": 10,
    "idle_timeout": 180,
    "total_timeout": 600,
    "max_attempts": 3
  }
}
```

省略 `parameters` 使用该模型完整默认配置，包括最大输出上限。显式提供 `parameters` 时，字典即完整发送参数，**不合并默认值**；`{}` 表示全部省略。修改默认配置时可从 `models list` 的 `default_parameters` 复制后调整，例如 DeepSeek 的 `{"max_tokens":4096,"thinking_type":"enabled","reasoning_effort":"high"}`。支持的字段、类型、取值范围、互斥规则分别校验，不接受任意 `extra_body`；嵌套思考字段由适配器转换。实际发送、省略、转换及公开默认依据都进入固定快照，未报告的服务端实际值保留未知。各模型的输出与思考计数方式由 `models list` 的 `output_accounting` 给出。

API Key 由环境变量或显式 `--env-file` 指定文件读取，环境变量优先；文件不执行 shell。使用者可建立自己的本机 `.env`，只需 `TOKENDANCE_API_KEY=...`，保持 Git 忽略，不随包发行，也不将密钥写入 JSON。网关地址来自 JSON／运行配置；`.env` 中其他变量不自动改变配置。

同账户本机进程和不同工作空间须使用相同 `quota_store` 与 `quota_group`；相对路径在首次加载时按当前工作目录固定为绝对路径。默认额度数据库为 `~/.local/state/hvs-catalog/tokendance.sqlite3`，默认 50000 RPM／100 并发，四模型共用；其他账户按实际额度配置，目标值不得超过账户值。共享组的限流参数不一致时拒绝调用；要调整上限，应先停止旧进程并等待其租约及滚动窗口失效，再让全部进程使用新的共享额度数据库。同空间写入仍串行，100 并发通过不同空间实现。流式请求直到完成或关闭才释放名额；进程退出后的租约覆盖总时限并留 30 秒余量，不立即释放未知请求的名额。

以下真实调用会使用账户额度。参数中的 `--max-output 4096` 只缩小本次验证的输出上限，思考设置沿用模型配置；省略该选项使用生产最大上限：

```bash
hvs-catalog models probe --config model.json --space MODEL_SPACE \
    --request-key probe-1 --env-file .env --max-output 4096
hvs-catalog models status --space MODEL_SPACE --execution EXECUTION_ID
hvs-catalog models resume --space MODEL_SPACE --execution EXECUTION_ID --env-file .env
```

也可用 `--model MODEL_ID` 代替 `--config`；`--plain --non-stream` 验证普通非流式输出。`--quota-store PATH` 可显式指定共享额度数据库。CLI 返回执行 ID、状态、尝试、原始响应引用、解析结果、服务请求标识和已知 Token 用量。产品不填报金额；开发验证的预算和账单独立管理，具体范围及结果见 [计划](../.dev/plan.md#验证与实验安排) 和 [进度](../.dev/progress.md)。

恢复使用保存的输入和配置，已保存响应不再次调用。出现 `completion_unknown` 时，尝试保留 `pending`，运行报告失败并阻止自动重发；须人工核对后才另建请求。已确定失败也不由恢复自动重试，修正输入／配置后使用新 `request-key`。只有可确认的发送前连接失败和 429 会在首次运行内自动重试，最多三次，每次共享门限并遵循 `Retry-After`；鉴权、参数、空响应、截断和格式错误直接报告，不更换提示、模型或协议。完整空间备份仍按工作空间约定进行，额度数据库单独属于本机账户限流状态。

当前接口不依赖 pi-ai。扩展供应商的可信适配器职责见 [模型配置设计](design/extraction.md#模型接口与运行配置)；适配器对象显式传入 `start_call` 和 `resume_call`，配置不会动态加载代码。

## 提取配置与提示预览

默认配置选择 `direct`、`deepseek-v4.1-flash`、流式和该模型已定义的默认参数，并开启专家求助。以下操作离线检查与组装提示，不调用模型：

```bash
hvs-catalog extract config > extraction.json
hvs-catalog extract check --config extraction.json
hvs-catalog extract preview --config extraction.json --step direct_roster --input step-input.json > request-preview.json
```

配置文件可只填需要设置的选项。例如保持默认 DeepSeek，候选判断和数值分别指定 GLM 与 MiMo：

```json
{
  "method": "candidate_review",
  "step_models": {
    "review_candidates": {"model": "glm-5.3-flash"},
    "quantities": {"model": "mimo-v2.6-flash"}
  },
  "examples": {"quantities": ["E13", "E14", "E16", "E19"]}
}
```

`step_models` 中每项是完整模型配置，`parameters` 的省略与显式字典规则同 [模型配置](#模型配置与调用)，不混入另一模型的参数。`use_model_supplements: false` 关闭模型补充提示，科学规则与填写要求照常保留；当前补充提示没有额外文字。`examples` 按步骤指定编号，空列表关闭该步例子；未指定使用英文步骤资源的默认选择，选例不代替科学规则。

`step-input.json` 提供 `manuscript` 字符串，包含准备好的原文块、表格及其文件／位置标识。例如名单预览的最小合成输入：

```json
{"manuscript": "FILE paper\n1|Synthetic A was selected as a Galactic unbound candidate."}
```

`targets` 提供本步必要的已有候选或名单对象；逐项 review 恰好一个带 `item_id` 的候选，数值步骤提供带 `object_id` 的名单对象，每项保留 `identifiers` 名称列表。`material_gaps` 是缺口列表，每项提供 `gap_id`、英文 `description` 和共同定义的 `affected_targets`，可带 `reason` 与 `file_id`。`open_questions` 每项提供 `question_id`、英文 `prompt`，可带 `category`、`target_ids`、`gap_ids` 和原文就地证据 `evidence`；不传记录修订或维护信息。原文、标识、数值和证据不翻译。完整预览同时输出 `request`（模型、两条消息和当前步骤工具 Schema）、`prepared`（模型适配器转换的实际协议参数）、内容摘要、选例及判例归档信息。步骤不存在、收尾配置模型、无关／重复例子、缺少必需英文内容或已有结果时拒绝，不生成模型调用。

日常 `mode: daily` 开启专家裁决。后续纯 AI 对照配置显式指定 `mode: evaluation` 与 `expert_adjudication: false`；`synthetic` 可用来模拟两种裁决配置。提示只改变歧义处理要求，仍保留未决、缺口和科学范围。可显式传入带审定状态、版本、英文说明、条件及边界的 `precedents` 快照，必须声明 `approved: true`；省略或未审定时拒绝。当前只核对所提供的输入，不代替科学审批，实际判例制作、审批、查找与库复用由 R21 接入。

以下命令使用固定的虚构论文与模拟服务，验证配置接入各步、请求归档及恢复，不代表真实论文提取：

```bash
hvs-catalog extract simulate --config extraction.json --space /tmp/hvs-config-demo --paper FIC-R05-B --request-key configured-example
hvs-catalog extract status --space /tmp/hvs-config-demo --execution exec-000001
hvs-catalog extract resume --space /tmp/hvs-config-demo --execution exec-000001
```

模拟执行可在科学疑问处暂停，决定提交仍用现有 Python 裁决接口。恢复只读取原运行的配置、提示、选例与判例内容；编辑配置文件或提示资源不会改写原运行。新材料、实际英文提示或配置使用新的 `request-key`。归档损坏或缺失时拒绝恢复，不能用当前提示替换。`status` 可回查完整配置和提示／例子引用，以及每步调用次数和已知 tokens；模拟服务用量是固定合成值。日常与评测通过同一个 Python 配置／提取入口，实际模型提取由后续方法实现接入。

## 持久化与恢复演示

`hvs-catalog demo` 使用共享 `pipeline`、`workspace`、`runtime`、`review`，全程 synthetic-mock。请指定全新独立目录 `SPACE`，不接管日常空间或旧演示，不提供 reset 删除捷径。

空间保存共同记录、固定输入和 LangGraph 检查点；同级 `SPACE.calls.jsonl` 是外部模拟服务的追加调用事实，不进入客户端事务或备份。必须保留它才能跨进程核对调用，不要当作客户端缓存清理。

虚构论文 FIC-R05-A 报告 FIC-A-1 的径向速度 520 ± 10 km/s；作者在 M1／M2 下判断相反，未给总体结论。固定模拟专家决定记为“无总体结论”，保留原值、条件和证据，不进行物理推算或对象合并。

```bash
hvs-catalog demo run --space SPACE
hvs-catalog demo status --space SPACE
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion  # Replay the same decision
hvs-catalog demo decide --space SPACE --choice keep_pending          # Conflicting existing decision: exit code 2
```

第一步在名单→数值后等待共享裁决，进程退出；后续命令在新进程查看／恢复。正常暂停到完成累计两次模拟调用，恢复复用已存结果／响应，同决定只保存应用一次。在另一个新目录可直接选择 `keep_pending`：答复已保存但仍未决，流程继续暂停。

状态中的 `config`／`input_digest` 表示固定输入，`steps`／`result_refs` 表示成果，`pending` 是问题与证据，`external_calls` 是独立服务累计事实，不等于客户端成功保存的尝试数。

### 响应尚未保存时终止进程

另用新目录验证死亡窗口：

```bash
hvs-catalog demo run --space CRASH_SPACE --crash-after-response  # Exit code 70
hvs-catalog demo status --space CRASH_SPACE                    # One external call occurred
hvs-catalog demo run --space CRASH_SPACE                       # Three total calls; paused
hvs-catalog demo decide --space CRASH_SPACE --choice no_overall_conclusion
```

外部调用与本地保存不是同一事务，未保存窗口恢复后可能再调用；真实服务可能重复计费，本演示不模拟金额。恢复沿用原材料与固定输入，缺失或错版即拒绝。旧 `merge`／`keep-separate`、`reset`、独立图／存储接口已退役，旧空间及账本保持原样，另选目录开始。

## 合成提取与重评

输出目录须在客户端空间外：

```bash
hvs-catalog methods list --json
hvs-catalog extract demo --space EXTRACT_SPACE --paper FIC-R05-A \
    --method direct --adjudication on --output EXTRACT_OUTPUT
hvs-catalog score demo --inputs EXTRACT_OUTPUT/evaluation-inputs.json
```

`extract demo` 还支持 `candidate_review`、专家开／关、B／C 合成材料和 `--decision keep_pending` 的未决输出。`score demo` 可在新进程反复重评，输入改动后拒绝按旧指纹评分。Gold 与人工评分预期只由评分侧使用，不进入提取请求。

科学完成与运行图终止分别报告；未知用量和专家用时为 `null`，不合成质量总分。模拟评分辅助仅验证协议，不是正式评分模型能力验证。完整就绪／隔离要求见 [评分集成](design/evaluation.md#提取与评分的集成入口)。

## Gold 检查、保存与人工恢复

评分前已确认的范围及问题通过评分侧 Python 接口固定：`GoldReadinessReview` 与 `gold_readiness_fingerprint(pair)` 绑定当前完整配对，把每个已裁定缺口的代码、位置、处理和依据交给 `gold_entries_from_compact_pair(pair, scoring_review=review)`。该入口不重写 Gold、不核验真实 PDF，也不授权保存；完整材料核对及具体专家决定须先完成。字段和处理边界见 [Gold 评分就绪](design/evaluation.md#gold-适配与就绪门禁)。

`ScoringPlan.limitations` 接收已审定的 `EvaluationLimitation`，用 `LimitationScope` 明确论文内对象标识、可选物理量、条件和科学归属。默认评分保留 PDF/源码差异和作者问题；包外及既无 Gold 又不提供模型输入的解析范围可显式设置 `exclude_from_default`。已有 PDF Gold 的范围不能以不支持解析为由排除默认分。PDF/源码不对齐的范围设置 `exclude_from_reference` 后，`score_batch` 和保存输入集的重评入口同时返回原默认结果与 `reference_score`，后者同步排除两侧对应条目并重算。`limitation_effects` 保存证据、受影响记录及观察到的错误，是潜在原因清单；条件或身份归属不明时受影响指标保持暂定。问题清单进入完整计划指纹，修改后不能按旧指纹重评。未提供清单的已有合成输入及其指纹保持原样。

当前格式、Python／CLI 参数、内容绑定批准和中断后的完整人工恢复步骤由 [生成 Gold 说明](../contracts/generated/gold/README.md#current-save-and-manual-recovery) 维护。工作副本、材料根和单篇正式目标须分别指定；工具不执行 Git，也不自动接管旧逐版目标。科学审定、保存授权及评价快照见 [Gold 维护约定](design/evaluation.md#gold-的局部修正与-git-历史)。
