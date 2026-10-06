# hvs-catalog

从高速星文献中整理对象、作者判断和测量值，保留原文依据，建设可回查来源与修订的星表。

项目仍在开发；当前实现、已知限制和验收状态见 [Progress](.dev/progress.md)。首版计划提供 GitHub Pages 星表浏览、完整数据与 SQL 下载，以及供 Agent 本地分析的 Skill；具体安排见 [Plan](.dev/plan.md#任务目录)。必要字段与下载用法随数据交付，当前命令用法先查对应 CLI 的 `--help`。

## 安装

需要 Python 3.11 或更高版本和 uv。在仓库构建，安装到独立环境：

```bash
uv build
uv venv /path/to/env
uv pip install --python /path/to/env dist/hvs_catalog-*.whl
```

在仓库外运行 `/path/to/env/bin/hvs-catalog --help`，不设置 `PYTHONPATH`。依赖见 [pyproject.toml](pyproject.toml) 与锁文件，开发检查见 [验证要求](.dev/plan.md#验证要求)。

## 开发入口

- [AGENTS](AGENTS.md)：模型阅读路由。
- [Plan](.dev/plan.md)：目标、开发规则、任务与验收条件。
- [Progress](.dev/progress.md)：当前进展、验证、问题与接续。

科学规则和格式说明从源定义生成，入口见 Plan 的[任务读取表](.dev/plan.md#按任务读取)。
