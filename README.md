# hvs-catalog

从高速星研究文献中整理对象、论文提出的判断和测量值，记录原文出处，识别不同论文中指向同一天体的记录，形成可以回查依据的星表。

计划支持本机文献处理、专家裁决、星表审核和获准发布。使用流程与社区修订方式见产品设计。

## 当前可用能力

自 R01 起提供可独立安装的最小 Python 包 `hvs-catalog`：安装后可用 `hvs-catalog --help` 查询命令行帮助、`hvs-catalog --version` 查询软件版本，也支持 `python -m hvs_catalog` 调用。文献处理、星表数据和正式发布功能尚未提供，命令行只列出已实现的操作。

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

开发环境同样使用 uv 与锁文件：`uv sync` 安装依赖后，用 `uv run ruff check .`、`uv run ruff format --check .` 和 `uv run pytest` 检查；`uv run python tools/check_distribution.py` 自动完成构建、发行内容检查、仓库外安装和独立子进程验证，可传入 Python 版本号（如 `3.12`）指定安装环境的解释器。

实测平台：macOS（Apple Silicon，darwin 25.6.0）上的 CPython 3.11.15、3.12.13 和 3.13.1。安装包为纯 Python wheel，无运行时第三方依赖；其他平台未实测。

## 阅读入口

- [产品设计](docs/design.md)：科学目标、系统各部分负责什么，以及两仓和运行工作空间怎样存放文件。
- [开发与文档维护](CONTRIBUTING.md)：怎样开发、检查和执行 Git 操作，以及怎样保持文档与实际工作一致。
- [Agent 入口](AGENTS.md)：执行仓库任务前的阅读要求。

主仓计划管理代码与正式星表。用于评价提取结果的参考答案称为 Gold，由独立的 `hvs-catalog-gold` 私有仓库管理。各类资料可以放在哪里、程序和数据如何提供下载，见产品设计。
