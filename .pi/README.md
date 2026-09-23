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

主会话为 Astra。Sol 的 high 是本项目的工程选择，不声称官方为本项目指定了档位。这里不新增执行插件、CLI、定时器或自制调度引擎；主会话用已安装的 pi-subagents 逐步交接，Pi 负责子进程、会话、结果与 mission 持久记录。

**轮次限制是主会话必须遵守的操作协议，不是不可绕过的程序硬限制。** 不宣称崩溃后会自动重启任务、无人值守运行或恰好执行一次。以后若模拟任务证明原生能力不足，再讨论必要的补充，而不提前开发平台。

## 加载与影响范围

- 对照的安装版本为 Pi **0.87.1**、pi-subagents **0.50.0**。升级后重新检查调用和续接行为。
- 项目配置需要信任；本机仅此仓库的信任记录位于 `~/.pi/agent/trust.json`。换机器应自行确认，不信任整个父目录或 Gold 仓。
- 修改后执行 `/reload` 或重启。设置加载检查不代表当前聊天已热切换模型；通过 `/model` 核实主模型和 thinking。
- 项目过滤全局通用 skills 和 prompts，仅保留 pi-subagents skill 与 `/hvs-work`；保留查资料和子代理插件，在本项目禁用 pi-btw、Plannotator。没有修改全局 settings、卸载包或删除全局技能。
- `autoload: false` 的项目包条目只调整已安装包的资源，不代为安装。`disableBuiltins` 禁用内置代理；子代理空 `extensions` 禁用常规扩展，插件必需的内部协议工具仍由插件加载。
- 子代理限制不是操作系统沙箱，尤其 bash 仍能写文件。只读审核、Gold 隔离和 Git 授权同时依赖角色纪律与主会话核查。

## 压缩与重试

自动压缩启用，`reserveTokens=32768`、`keepRecentTokens=20000`。阈值为 `contextTokens > contextWindow - reserveTokens`；有效窗口以模型 registry 为准，而不是只看模型宣传值。项目信任、设置加载与实际模型配置都必须生效。压缩使用当前模型，也可能因额度失败。

Pi 的瞬时错误重试最多 2 次，provider 层重试为 0；工作流层不额外自动重试、切换模型或等待定时重启。失败后保存现场并通知用户。原始会话 JSONL、运行材料和任务计数不能由压缩摘要代替；清理运行材料前先确认不再需要恢复。

## 主会话操作协议

### 1. 固定任务与持久交接

确认符合入口条件后，先读当前进度，核对关卡、授权、Git 基线；执行 `subagent({action:"list"})` 确认两名代理可运行。本入口不授权恢复暂停任务、读取 Gold 或替代科学审批。

新工程任务用 `mission.create` 创建一项原生 mission，不启用 goal 定时提醒。把目标、范围、非目标、验收条件、验证方式、授权和基线写入固定任务说明，保存在 `.work/pi-delivery/<taskId>/contract.md`，并在 mission objective 中关联。taskId 只用字母、数字、连字符或下划线。正式 progress 只记状态、missionId 和恢复入口，不复制整份运行日志。

同一任务始终使用同一 missionId、同一父会话，串行调度。同一 mission 的原生 `state` 中，键 `delivery` 保存一份简短运行记录：

- 固定的任务说明路径、父会话路径；外层 `cycle` 和内层 `round`，取值须满足 [计划的轮次上限](../.dev/plan.md#逐关实施与交接)。
- 当前阶段：`ready_implementation`、`running_implementation`、`ready_review`、`running_review`、`awaiting_alignment`、`paused` 或 `awaiting_human`。
- 实现者和审核者各自最新 runId、会话／报告路径；上一轮审核结论与第二周期纠偏说明。
- `pending`：本次角色、周期、轮次、唯一 workflow key、旧 resumeId、已知启动回执和异常线索。没有未决步骤时为 null。

通过附同一 missionId 的短 `workflowScript` 使用 `state.get("delivery")` / `state.set("delivery", record)` 读写；它是运行记账，不是新的自动状态机。初始化前必须确认不存在记录，不能重新初始化以重置预算。保持记录简短，完整报告只记路径，整个原生 state 文件有 256 KiB 上限。

### 2. 明确启动与完成的区别

**任何启动前先保存 pending；保存失败就不启动。** 已有 pending 必须先核对，不重放同一调用。首次实现与首次审核都显式 fresh，且要在 **`runs.run` 的子项** 写 `async:true`，不能只把外层 workflow 设为 async。示意（占位值必须替换为本次已核实内容）：

```js
// 通过 subagent 提交，外层同时传入同一 missionId、async:true。
return runs.run("c1-r1-impl", {
  agent: "hvs-implementer", task: "本次批准的任务与必要依据",
  context: "fresh", async: true,
  output: "/已核实的仓库绝对路径/.work/pi-delivery/task/c1-r1-impl.md",
  share: false
});
```

初审对应 `hvs-reviewer`，附原始需求、实现报告、实际改动依据和 `review.schema.json` 路径；**不传 outputSchema，也不传 acceptance 覆盖**。审核者将严格审核 JSON 与独立的原生 `acceptance-report` 块放在同一最终文字回复中，运行时剥除回执并保存完整审核 JSON 到指定的绝对 `review.json` 路径，同时保存 attestation 元数据。主会话读取整份 JSON，用 schema 校验格式，再核对结论、实际命令和依据。

实现者的原生验收只检查 checked evidence，角色明确关闭重复的内部 review gate；Sol 的独立审核仍必需，由主会话另行执行和关联，不能从 checked 推断技术通过。审核者的原生 attested 也只证明提供了回执，不代表其技术 verdict 为 pass。

后台启动返回后立即记下**子 runId**、外层 workflow ID 和实际 artifact 路径。外层 workflow 完成或工具返回成功，仅可能代表已成功发起子任务，**不代表子任务完成**。等待插件完成通知，不 sleep 或反复查询等候；需要在当前轮交回最终结果时，才用 `subagent_wait` 等具体运行。

收到完成通知后，核对精确子 runId 的状态与终止证据，再读取完整报告、实际文件和必要测试结果；状态摘要或 `runs.status` 的成功返回不等于完整报告。只认真实保存的完整审核 JSON 和原生验收元数据，不从启动消息、日志片段或总结语猜测 verdict。找不到报告、结果被截断、没有验证依据、状态或结构不明时，保存所有已知线索并暂停。

完成核对后才清空 pending、记录结果并推进下一阶段；不能边实现边审核同一工作目录。报告使用仓库 `.work` 下的绝对路径，避免全局输出基目录改写相对路径；续接时以插件返回的实际路径为准，不能假定沿用新路径或会话一直存在。续接可能覆盖原报告，先保存之前的报告快照，再核对本次产物。

### 3. 审核、续接与两层上限

轮次上限及外层纠偏条件以 [计划](../.dev/plan.md#逐关实施与交接) 为准。正常实现完成后自动交 Sol 审核，无需用户每轮点击。主会话检查完整报告是否为合法 JSON 并符合 schema，不能把未知严重度降成 optional，也不能遗漏必填字段后自行补空。原生验收若 rejected、证据缺失或格式错误，应暂停核对，而非仅凭报告里的 pass 放行。

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

静态配置检查包括 JSON 可解析、`git diff --check`、实际资源加载和 `list/get` 的模型、角色与工具配置。查看有效 compaction/retry，不仅查看 JSON 文件；静态检查不代替真实调用与恢复验证。

0.50.0 的后台结构化工具输出与最终文字验收通道分离，混用会导致审核 JSON 有效但普通报告为空、attestation 被拒绝。因此本配置使用上述单一文字通道；不修改全局插件，也不关闭审核者的原生回执检查。旧会话会保留旧输出契约，配置变化后应在获准的新测试中 fresh 验证，不能假设 resume 会应用新契约。实际复测结果见 progress。

合成试运行限定到 `.work` 的临时工程目录、不触碰业务或 Gold；新增测试范围另行确认。完整检查清单包括：首次 fresh、首次 pass、一次必修返工、两角色最新 ID 续接、第二周期 fresh、两层上限、缺失报告不放行、故障暂停、父会话重启后的状态核对。先从最小启动／完成／续接链路测试，不直接跑满所有轮次，不人为消耗真实额度来制造限额。未实际验证的项目必须保持“未验证”。
