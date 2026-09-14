# 项目开发进度

本文记录实际进度和当前交接。填写与接续方式见 [逐关实施与交接](plan.md#逐关实施与交接)。

## 当前交接

更新日期：2026-09-14。

### 任务与范围

R01 后的文档职责整理，由 Codex / Astra 执行。范围为主仓 README、CONTRIBUTING、AGENTS、design、plan、progress，以及 Gold 仓 README、AGENTS。

### 已完成

通用规范、产品设计、关卡要求和交接事实各归其位；重复正文改为章节引用。已将专家确认的科学规则维护方式写入设计与 R04：结构化材料作为唯一来源，程序读取并生成阅读版。具体格式和子目录仍由 R04 确定。

R01 已交付的产品能力见 [README](../README.md)，验收状态见任务总表。

### 验证结果

本次执行临时检查脚本 `python3 /tmp/check-hvs-docs.py`，8 份文档的 124 处本地文件与章节链接通过检查，其中 12 处跨仓链接在当前相邻目录布局下可解析；未验证网页托管后的跨仓链接。31 个关卡及进度行顺序一致，开始条件、验收状态与日期未变。两仓 `git diff --check` 通过，改动仅涉及上述八份文档。

逐项核对规则迁移：R01–R30 中 28 个验收段落原文保留；R04 补入已确认的规则阅读版生成要求，R18 改为引用完整评价设计，原指标、复验和范围限制仍有对应正文。持久的文档自动检查在 R27 接入，本次未增加运行代码或检查工具。

R01 已有验收记录（本次未重跑代码验证）：Astra 在 `/tmp/hvs-r01-astra-review/` 使用全新开发环境，先以 `uv sync --locked --python 3.12` 联网安装锁定依赖，再设置以下环境执行离线检查：

```bash
export UV_CACHE_DIR=/tmp/hvs-r01-astra-review/cache
export UV_PROJECT_ENVIRONMENT=/tmp/hvs-r01-astra-review/venv
export UV_OFFLINE=1
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv run --frozen python tools/check_distribution.py 3.11
uv run --frozen python tools/check_distribution.py 3.12
uv run --frozen python tools/check_distribution.py 3.13
```

上述检查通过，测试为 16 passed。wheel 为 3 个源码文件及 4 个元数据文件，源码包检查通过。另在三个仓库外全新环境安装 wheel，以最小环境变量及 `python -I` 验证实际导入路径位于 site-packages，版本及控制台命令正常。实测为 macOS arm64 的 CPython 3.11.15、3.12.13、3.13.1，Linux/Windows 未测；README 安装说明与结果一致，`git diff --check` 通过。默认 uv 缓存曾受沙箱写权限限制，使用上述临时缓存解决。

### 待解决

本次文档整理无待解决问题。

R01 无阻塞代码问题。原仓库 `.venv` 的 `_virtualenv.pth` 带 hidden 标志，本机 CPython 3.12 的 `site.py` 会跳过 hidden `.pth`；仓库外全新 editable 环境可正常运行。标志来源及影响范围未知。GLM 报告的 `UV_NO_EDITABLE=1` 可作排障备选，已有验收未依赖它。现有导入检查用例实际只查询元数据，后续可改为直接导入并核对路径；R01 已独立补验，CLI 与模块入口测试也覆盖导入。

### 接续安排

R01 远程交付仍暂缓，获准完成后由 Zcode / GLM 进入 R02。

### Git 与授权

人类专家已确认科学规则的唯一维护来源与阅读版生成方式，并授权在两仓本地提交本批八份文档。提交结果以实际 Git 状态为准，推送未获授权。

此前 R01 已获准本地提交及修改提交信息，操作已完成，推送暂缓。提交信息约定已写入 CONTRIBUTING。核对实际 Git 状态后接续，不把此前授权用于本次文档修改。

## 任务总表

| 任务 | 验收状态 | 验收日期 | 关键结果或未解决事项 |
|---|---|---|---|
| [R00](plan.md#r00) | 通过 | 2026-09-14 | 两仓基础文档、整体计划与忽略规则完成验收。 |
| [R01](plan.md#r01) | 通过 | 2026-09-14 | 最小工程骨架完成验收；独立安装与 CLI 可用。 |
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
