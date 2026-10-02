# hvs-catalog Agent Entry

## Progressive Reading

1. **Start here:** read [current capabilities](README.md#当前可用能力), [shared scientific boundaries](docs/design.md#产品目标与科学边界), and the maintenance sections on [development conventions](CONTRIBUTING.md#开发工作约定), [validation](CONTRIBUTING.md#验证要求), and [Git authorization](CONTRIBUTING.md#git-操作). Do not read these files in full by default.
2. **Locate the task:** use the development-stage entry below to check the current handoff, Git status, and scope, then follow the [task reading table](docs/design.md#按任务阅读) to the necessary sections. For field changes, inspect the relevant generated model/step contract; for existing operations, use [operations](docs/operations.md); for documentation maintenance, use the [responsibility table](CONTRIBUTING.md#文档维护与防止漂移).
3. **Read dependencies as needed:** expand reading only when crossing module, science, material, or authorization boundaries. Links are not instructions to recursively read everything. Report missing materials, conflicting rules, or unclear authorization; never assume review passed or substitute old reports for current requirements.

Create a separate task directory under `.work/` only when work materials need persistent storage. Ordinary questions, read-only checks, and small edits needing no retained materials require no directory or report. Continue the same task in its existing space; follow [workspace conventions](CONTRIBUTING.md#工作空间创建使用与清理) at completion and archive only materials with continuing value. Do not read `.work/archive/` without a specific traceability need. Historical materials are not current instructions or authorization.

## Development Execution

The current primary Agent owns requirement alignment, planning, implementation, self-checks, and delivery. Human experts approve requirements and scientific meaning and accept results. See the [development approach](.dev/plan.md#开发方案) for tools and responsibilities.

Arrange independent technical review or multiple Agents only when human experts explicitly request it. The primary Agent may recommend them but must not initiate them autonomously. Label self-checks honestly, without claiming independent review. Shared conventions still govern human acceptance, scientific approval, data authorization, and Git authorization.

## Development-Stage Entry

The project is being rebuilt from scratch. Read the current handoff and task table in [.dev/progress.md](.dev/progress.md), then locate shared responsibilities, the general handoff format, and the current gate in [.dev/plan.md](.dev/plan.md). For maintenance outside a gate, read only applicable responsibilities and handoff requirements, without unrelated gates. Read dependencies, reference-repository limits, and experiment constraints according to impact; verify actual files and Git status. Do not read the whole plan by default. See [reading and compression conventions](CONTRIBUTING.md#开发计划的按需阅读与压缩).

Follow [gate implementation and handoff](.dev/plan.md#逐关实施与交接) for execution and handoff.

See [documentation maintenance](CONTRIBUTING.md#文档维护与防止漂移) for conditions allowing removal of this development-stage entry.
