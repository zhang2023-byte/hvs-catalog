# Pi 开发协作

本目录只配置开发工具，不改变 hvs-catalog 的产品架构或运行依赖。角色与授权正文见 [.dev/plan.md](../.dev/plan.md#逐关实施与交接)。

启用前按 [Agent 入口](../AGENTS.md#执行环境与任务分流) 核实执行环境与任务分工。本文只维护操作协议；实际验证结果和未验证范围见 [当前交接](../.dev/progress.md#验证结果)。

## 保留什么

| 文件 | 职责 |
|---|---|
| `settings.json` | 项目模型、资源加载、压缩、有限重试与子代理策略 |
| `agents/hvs-implementer.md` | GLM 5.3 / max，实现与自检 |
| `agents/hvs-reviewer.md` | GPT-6 Sol / high，独立只读审核 |
| `review.schema.json` | 主会话校验完整审核 JSON 报告的格式，不作为子调用的 outputSchema |
| `prompts/hvs-work.md` | 可选的 `/hvs-work` 入口；自然语言提出任务也可以 |
| `pi-host.mjs` | 自检的唯一 Pi SDK／公开工具接口适配边界，不是生产调度器 |
| `check.mjs`、`check.test.mjs` | 离线能力检查、审核报告校验及拒绝条件测试，不调用模型 |

主会话为 Astra。Sol 的 high 是本项目的工程选择，不声称官方为本项目指定了档位。这里不新增执行插件、CLI、定时器或自制调度引擎；主会话用已安装的 pi-subagents 逐步交接，Pi 负责子进程、会话、结果与 mission 持久记录。

**轮次限制是主会话必须遵守的操作协议，不是不可绕过的程序硬限制。** 不宣称崩溃后会自动重启任务、无人值守运行或恰好执行一次。以后若模拟任务证明原生能力不足，再讨论必要的补充，而不提前开发平台。

## 加载与影响范围

- 安装版本仅在包声明／锁文件固定，实际通过的版本和证据记在 progress；工作流不按版本号分支。升级不等于兼容，按下文自检和合成复测后才能用于正式任务。
- 项目配置需要信任；本机仅此仓库的信任记录位于 `~/.pi/agent/trust.json`。换机器应自行确认，不信任整个父目录或 Gold 仓。
- 仅修改配置后可 `/reload`；替换 npm 包后应重启 Pi，避免旧进程缓存与磁盘新文件混用。新进程自检不代表当前聊天已热切换；通过 `/model` 核实主模型和 thinking。
- 项目过滤全局通用 skills 和 prompts，仅保留 pi-subagents skill 与 `/hvs-work`；保留查资料和子代理插件，在本项目禁用 pi-btw、Plannotator。这些资源过滤只作用于本项目，不卸载包或删除全局技能；升级包版本时用户级安装声明会同步更新。
- `autoload: false` 的项目包条目只调整已安装包的资源，不代为安装。`disableBuiltins` 禁用内置代理；子代理空 `extensions` 禁用常规扩展，插件必需的内部协议工具仍由插件加载。
- 子代理限制不是操作系统沙箱，尤其 bash 仍能写文件。只读审核、Gold 隔离和 Git 授权同时依赖角色纪律与主会话核查。

## 与包版本解耦

分清三个边界：

1. **项目契约**：角色职责、两层轮次上限、单写入者、科学／Git 授权、完整审核 JSON 和故障暂停，由计划、角色正文及 `review.schema.json` 维护；不随插件默认策略改变。
2. **插件适配**：只用公开 `subagent` 工具及当前安装版的 guide。角色 frontmatter 只保留需要的显式策略，不携带历史空字段。先发现能力再调用；插件工具若按需加载，先用其 loader 激活。等待工具名以实际注册为准。新增 package skills/prompts 不自动进入项目，使用明确允许名单。
3. **环境与证据**：包版本固定用于部署保护，不是工作流条件。`pi-host.mjs` 通过包公开入口、Pi 导出的 SDK 和注册工具调用做自检；不导入 `pi-subagents/src/*`，不依赖 `.ts`／`.js` 布局、终端文案或固定临时目录。工具返回的新增字段可忽略，所需能力／字段缺失则报错暂停，不能删掉检查来冒充兼容。

公开接口也可能变化，无法保证任意未来升级零适配。升级时只修改必要的安装配置、适配边界和能力检查，不复制插件源码、不补丁全局包、不建立另一套执行引擎。先备份用户／项目设置与 npm 锁文件；一次明确目标版本升级，重启后离线检查，再以合成任务验证后台启动、审核输出、退出证明和两角色续接。失败保留现场，修复后同协议复测或经同意回退；不自动换模型或降低验收要求。`--ignore-scripts` 可用于工程安装；未运行的原生安装脚本及禁用插件的未测功能须明确保留为限制。

## 压缩与重试

自动压缩启用，`reserveTokens=32768`、`keepRecentTokens=20000`。阈值为 `contextTokens > contextWindow - reserveTokens`；有效窗口以模型 registry 为准，而不是只看模型宣传值。项目信任、设置加载与实际模型配置都必须生效。压缩使用当前模型，也可能因额度失败。

Pi 的瞬时错误重试最多 2 次，provider 层重试为 0；工作流层不额外自动重试、切换模型或等待定时重启。失败后保存现场并通知用户。原始会话 JSONL、运行材料和任务计数不能由压缩摘要代替；清理运行材料前先确认不再需要恢复。

## 主会话操作协议

### 1. 固定任务与持久交接

确认符合入口条件后，先读当前进度，核对关卡、授权、Git 基线。配置或包变化后先运行 `PI_OFFLINE=1 node .pi/check.mjs`；在当前已重启的聊天中激活插件工具后，执行 `subagent({action:"list", agentScope:"project", capabilities:true})` 确认两名代理可运行及模型／权限正确。本入口不授权恢复暂停任务、读取 Gold 或替代科学审批。

新工程任务用 `mission.create` 创建一项原生 mission，不启用 goal 定时提醒。把目标、范围、非目标、验收条件、验证方式、授权和基线写入固定任务说明，保存在 `.work/pi-delivery/<taskId>/contract.md`，并在 mission objective 中关联。taskId 只用字母、数字、连字符或下划线。正式 progress 只记状态、missionId 和恢复入口，不复制整份运行日志。

同一任务始终使用同一 missionId、同一父会话，串行调度。同一 mission 的原生 `state` 中，键 `delivery` 保存一份简短运行记录：

- 固定的任务说明路径、父会话路径；外层 `cycle` 和内层 `round`，取值须满足 [计划的轮次上限](../.dev/plan.md#逐关实施与交接)。
- 当前阶段：`ready_implementation`、`running_implementation`、`ready_review`、`running_review`、`awaiting_alignment`、`paused` 或 `awaiting_human`。
- 实现者和审核者各自最新 runId、会话／报告路径；上一轮审核结论与第二周期纠偏说明。
- `pending`：本次角色、周期、轮次、唯一 workflow key、旧 resumeId、已知启动回执和异常线索。没有未决步骤时为 null。

通过附同一 missionId 的短 `workflowScript` 使用 `state.get("delivery")` / `state.set("delivery", record)` 读写；它是运行记账，不是新的自动状态机。初始化前必须确认不存在记录，不能重新初始化以重置预算。保持记录简短，完整报告只记路径，整个原生 state 文件有 256 KiB 上限。

### 2. 明确启动与完成的区别

**任何启动前先保存 pending；保存失败就不启动。** 已有 pending 必须先核对，不重放同一调用。首次实现与首次审核都显式 fresh；外层 workflow 设 `async:true`，子项省略 `async`，让后台子任务完成后才返回结果。子项显式 `async:true` 则仅返回启动回执，不得当作交付；本项目默认不用这种提前分离形式。示意（占位值必须替换为本次已核实内容）：

```js
// 通过 subagent 提交，外层同时传入同一 missionId、async:true。
return runs.run("c1-r1-impl", {
  agent: "hvs-implementer", label: "Implement approved task", task: "本次批准的任务与必要依据",
  context: "fresh",
  output: "/已核实的仓库绝对路径/.work/pi-delivery/task/c1-r1-impl.md",
  share: false
});
```

初审对应 `hvs-reviewer`，附原始需求、实现报告、实际改动依据和 `review.schema.json` 路径；**不传 outputSchema，也不传 acceptance 覆盖**。审核者将严格审核 JSON 与独立的原生 `acceptance-report` 块放在同一最终文字回复中，运行时剥除回执并保存完整审核 JSON 到指定的绝对 `review.json` 路径，同时保存 attestation 元数据。主会话从实际 `outputReference` 读取整份 JSON，用 schema 校验格式，再核对结论、实际命令和依据。`runs.run(...).output` 是展示文本，可能在 JSON 后附保存路径提示，不能直接 `JSON.parse`，也不能猜测并裁掉提示来代替读取落盘文件。

实现者的原生验收只检查 checked evidence，角色明确关闭重复的内部 review gate；Sol 的独立审核仍必需，由主会话另行执行和关联，不能从 checked 推断技术通过。审核者在角色配置中显式要求 `acceptance: attested`，不依赖只读角色的插件默认值；原生 attested 只证明提供了回执，不代表其技术 verdict 为 pass。

后台启动返回后立即记下外层 workflow ID、已知子 runId 和实际 artifact 路径；子 ID 尚未分配时如实记 pending，收到结果后补全。工具返回成功不等于子任务完成；外层完成也必须核对每个子任务终态。等待插件完成通知，不 sleep 或反复查询等候；headless 测试或必须在当前轮交回结果时，用当前插件公开的等待工具等精确运行，不将某个等待工具名称写成业务规则。

等待工具可能因监督询问或超时提前返回，并不代表完成。出现询问时通过当前父会话的原生监督通道核对、回复；保持宿主和运行现场，不因等待返回就销毁 SDK 会话或启动替代任务。

收到完成通知后，核对精确子 runId、成功状态及公开的进程退出证明（当前 `processTerminalProof`／`workflowTerminalProof` 必须为 `observed`），再读取完整报告、实际文件和必要测试结果；时间戳、PID 消失、状态摘要或 `runs.status` 成功不能代替这些证据。使用结果给出的 `outputReference`／artifact 路径，不猜临时文件位置。只认真实保存的完整审核 JSON 和原生验收元数据，不从启动消息、日志片段或总结语猜测 verdict。找不到报告、结果被截断、没有验证依据、状态或结构不明时，保存所有已知线索并暂停。

完成核对后才清空 pending、记录结果并推进下一阶段；不能边实现边审核同一工作目录。报告使用仓库 `.work` 下的绝对路径，避免全局输出基目录改写相对路径；续接时以插件返回的实际路径为准，不能假定沿用新路径或会话一直存在。续接可能覆盖原报告，先保存之前的报告快照，再核对本次产物。

### 3. 审核、续接与两层上限

轮次上限及外层纠偏条件以 [计划](../.dev/plan.md#逐关实施与交接) 为准。正常实现完成后自动交 Sol 审核，无需用户每轮点击。主会话用 `node .pi/check.mjs --review /绝对路径/review.json` 校验完整报告的 JSON、schema 和 verdict 一致性；此命令只校验报告，不代替原生验收、退出证明或真实证据核查。不能把未知严重度降成 optional，也不能遗漏必填字段后自行补空。原生验收若 rejected、证据缺失或格式错误，应暂停核对，而非仅凭报告里的 pass 放行。

- `pass` 必须没有 must_fix 且提供实际验证依据；否则不放行。自相矛盾的结论暂停核对。
- `changes_required` 必须指出范围内具体必修项。有剩余内层轮次才递增 `round` 并交回实现；耗尽则 `awaiting_human`，不启动外层纠偏。
- `blocked` 直接等待人类或必要设计决定，可选优化不触发无限返工。

每次修复或复审前，实际执行 `children.list`。仅续接其中 **resumable 的精确 runId**，并确认角色、最新会话和旧进程已结束。分别保留 GLM 与 Sol 自己的上下文，不交叉续接、不 fork 父会话。用同一 mission 的 `runs.run(key, {resume: runId, task: followUp})`；不同时传 agent、model 或新工具契约。插件文档说明这种调用等待续接结果，而直接 `action:"resume"` 返回后台回执；仍以实际完成证据为准。每次续接可能得到新 runId，及时替换该角色的最新 ID。

Sol 技术 pass 后，Astra 对照原始需求检查实际成果：对齐则 `awaiting_human`；符合计划的外层纠偏条件时，先保存纠偏说明、递增 `cycle` 并将 `round` 设为 1，**两个角色均重新 fresh**，携带原始任务与必要证据而非旧聊天。没有剩余交付次数时停止。技术 pass 或需求对齐均不授权 Git、Gold 正式保存或关卡放行。

### 4. 故障与重启

额度、服务、压缩或进程错误后立即停止自动交接，保留阶段、cycle/round、pending、全部已知 runId／路径、改动和失败信息。向用户说明卡在哪一步以及剩余轮次；不能自动 fallback、重新创建 mission 或清掉预算。

重启后先恢复原父会话，再从 `mission.show`、`state.get("delivery")`、具体 run 的 status 和实际文件交叉核对。若启动回执未写入，查 mission 的 workflow key 和运行材料；找不到结果不等于没有执行过。核对原进程已结束、没有同目录写入者，再决定：

- 已完成且报告有效：登记既有结果，不重新执行。
- 失败且同角色最新会话确实 resumable：经用户明确允许，续接失败步骤，保留原轮次和其他角色结果。
- 仍在运行、结果不确定、会话丢失、进程终止证据不足或只能 fresh：暂停说明，等待决定。不得静默重开；人工批准的替代会话须标为 fallback，仍保留原预算。

原生 state 不提供整个“写 pending → 启动 → 收结果”的事务，也不自动执行这些规则。这是选择小范围原生方案的明确限制。

## 测试与自检

执行 `PI_OFFLINE=1 node .pi/check.mjs`、`PI_OFFLINE=1 node --test .pi/check.test.mjs` 和 `git diff --check`。自检须在父 Pi 环境运行，不在受限子代理内运行或清除其安全环境变量；子代理可运行 `check.test.mjs` 并审阅父进程保存的检查报告。自检检查实际资源加载、公开 `list/get/children.list/validate`、模型／工具／显式验收配置及有效 compaction/retry，不启动子代理或调用模型；可加 `--output .work/路径/offline.json` 保存版本、输入哈希与检查记录；重检开始先撤销旧通过结果，失败留下 failed，使用证据前还须核对时间、安装版本和输入哈希，不能复用陈旧报告。npm 安装的 Pi 默认从 `npm root -g` 定位；特殊 SDK 布局显式设置 `HVS_PI_PACKAGE_ROOT` 为 Pi 包目录。检查器只在当前进程设置插件文档支持的宿主路径提示，不写全局环境。

静态检查不代替真实调用与恢复验证。审核采用完整 JSON 加原生回执的单一文字通道，避免并行输出通道的交付歧义；不修改全局插件，也不关闭审核者的原生回执检查。旧会话保留旧输出契约，配置变化后应在获准的新测试中 fresh 验证，不能假设 resume 会应用新契约。实际复测结果见 progress。旧 `.work/check-pi-config.mjs` 是历史本机检查，不再作为升级入口。

合成试运行限定到 `.work` 的临时工程目录、不触碰业务或 Gold；新增测试范围另行确认。完整检查清单包括：首次 fresh、首次 pass、一次必修返工、两角色最新 ID 续接、第二周期 fresh、两层上限、缺失报告不放行、故障暂停、父会话重启后的状态核对。先从最小启动／完成／续接链路测试，不直接跑满所有轮次，不人为消耗真实额度来制造限额。未实际验证的项目必须保持“未验证”。
