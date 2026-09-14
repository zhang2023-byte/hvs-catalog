# 项目开发进度

本文记录实际进度和接手者需要的信息，任务要求见 [整体计划](plan.md)。暂停或交接前更新，接手时核对实际文件与 Git 状态。填写要求见 [逐关实施与交接](plan.md#逐关实施与交接)。

## 当前交接

更新日期：2026-09-14。

- **任务与范围：** [R01](plan.md#r01) 最小工程骨架已由 Zcode / GLM 实施，Codex / Astra 完成技术验收，结论通过；人类专家于 2026-09-14 批准 R01 验收。涉及包定义、锁文件、CLI、测试、发行检查工具和 README；Astra 完成技术验收，并按人类专家要求补充 CONTRIBUTING 的提交信息约定。
- **已完成：** 包可独立安装，软件版本由 `pyproject.toml` 定义并从已安装元数据读取；CLI 支持帮助、版本及 `python -m hvs_catalog`，无运行时第三方依赖。已检查源码、测试、构建配置与 R01 约定一致。实际 wheel 为 3 个源码文件及 4 个元数据文件，源码包内容检查通过；README 安装步骤及三个已测解释器版本与实际结果一致。
- **验证结果：** Astra 在 `/tmp/hvs-r01-astra-review/` 使用独立缓存和全新开发环境，先执行 `uv sync --locked --python 3.12` 安装锁定依赖（准备阶段联网下载）。随后设置 `UV_CACHE_DIR=/tmp/hvs-r01-astra-review/cache`、`UV_PROJECT_ENVIRONMENT=/tmp/hvs-r01-astra-review/venv`、`UV_OFFLINE=1`，执行 `uv run --frozen ruff check .`、`uv run --frozen ruff format --check .`、`uv run --frozen pytest -q`，均通过，测试为 16 passed。相同环境下执行 `uv run --frozen python tools/check_distribution.py`，分别传入 `3.11`、`3.12`、`3.13`，均通过。另构建 wheel 并在三个仓库外全新环境安装，以最小环境变量及 `python -I` 实际导入 `hvs_catalog`，断言文件位于该环境的 site-packages，版本及控制台命令均正常。实测为 macOS arm64 的 CPython 3.11.15、3.12.13、3.13.1；Linux/Windows 未实测。原默认 uv 缓存受本次沙箱写权限限制，使用临时缓存解决；`git diff --check` 通过。
- **待解决：** 无阻塞 R01 的代码问题。已核实仓库原 `.venv` 的 `_virtualenv.pth` 带 hidden 标志，且本机 CPython 3.12 的 `site.py` 会跳过 hidden `.pth`；本轮仓库外全新 editable 环境可正常运行。设置标志的来源及发生范围未知，不能断言由后台进程导致或其他机器不受影响。GLM 报告的 `UV_NO_EDITABLE=1` 可作为环境排障备选，本轮验收未依赖该设置。非阻塞测试改进：现有名为导入检查的用例实际只查询元数据，建议后续直接导入包并核对路径；本轮已独立补验，CLI 与模块入口测试也覆盖了包导入。
- **接续安排：** 人类专家已授权 R01 本地 Git 提交，明确暂不推送。本地提交已完成；按人类专家要求修改提交信息，并将提交信息约定纳入 CONTRIBUTING。远程交付暂缓，获准完成本关交付后由 Zcode / GLM 进入 R02。
- **Git 与授权：** R00 已由人类专家确认通过，先前已核实文档交付。当前 R01 交付包括包定义、锁文件、源码、测试及工具、README、CONTRIBUTING 与本文件。人类专家已批准 R01 验收，并明确授权本地提交、暂不推送；本次另获准修改当前提交信息，采用简洁英语描述具体改动，去掉关卡编号；通用约定写入 CONTRIBUTING 并随本次修订保存，提交结果以 Git 状态为准。R02 未开始。

## 任务总表

每关一行。“通过”表示人类专家验收通过，验收日期据实填写；提交与推送以实际 Git 状态为准。

| 任务 | 验收状态 | 验收日期 | 关键结果或未解决事项 |
|---|---|---|---|
| [R00](plan.md#r00) | 通过 | 2026-09-14 | 文档、整体计划、忽略规则及流程修订经人类专家确认通过；两仓文档已交付。 |
| [R01](plan.md#r01) | 通过 | 2026-09-14 | Codex / Astra 技术验收通过：16 项测试及三个 Python 版本的发行安装、隔离导入验证通过；人类专家已批准并授权本地提交，推送暂缓。 |
| [R02](plan.md#r02) | 未开始 | — | — |
| [R03](plan.md#r03) | 未开始 | — | — |
| [R04](plan.md#r04) | 未开始 | — | — |
| [R05](plan.md#r05) | 未开始 | — | — |
| [R06](plan.md#r06) | 未开始 | — | — |
| [R07](plan.md#r07) | 未开始 | — | — |
| [R08](plan.md#r08) | 未开始 | — | — |
| [R09](plan.md#r09) | 未开始 | — | — |
| [R10](plan.md#r10) | 未开始 | — | — |
| [R11](plan.md#r11) | 未开始 | — | — |
| [R12](plan.md#r12) | 未开始 | — | — |
| [R13](plan.md#r13) | 未开始 | — | — |
| [R14](plan.md#r14) | 未开始 | — | — |
| [R15](plan.md#r15) | 未开始 | — | — |
| [R16](plan.md#r16) | 未开始 | — | — |
| [R17](plan.md#r17) | 未开始 | — | — |
| [R18](plan.md#r18) | 未开始 | — | — |
| [R19](plan.md#r19) | 未开始 | — | — |
| [R20](plan.md#r20) | 未开始 | — | — |
| [R21](plan.md#r21) | 未开始 | — | — |
| [R22](plan.md#r22) | 未开始 | — | — |
| [R23](plan.md#r23) | 未开始 | — | — |
| [R24](plan.md#r24) | 未开始 | — | — |
| [R25](plan.md#r25) | 未开始 | — | — |
| [R26](plan.md#r26) | 未开始 | — | — |
| [R27](plan.md#r27) | 未开始 | — | — |
| [R28](plan.md#r28) | 未开始 | — | — |
| [R29](plan.md#r29) | 未开始 | — | — |
| [R30](plan.md#r30) | 未开始 | — | — |
