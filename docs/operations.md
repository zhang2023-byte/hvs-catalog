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
hvs-catalog acquisition import --space ./ws --file paper.pdf \
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
| 4 | 保留给历史不支持形态；新PDF完整性错误返回1 |
| 5 | PDF不可用，保留待处理；不表示科学上无关 |
| 6 | 无可用材料 |
| 7 | 修订检查未全部核实，状态为 unknown；不表示没有新版 |

网络临时失败有限重试，拒绝访问时停止。共享限流出现未确认在途状态时须人工协调恢复，CLI 不提供重置入口；状态版本不兼容时拒绝。已提交原包可在重试时复用，响应／文件尚未提交的中断仍可能造成重复请求。获取与网络等待不在长期数据库事务内。

页级溯源保存完整解析视图（含摘要等返回元数据），不是原始 Atom 线路响应。获取进度、页溯源和保留的原包均随空间备份恢复；HTTP 传输关闭底层自动重试／重定向，每一跳经过共享节流并有界读取。

### PDF导入与可选源码存档

默认`fetch`只下载PDF。手动加`--latex`额外保存固定版本源码字节、来源和哈希；不解包、不交给模型，其失败通过`latex_error`单独报告，不使有效PDF失败。

```bash
hvs-catalog acquisition fetch --space ./ws --id 2401.01234v2 --latex
hvs-catalog acquisition import-pdf --space ./ws --file journal.pdf \
    --paper-key my-paper --source-version published-2026 \
    --provenance "从期刊网页下载，固定本地原件" --doi 10.example/example
```

期刊导入返回固定的`source_ref`，准备时使用它；无需构造arXiv编号。相同论文键、来源版本及内容可复用，内容冲突拒绝覆盖。支持未加密、结构可读、至多256 MiB／2000页的PDF；HTML、损坏和加密文件拒绝。结构检查不证明科学内容或解析准确。

## 材料准备：正文与表格

使用本地MinerU 4解析固定PDF的全部页面。先在独立环境安装工具并下载模型；本地解析不会向解析服务上传论文，也不会在缺模型时自动切换服务；后续科学提取按配置将文字和图片发送给模型API。安装方式和硬件要求以 [MinerU官方说明](https://opendatalab.github.io/MinerU/quick_start/) 为准。例如在项目环境之外准备解析器：

```bash
uv venv --python 3.12 /path/to/mineru-env
uv pip install --python /path/to/mineru-env/bin/python 'mineru==4.0.10'
export HVS_MINERU_COMMAND=/path/to/mineru-env/bin/mineru-kit
export MINERU_HOME=/path/to/mineru-data
export MINERU_MODEL_SMALL_BACKEND=onnx
export MINERU_MODEL_VLM_ENGINE=llama-cpp
"$HVS_MINERU_COMMAND" models download --tier standard
"$HVS_MINERU_COMMAND" models verify --tier standard
```

advanced与standard共用官方standard资源集；上述下载与校验命令因此仍使用standard。解析固定为`mineru-kit parse INPUT.pdf --tier advanced --pages all`，完整选项由适配器配置。当前固定安装版本为4.0.10。

本地模型和解析器资源由操作者管理，未包含在项目wheel或业务空间备份中。适配器固定advanced、ONNX、llama-cpp及全部页面，并关闭可选远程LLM后处理；实测macOS Apple Silicon，其他平台未实测。准备与消费命令：

```bash
hvs-catalog materials prepare --space ./ws --paper 2401.01234 --version 2 --request-key pdf-example
hvs-catalog materials prepare --space ./ws --source-ref SOURCE_ID@1 --request-key journal-example
hvs-catalog materials status --space ./ws --execution PREPARATION_ID --json
hvs-catalog materials consume --space ./ws --execution PREPARATION_ID --json
hvs-catalog materials resume --space ./ws --execution PREPARATION_ID --json
```

保存原PDF、MinerU ZIP、原始Markdown、页／块索引、图片文件、版本配置及已知缺口。模型接收正文、表格文字、作者图注以及原始图片附件；MinerU自行生成的图片／图形描述不进入模型材料，原始解析产物仍完整保留。图片和表格截图按解析器提供的裁剪保存，使用内嵌base64图片传入API，本地文件链接不代替图片内容。当前图片请求接入DeepSeek Flash，其他模型在未配置图片能力时明确拒绝，不静默丢图；DeepSeek Flash的图片传输和图片证据保存已通过真实调用验证，具体读图内容仍须对照原PDF核对。

名单模型以位置编号和原文提交证据，程序补齐PDF页／块；文字证据匹配引文；图片证据只验证PDF位置及图片文件完整性，模型读到的文字不算通过字符串核验。引用匹配不能证明解析忠实或科学归属正确。图片裁剪、表格、符号和误差仍可能遗漏或失真，原PDF是最终依据。`materials_complete`只表示没有程序已知的缺口；`consume`不认证科学准确性。旧准备结果须用新请求重新准备后用于新提取，已有实验记录保持不变。

`prepare`冻结来源和解析器指纹，同请求同输入复用；配置或代码变化使用新请求。`resume`沿用原执行，版本不符明确拒绝，不替换固定输入。准备接口不接收Gold，不创建科学裁决任务。缺页或不可读页随成果记录；解析失败保存错误，保持可查询的失败执行。

退出码：0成功；1工程错误；2参数错误；4无可用材料；5已完成幂等重入。准备完成但有缺口时返回0，须检查JSON中的缺口，不能仅凭退出码判断完整。

## 模型配置与调用

`hvs_catalog.llm` 提供 `CallRequest`、`ModelConfig`、`RuntimeConfig`、`start_call`／`resume_call`／`get_call`。当前接入 Token Dance 的 `deepseek-v4.1-flash`、`glm-5.3-flash`、`mimo-v2.6-flash` 和 `qwen3.8-flash`，固定各自官方上游，关闭回退。[名单提取](#名单提取) 已使用此接口；`models probe` 只执行英文合成对象 `SYN-1`、数值 `42` 的连通验证，不证明提取质量。

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

省略 `parameters` 使用该模型完整默认配置，包括最大输出上限。显式提供 `parameters` 时，字典即完整发送参数，**不合并默认值**；`{}` 表示全部省略。修改默认配置时可从 `models list` 的 `default_parameters` 复制后调整，例如将 DeepSeek 推理改为 high，同时保留服务商最大输出上限：`{"max_tokens":393216,"thinking_type":"enabled","reasoning_effort":"high"}`。DeepSeek 的 `max_tokens` 同时包含推理和最终答案；提高 `reasoning_effort` 不会自动扩大显式设置的输出上限，耗尽上限仍报告截断。支持的字段、类型、取值范围、互斥规则分别校验，不接受任意 `extra_body`；嵌套思考字段由适配器转换。实际发送、省略、转换及公开默认依据都进入固定快照，未报告的服务端实际值保留未知。各模型的输出与思考计数方式由 `models list` 的 `output_accounting` 给出。

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

默认配置选择 `deepseek-v4.1-flash`、流式和该模型已定义的默认参数（推理深度 `max`），并开启专家求助。以下操作离线检查与组装提示，不调用模型：

```bash
hvs-catalog extract config > extraction.json
hvs-catalog extract check --config extraction.json
hvs-catalog extract preview --config extraction.json --step roster --input step-input.json > request-preview.json
```

配置文件可只填需要设置的选项。例如名单使用支持图片输入的DeepSeek Flash，数值提示指定MiMo（数值真实执行尚未提供）：

```json
{
  "step_models": {
    "roster": {"model": "deepseek-v4.1-flash"},
    "quantities": {"model": "mimo-v2.6-flash"}
  },
  "examples": {"quantities": ["E13", "E14", "E16", "E19"]}
}
```

`step_models` 中每项是完整模型配置，`parameters` 的省略与显式字典规则同 [模型配置](#模型配置与调用)，不混入另一模型的参数。`use_model_supplements: false` 关闭模型补充提示，科学规则与填写要求照常保留；当前补充提示没有额外文字。`examples` 按步骤指定编号，空列表关闭该步例子；未指定使用英文步骤资源的默认选择，选例不代替科学规则。

`step-input.json` 提供 `manuscript` 字符串，包含准备好的原文块、表格及其文件／位置标识。例如名单预览的最小合成输入：

```json
{"manuscript": "FILE raw\nPAGE 1 BLOCK p1-b0\nSynthetic A was selected as a Galactic unbound candidate."}
```

`targets` 提供本步必要的名单对象；数值步骤提供带 `object_id` 的名单对象，每项保留 `identifiers` 名称列表。`material_gaps` 是缺口列表，每项提供 `gap_id`、英文 `description` 和共同定义的 `affected_targets`，可带 `reason` 与 `file_id`。`open_questions` 每项提供 `question_id`、英文 `prompt`，可带 `category`、`target_ids`、`gap_ids` 和原文就地证据 `evidence`；不传记录修订或维护信息。原文、标识、数值和证据不翻译。完整预览同时输出 `request`（模型、两条消息和当前步骤工具 Schema）、`prepared`（模型适配器转换的实际协议参数）、内容摘要、选例及判例归档信息。步骤不存在、收尾配置模型、无关／重复例子、缺少必需英文内容或已有结果时拒绝，不生成模型调用。

日常 `mode: daily` 开启专家裁决。纯AI配置显式指定 `mode: evaluation` 与 `expert_adjudication: false`；`synthetic` 可用来模拟两种裁决配置。两种配置共用科学正文，仅切换歧义策略；全自动配置要求模型自行解释，不创建专家待办。真实材料缺口和原文冲突仍须保留。可显式传入带审定状态、版本、英文说明、条件及边界的 `precedents` 快照，必须声明 `approved: true`；省略或未审定时拒绝。当前只核对所提供的输入，不代替科学审批；判例制作、审批、查找与库复用尚未提供。

以下命令使用固定的虚构论文与模拟服务，验证配置接入各步、请求归档及恢复，不代表真实论文提取：

```bash
hvs-catalog extract simulate --config extraction.json --space /tmp/hvs-config-demo --paper FIC-R05-B --request-key configured-example
hvs-catalog extract status --space /tmp/hvs-config-demo --execution exec-000001
hvs-catalog extract resume --space /tmp/hvs-config-demo --execution exec-000001
```

模拟执行可在科学疑问处暂停，决定提交仍用现有 Python 裁决接口。恢复只读取原运行的配置、提示、选例与判例内容；编辑配置文件或提示资源不会改写原运行。新材料、实际英文提示或配置使用新的 `request-key`。归档损坏或缺失时拒绝恢复，不能用当前提示替换。`status` 可回查完整配置和提示／例子引用，以及每步调用次数和已知 tokens；模拟服务用量是固定合成值。日常与评测通过同一个 Python 配置／提取入口，实际名单操作见下一节。

## 名单提取

先按 [材料准备](#材料准备正文与表格) 获得已完成的准备执行编号，再在同一空间执行：

```bash
hvs-catalog extract roster --space SPACE --preparation PREPARATION_ID --request-key roster-example --config extraction.json --env-file /path/to/.env
hvs-catalog extract status --space SPACE --execution EXTRACTION_ID
hvs-catalog extract resume --space SPACE --execution EXTRACTION_ID --env-file /path/to/.env
```

`roster` 创建并运行名单步骤，返回提取执行编号；这会调用真实 Token Dance 模型，凭据及参数规则见 [模型调用](#模型配置与调用)。省略 `--config` 使用专家辅助与DeepSeek Flash。Python 对应 `start_roster_extraction(space, preparation_execution_id=..., request_key=..., configuration=...)`，再用共同的 `resume_extraction`、`get_extraction`；`CallService` 可指定凭据文件。

输入固定为准备完成的来源修订、PDF页／块文本及原始图片、已知缺口、科学规则、提示及配置；恢复校验附件指纹。模型收到正文、表格文字、作者图注和base64图片附件，解析器生成的读图描述不进入输入，不直接发送PDF二进制。当前图片请求只为DeepSeek Flash配置；选择其他模型时明确拒绝，不静默丢弃图片。明确编号范围由模型理解并逐个提交对象，多个成员可以引用同一原范围证据；程序不展开成员。填写结构只允许引用输入中已登记的材料缺口，已知缺口不得凭模型回复抹去。

输出保存名单、贡献、作者判断、条件、排除项、未列清群体及PDF证据；18种物理量保持`not_checked`。专家辅助版先保存本轮有效结果与全部问题，结束调用后暂停；无专家答复时部分结果仍可查询、备份，恢复不会空转调用模型。通过 [共同Python裁决接口](design/review.md#裁决共同接口方案) 保存决定后恢复相关处理。全自动版将解释保存为`model_judgments`，不创建专家问题、不等待专家；真实材料缺口仍可使结果partial。运行结束不等于科学提取完整或正确。

格式纠正最多两轮，原响应和每次请求均保存。每个补正请求保留完整补正历史：原始指令、论文文字和图片只放一次，之后按轮次追加此前的模型提交与校验反馈。恢复时复用实际已发送的请求历史。首次JSON无法解析时返回具体行列位置并要求重交；一旦可解析，改用`repair_roster`，只接收程序指出的缺失／无效字段或引用，其他内容由程序原样合并保留，不要求重交名单。已有可解析且格式有效的科学判断、对象及收录内容不得在纠正中改变；已在群体中明确列名却缺少对应条目的成员可补齐，不能添加其他对象或重写既有判断。引文定位、摘录及无效缺口编号可对照同一材料修正，证据须对应输入中的文件、位置与原文，不能编造。截断、调用失败、无有效工具提交或无效证据不能保存为成功空名单。已归档响应恢复时复用；发出后完成状态不明时拒绝重发，须检查记录后另建请求，可能有已发生而未确认的服务计费。实际科学覆盖与准确率由后续真实评价检验，小范围接通检查不构成质量结论。

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
    --adjudication on --output EXTRACT_OUTPUT
hvs-catalog score demo --inputs EXTRACT_OUTPUT/evaluation-inputs.json
```

`extract demo` 支持专家开／关、B／C 合成材料和 `--decision keep_pending` 的未决输出。`score demo` 可在新进程反复重评，输入改动后拒绝按旧指纹评分。Gold 与人工评分预期只由评分侧使用，不进入提取请求。

科学完成与运行图终止分别报告；未知用量和专家用时为 `null`，不合成质量总分。模拟评分辅助仅验证协议，不是正式评分模型能力验证。完整就绪／隔离要求见 [评分集成](design/evaluation.md#提取与评分的集成入口)。

## Gold 检查、保存与人工恢复

评分前已确认的范围及问题通过评分侧 Python 接口固定：`GoldReadinessReview` 与 `gold_readiness_fingerprint(pair)` 绑定当前完整配对，把每个已裁定缺口的代码、位置、处理和依据交给 `gold_entries_from_compact_pair(pair, scoring_review=review)`。该入口不重写 Gold、不核验真实 PDF，也不授权保存；完整材料核对及具体专家决定须先完成。字段和处理边界见 [Gold 评分就绪](design/evaluation.md#gold-适配与就绪门禁)。

`ScoringPlan.limitations`接收已审定的范围声明，用`LimitationScope`限定对象、物理量、条件和科学归属。评分只输出一套L0/L1/L2结果及逐论文、逐字段的结构化差异；固定PDF范围内的解析遗漏、失败和未完成结果照常计分。事先确定的范围外材料可设置`exclude_from_default`，已有PDF Gold不能因解析器不支持而被排除。作者矛盾及原有范围记录保留；`limitation_effects`仅关联已登记事项和观察到的差异，不判断错误根因。无法确定身份或条件是否属于排除范围时，保留预测及计数，受影响指标暂定。范围声明进入计划指纹，变更后须生成新输入集。

当前格式、Python／CLI 参数、内容绑定批准和中断后的完整人工恢复步骤由 [生成 Gold 说明](../contracts/generated/gold/README.md#current-save-and-manual-recovery) 维护。工作副本、材料根和单篇正式目标须分别指定；工具不执行 Git，也不自动接管旧逐版目标。科学审定、保存授权及评价快照见 [Gold 维护约定](design/evaluation.md#gold-的局部修正与-git-历史)。
