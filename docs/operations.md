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

## 持久化与恢复演示

`hvs-catalog demo` 使用共享 `pipeline`、`workspace`、`runtime`、`review`，全程 synthetic-mock。请指定全新独立目录 `SPACE`，不接管日常空间或旧演示，不提供 reset 删除捷径。

空间保存共同记录、固定输入和 LangGraph 检查点；同级 `SPACE.calls.jsonl` 是外部模拟服务的追加调用事实，不进入客户端事务或备份。必须保留它才能跨进程核对调用，不要当作客户端缓存清理。

虚构论文 FIC-R05-A 报告 FIC-A-1 的径向速度 520 ± 10 km/s；作者在 M1／M2 下判断相反，未给总体结论。固定模拟专家决定记为“无总体结论”，保留原值、条件和证据，不进行物理推算或对象合并。

```bash
hvs-catalog demo run --space SPACE
hvs-catalog demo status --space SPACE
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion
hvs-catalog demo decide --space SPACE --choice no_overall_conclusion  # 同决定回放
hvs-catalog demo decide --space SPACE --choice keep_pending          # 已有不同决定，退出码 2
```

第一步在名单→数值后等待共享裁决，进程退出；后续命令在新进程查看／恢复。正常暂停到完成累计两次模拟调用，恢复复用已存结果／响应，同决定只保存应用一次。在另一个新目录可直接选择 `keep_pending`：答复已保存但仍未决，流程继续暂停。

状态中的 `config`／`input_digest` 表示固定输入，`steps`／`result_refs` 表示成果，`pending` 是问题与证据，`external_calls` 是独立服务累计事实，不等于客户端成功保存的尝试数。

### 响应尚未保存时终止进程

另用新目录验证死亡窗口：

```bash
hvs-catalog demo run --space CRASH_SPACE --crash-after-response  # 退出码 70
hvs-catalog demo status --space CRASH_SPACE                    # 外部已调用一次
hvs-catalog demo run --space CRASH_SPACE                       # 累计三次，暂停
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

当前格式、Python／CLI 参数、内容绑定批准和中断后的完整人工恢复步骤由 [生成 Gold 说明](../contracts/generated/gold/README.md#current-save-and-manual-recovery) 维护。工作副本、材料根和单篇正式目标须分别指定；工具不执行 Git，也不自动接管旧逐版目标。科学审定、保存授权及评价快照见 [Gold 维护约定](design/evaluation.md#gold-的局部修正与-git-历史)。
