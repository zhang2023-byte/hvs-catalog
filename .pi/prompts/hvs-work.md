---
description: 仅供 Pi 执行开发计划明确要求协作的任务；其他 Agent 不适用
argument-hint: "<HVS 工程任务或已有 missionId>"
---

任务：$@

先按 AGENTS.md 的“执行环境与任务分流”确认本任务是否适用；调用本提示不扩大范围或授权。

适用时读取 `.dev/progress.md`、`.dev/plan.md` 的当前任务与“逐关实施与交接”，再按 `.pi/README.md` 的“主会话操作协议”执行。新任务核对授权后创建 mission；已有任务先核对原 mission 与未决运行，不重新初始化计数或自动恢复暂停任务。

轮次边界由计划维护，启动、审核输出、续接与故障处理由 Pi README 维护；不要依据本提示另建一套调度规则。
