# 扩展体系设计（Extension Design）

> 状态：**阶段 0–5 已实现（阶段 4 为可选的 best-effort 沙箱）**。
> 实现代码位于 `zero_agent/extensions/`，测试见 `tests/test_extension_runtime.py`
> 与 `tests/test_mcp_client.py`。`core/hooks.py` 已删除，生命周期事件统一走
> `ExtensionRuntime.notify()`。
> 参考：pi（earendil-works/pi）的轻量扩展入口、DeepSeek Harness 的注册归属与生命周期。
> 原则：本方案只描述“要建成什么样”，落地时以本文的验收项为准，不以估算代码量作为承诺。

## 一、目的与范围

### 目标

1. 用户能把外部 skill 目录原样放进来使用，不改写格式、不新增清单文件。
2. 插件能改变 agent 的整体行为与方式（提示词、模型请求、工具调用与结果）。
3. SOP 保持“agent 可修订的经验”，skill / MCP 保持“只读来源”，两者不混为一谈。
4. agent 修改插件、skill、MCP 配置时必须获得人类同意。
5. 核心执行链路（循环、工具分发、完成判定）保持现有形态，不重写。

### 非目标

- 不把 ZeroAgent 改造成 DSH 那样的全面服务化/Cordis 插件树。
- 不引入 `CAPABILITY.toml` 一类的自造清单文件。
- 不在首版支持插件热重载与多租户隔离。
- 不承诺“某个护栏绝对不可绕过”（原因见第九节）。

## 二、现状与约束

以下是本设计必须尊重的既有事实，均可在代码中核对。

| 组件 | 现状 | 影响 |
| --- | --- | --- |
| `core/hooks.py` | **已删除**；生命周期事件统一由 `ExtensionRuntime.notify()` 分发 | 不再存在两套事件体系 |
| `core/handler.py` `_notify` | 直接调用运行时；通知回调失败由运行时记录并继续 | 插件异常不会中断工具执行 |
| `core/handler.py` `dispatch` | `tool_before` 在门控前通知；`tool_after` 统一由 `_emit_tool_after` 触发 | 通知只读；修改只能走门控/结果变换 |
| `core/loop.py` | 触发 `agent_start / turn_start / llm_before / llm_after / turn_end / agent_end`；负载含 handler | 内部插件通过同一运行时订阅 |
| `core/agent.py` | 内部插件（langfuse / project_mode / worldline）与外部插件都经 `setup(ctx)` 安装 | 唯一扩展入口 |
| `core/agent.py` `_build_prompt_assembly` | 块序列 base / reply_language / date / memory / task_control / backend_extra / peer_hint / capabilities；外部块追加在末尾 | 插件可贡献提示词块 |
| `tools/registry.py` `register` | 同名覆盖前记录快照，支持按归属回滚 | 插件替换内置工具后可恢复 |
| `core/types.py` `PendingTaskState` | `waiting_kind` 增加 `approval` | 支持待批准操作 |
| `memory/manager.py` | SOP 是 L3；提示词只注入结构说明与 L1 索引，正文按需 `file_read` | SOP 已经是渐进式加载 |
| MCP | `extensions/builtin/mcp_client.py` 已实现 stdio | HTTP 传输未实现 |

### 已具备的基础

- `ToolRegistry` + `ToolDefinition` 是统一工具声明格式（含 `evidence_kind` 与 `effects`）。
- `ExtensionRuntime` 提供通知、变换、门控三类扩展点，以及注册归属与卸载。
- `StepOutcome` / `StepAction` / `TerminalEvent` 已能表达“等待用户”。
- 记忆系统已经区分“可写经验（SOP）”与“只读上下文”。

## 三、评审结论摘要

设计过程中经历多轮修正，以下结论已并入本方案：

| 结论 | 处理 |
| --- | --- |
| 插件统一入口、外部 skill 原样复用 | 采纳 |
| “只升级 `trigger()` 就能获得全部扩展能力” | 驳回。调用方必须消费改写结果，并处理阻断与等待 |
| 所有事件统一成 waterfall | 驳回。通知、变换、门控的返回值与失败语义不同，不能合并 |
| 权限做成内置插件 | 采纳，但前提是运行时提供审批与恢复能力 |
| “旧插件零改动、零回归” | 驳回。共享可变对象与注册顺序仍可能带来行为差异，需逐项验证 |
| 照搬 DSH 的一切皆插件 | 首阶段不采用，仅借用注册归属与生命周期思路 |
| 文件写护栏不可绕过 | 驳回，改为分层说明（第九节） |

## 四、设计原则

1. **一个执行内核，一个插件运行时。** 扩展能力集中在一个模块里，不散落到各调用方。
2. **接口要深。** 插件作者只学一个 `setup(ctx)`；加载、顺序、冲突、卸载、清理都藏在运行时内部。
3. **语义显式。** 每类扩展点的返回值、异常与失败处理都写进接口契约，不依赖共享字典的副作用。
4. **可撤销。** 每次注册都有归属，插件卸载时注册随之撤销（来自 DSH 的 reversible effects）。
5. **失效不扩大。** 插件出错只影响它自己注册的能力，不能拖垮循环。

## 五、目标架构

```text
zero_agent/
├── core/                       # 执行内核：本方案不重写
│   ├── loop.py                 # 循环（新增：消费变换后的请求/工具调用）
│   ├── handler.py              # 分发（新增：门控与结果变换）
│   ├── agent.py                # 组装（引导插件运行时；提示词补充块）
│   └── interfaces.py           # LLM/Tool 协议
│
├── extensions/                 # 新增：插件运行时（深模块，唯一扩展入口）
│   ├── runtime.py              # 加载、注册归属、顺序、卸载、异常隔离
│   ├── context.py              # 面向插件的稳定 API 对象
│   ├── events.py               # 三类扩展点的契约定义
│   └── builtin/                # 内置扩展（它们和第三方用同一套接口）
│       ├── permission_gate.py  # 写入需人类同意的策略
│       ├── skills_index.py     # skill 发现与描述注入
│       ├── mcp_client.py       # MCP 工具来源
│       └── browser_tools.py    # 可选浏览器工具包（extensions.browser 控制）
│
└── tools/                      # 工具仍注册进现有 ToolRegistry
```

外部扩展位置：

```text
~/.zeroagent/
├── skills/                     # 目录 + SKILL.md，与 ~/.dsh/skills 同构，可软链
├── mcp.json                    # 单个文件，mcpServers 键
└── extensions/                 # 单文件 <name>.py 或目录 <name>/__init__.py
```

`memory/sops/` 保持现状，仍由记忆模块管理。

## 六、插件运行时

### 6.1 目录组织

注意区分两个同名目录：`zero_agent/extensions/` 是运行时的源码实现（见第五节），`~/.zeroagent/extensions/` 才是外部插件的存放处，两者互不重叠。

```text
~/.zeroagent/extensions/
├── my_guard.py              # 单文件插件
├── daily_stock/             # 目录插件
│   ├── __init__.py          # 入口，必须存在
│   ├── tools.py             # 目录内可自由拆分
│   └── prompts.py
└── _shared/                 # 下划线开头：不加载，仅作共享库
```

发现规则：

- **只扫描一层**，不递归；更深的模块由插件自己 `import`。
- `*.py` 且不以 `_` 或 `.` 开头 → 单文件插件，入口是模块本身。
- `*/__init__.py` 存在 → 目录插件，入口是 `__init__.py`。
- `_` 开头的目录、`__pycache__` 一律跳过，可用来放公共代码。
- 插件名 = 文件名或目录名（去掉 `.py`），用于日志、冲突裁决与卸载。

### 6.2 加载时机与顺序

| 项目 | 规则 |
| --- | --- |
| 时机 | `ZeroAgent.__init__` 中，`ToolRegistry.with_builtins()` 与 handler registry 归一化**之后** |
| 理由 | 内置工具先就位，插件才能覆盖或补充；handler 可能替换 registry，
  扩展必须加载到最终那份 registry 上，否则插件工具会注册进随后被丢弃的对象 |
| 顺序 | 按插件名字典序，稳定可预期 |
| 次数 | 每个插件 `setup(ctx)` 只调用一次，进程内常驻 |
| 失败 | 导入或 `setup` 抛异常 → 记录日志并跳过该插件，不阻断启动 |

### 6.3 插件形态

单文件或目录，内部导出唯一入口函数 `setup(ctx)`：

```python
# ~/.zeroagent/extensions/my_guard.py
NAME = "my-guard"          # 可选，仅用于日志
VERSION = "0.1.0"          # 可选

def setup(ctx):
    @ctx.tool(name="deploy", description="部署到目标环境", parameters={...})
    def deploy(args):
        return run_deploy(args["target"])

    @ctx.on_tool_call
    def guard(call):
        if call.tool_name == "code_run" and "rm -rf" in call.args.get("code", ""):
            return ctx.block("危险命令已拦截")

    @ctx.on_llm_request
    def tag(request):
        # 只影响本次请求；system_prompt 会在下一轮自动恢复，不会累积
        return request.replace(system_prompt=(request.system_prompt or "") + "\n[tag]")
```

要点：

- **没有 manifest、没有清单文件**。存在即加载，`setup` 返回后运行时接管。
- 模块级常量 `NAME` / `VERSION` / `DESCRIPTION` 可选，不参与发现。
- 插件自行 `import` 依赖；框架不代为安装，缺依赖按加载失败处理。
- 一个插件拥有的全部注册归属于它，可整体撤销。

### 6.4 生效机制

三类注册对应三种生效时机：

| 注册 | 存在哪里 | 何时生效 |
| --- | --- | --- |
| 工具 | `ToolRegistry` | 下一次 `agent.run()` 生成 schema 时 |
| 提示词块 | 运行时块列表 | 下一次 `_build_prompt_assembly()` |
| 事件回调 | 运行时事件表 | 下一次对应事件触发 |

`on_llm_request` 在 LLM session 组装完整请求后执行，但只接收**当前轮**的
消息副本：前缀历史保持原对象，本次变换不回写 `history`。`system_prompt` 的
改写只作用于本次 wire 请求，下一轮自动恢复原值。`model` 与 `headers` 字段
保留在契约中，但当前版本不会生效（切换模型与自定义请求头需要 LLM 会话层配合，
留待后续）。

依据：`agent.py` 的 `run()` 每次都调用 `registry.generate_openai_schema()` 重新生成工具 schema，因此启动时注册的工具不需要额外刷新。

### 6.5 热重载边界

`reload_config()` 在工具语言变化时会重建 `ToolRegistry`（见 `core/agent.py`）。此时插件注册的工具会随旧 registry 一起消失。

运行时必须在 registry 重建后重新应用插件注册，否则表现为“改了配置，插件工具突然不见了”。这是实现阶段必须覆盖的边界，验收要有对应用例。

`extensions` 字段的生效边界：

| 字段 | 生效方式 |
| --- | --- |
| `sandbox` | 热重载时随 registry 重建立即生效 |
| `enabled` / `home` | 下一轮 turn 边界自动应用（§15.1） |

生命周期与卸载：`ZeroAgent.close()` 调用 `ExtensionRuntime.release()`，
撤销插件工具、门控与提示词块，并执行卸载回调（关闭 MCP 子进程）；可重复调用。
CLI 退出、桌面端删除/重置会话时显式调用，进程退出时另有 `atexit` 兜底。
插件在每轮同步中热加载/热卸载（§15.1），改代码无需重启进程。

### 6.6 `ctx` 接口（对外唯一契约）

| 方法 | 语义 | 注册归属 |
| --- | --- | --- |
| `ctx.tool(...)` | 注册工具进 `ToolRegistry`；可声明 `effects / write_path_keys / execute_code_keys` | 插件 |
| `ctx.on_agent_start / on_turn_start / on_turn_end / on_agent_end` | 通知型，返回值忽略 | 插件 |
| `ctx.on_llm_before / on_llm_after` | 通知型，只观察请求/响应 | 插件 |
| `ctx.on_tool_before / on_tool_after` | 通知型，只观察工具生命周期 | 插件 |
| `ctx.on_llm_request` | 变换型，可改当前轮 `messages / tools / system_prompt`（只作用于本次请求） | 插件 |
| `ctx.on_tool_call` | 门控型，可放行、改参、阻断 | 插件 |
| `ctx.on_tool_result` | 变换型，可改结果 | 插件 |
| `ctx.prompt_block(name, factory)` | 贡献系统提示词块 | 插件 |
| `ctx.block(reason)` / `ctx.confirm(question, candidates=..., scope=...)` | 门控回调的返回值：拒绝 / 请求人类批准 | — |

接口刻意保持小：**工具、提示词、请求、工具调用、工具结果，五类**。其余能力通过这五类组合实现。
`ctx.confirm` 不是独立通道，而是 `on_tool_call` 门控的返回值；批准后由任务等待/恢复链路续跑。

通知型回调是**只读契约**：每个回调拿到的是负载的深拷贝（运行期对象如
`handler` 按引用传递并标为只读），原地修改 `messages`/`args` 不会污染真实请求或历史。
需要修改行为时使用变换或门控。

### 6.7 三类扩展点语义

这是本方案的核心契约，替代“所有事件都走 waterfall”的错误做法。

| 类别 | 事件 | 返回值 | 链式 | 异常处理 |
| --- | --- | --- | --- | --- |
| 通知 | `agent_start`、`turn_start`、`llm_before`、`llm_after`、`tool_before`、`tool_after`、`turn_end`、`agent_end` | 忽略 | 否，全部执行 | 记录后继续 |
| 变换 | `llm_request`、`tool_result`、`prompt_block` | 返回新值 | 是，后一个收到前一个的结果 | 视为不干预，继续链 |
| 门控 | `tool_call` | `None`（放行，可原地改 `call.args`）/ `ctx.block(...)` / `ctx.confirm(...)` / `False` | 否，按顺序求值 | **fail-closed：异常或无法识别的返回值即 block** |

门控与变换的区别是硬性的：

- 门控用于安全决策，**判断不了就必须阻止**，不能用“异常忽略”的方式放行。
- 变换用于增强，出错时退回原值，不能让插件错误毒化请求。

### 6.8 注册归属与卸载

`ToolRegistry.register` 目前同名直接覆盖，虽然有 `remove()` 但不能恢复被覆盖的原定义（`tools/registry.py`）。运行时在其上增加一层：

1. 注册前记录被覆盖的原始 `ToolDefinition`。
2. 卸载插件时按逆序恢复。
3. 插件之间同名冲突时按加载顺序裁决，冲突写入日志。

这是采纳 DSH “注册即效应、卸载即回滚”的最小实现，也是后续支持热重载的前提。

## 七、四类扩展来源

| 来源 | 位置 | 可写性 | 是否参与优先级 | 加载方式 |
| --- | --- | --- | --- | --- |
| SOP | `memory/sops/*.md` | agent 可修订，受 L0 约束 | **最高** | 已有机制，正文按需 |
| Skill | `~/.zeroagent/skills/<name>/SKILL.md` | 需人类同意 | 低于 SOP | 只读，索引注入 + 正文按需 |
| MCP | `~/.zeroagent/mcp.json` | 需人类同意 | 不参与仲裁 | 内置扩展注册为工具 |
| Plugin | `~/.zeroagent/extensions/` | 需人类同意 | 不参与仲裁 | 运行时加载 |

### 7.1 SOP

保持现状，不做迁移。它是唯一“agent 可以自己改并沉淀”的知识载体，写入仍需遵循 L0 的 `memory_management_sop`。SOP 优先级最高，但仍低于用户当轮指令与运行时的安全策略。

### 7.2 Skill

- 目录结构完全对齐 `~/.dsh/skills`：一个目录一个 `SKILL.md`，YAML frontmatter 带 `name` 与 `description`。
- 外部 skill 可以直接软链进来，无需改写。
- 提示词中只注入“名字 + 一句话描述”，正文由模型按需读取（与 pi 的按需加载一致）。
- skill 是只读来源，运行时不为它提供写入口。

### 7.3 MCP

- 单个 `mcp.json`，使用社区标准的 `mcpServers` 键，靠字段形状判断传输方式（`command` → stdio，`url` → HTTP）。
- 支持 `${ENV}` 替换，凭据只存在环境变量里。
- 工具名统一加前缀 `mcp__<server>__<tool>`，避免与内置工具重名。
- MCP SDK 在首次使用时才导入；未配置时零依赖。
- 由 `extensions/builtin/mcp_client.py` 实现，核心代码不出现 MCP 概念。

**实现说明（当前状态）**：

- 首版只实现 **stdio** 传输，且**不依赖 MCP SDK**——按 newline-delimited JSON-RPC 手写，
  因此未配置 MCP 时进程内不出现 `mcp` 模块（满足零依赖要求）。
- 配置里只有 `url`（HTTP 传输）的 server 会被记录一条警告后跳过；HTTP 传输待后续实现。
- 子进程在进程退出时由 `atexit` 统一关闭。

## 八、审批与恢复

### 8.0 权限模式（`permissions.mode`）

| 模式 | 人类参与 | `confirm` / `ApprovalRequired` | `block` / 插件 veto / 门控异常 |
| --- | --- | --- | --- |
| `ask` | 有 | 暂停任务，等用户批准 | 拒绝 |
| `full`（默认） | 无 | **不执行** | **不执行**（含门控异常） |

`full` 就是关掉整层门控：`ExtensionRuntime.evaluate_tool_call` 在方法入口直接
返回 `allow`，不构造 `ToolCall`、`on_tool_call` 回调**一次都不跑**。危险 shell、
`effects` 声明缺失、写入目标不可判定、插件主动 veto、门控抛异常在 `full` 下都
不再拒绝。`ask` 保留原有的审批三态与 fail-closed（门控异常即拒绝）。
`permission_gate` 只负责给出 `confirm` / `block`，不感知模式。

运行时切换：`/permissions [ask|full]`（只影响当前进程，不写回 YAML）。

**底线层（两种模式都生效，不弹审批）**：`zero_agent/core/safety.py` 在
`file_write` / `file_patch` 的入口做一次写入判定，命中即返回错误，不进入门控、
也没有「同意」选项。保护范围是**不可逆破坏**，不是行为管控：

- 系统与工具链：`/System`、`/Library`、`/Applications`、`/usr`、`/bin`、`/sbin`、
  `/etc`、`/private/var/{db,vm}`、`/dev`、`/boot`、`/Volumes`、`/opt/homebrew`、
  `/usr/local`。
- 凭据与安全配置：`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.kube`、`~/.docker`、
  `~/.password-store`、`~/.config/gh`、`~/.netrc`、`~/.git-credentials`、
  `~/Library/Keychains`。
- 用户重要数据：`~/Documents`、`~/Desktop`、`~/Downloads`、`~/Pictures`、
  `~/Movies`、`~/Music`、`~/Library/Mobile Documents`（iCloud Drive）。
- 任何 `.git` 目录（hooks/config/refs 被改写等价于植入持久化后门）。

仓库源码、workspace、临时目录都不在保护范围内，`file_write` 照常写：**自我升级
不受影响**。用 `safety.protected_paths` 追加、`safety.allow_paths` 解禁（例如把
`~/Downloads` 交给 agent 管）。`code_run` 在沙箱**未真正生效**时（`off`、平台
不支持、探测失败）额外用 `shell_policy` 做静态兜底：格式化、写块设备、关机、
fork 炸弹、workspace 之外的递归删除都被拒；沙箱生效时不做这一步，命令保持自由。

**威胁模型提醒**：`full` 下 agent 可以不经批准修改 `<home>/extensions/` 与仓库
源码，而插件热加载（第十五节）与核心热重启会在**同一个会话的下一轮**生效。
也就是说「模型写插件/核心代码 → 进程内任意代码执行」这条链在默认配置下是通的，
且 `permission_gate` 完全不执行。这是 `full` 的已知代价；需要人工把关时把
`permissions.mode` 切回 `ask`，需要机器级隔离时用 `extensions.sandbox`。

### 8.1 触发场景

- agent 修改 skill / 插件代码 / `mcp.json`。
- agent 执行被策略判为高危的工具调用（如越界写入、危险命令）。
- 插件在门控里返回 `ctx.confirm(...)` 主动请求确认。

### 8.2 机制

1. 运行时生成一个待批准操作描述（工具、目标、参数摘要、内容指纹）。
2. 复用现有等待链路：返回 `StepAction.WAIT_FOR_USER`，在 `PendingTaskState` 中记录。
3. 用户答复后，只有**与指纹匹配**的那一次操作被放行，随后作废；拒绝则返回“禁止重试”提示。
   对「同一个文件反复改写」这类高频场景，可由插件给出 `scope`（批量授权键），
   批准后在同任务、同 scope、通行证有效期内不再重复询问（详见 8.4）。

落地位置（阶段 3 已实现）：

- `core/types.py` 的 `waiting_kind` 增加 `"approval"` 取值。
- `core/agent.py` 的恢复路径识别批准/拒绝，并按指纹放行被暂停的那一次操作。
- 审批绑定具体变更（指纹），不能仅凭“模型收到了同意的文本”放行；答复只认
  整串命中的批准词（允许句末标点），其余一律按拒绝处理。

### 8.3 与“受信任插件”的区分

用户启用一个插件，意味着允许它正常改变行为（提示词、请求、工具选择）。这与“agent 自行修改插件代码”是两件事，只有后者才走审批。

### 8.4 实现形态（当前）

- **三态门控**：`evaluate_tool_call()` 返回 `allow` / `block` / `approve`。
  插件在门控里 `return ctx.confirm("原因")` 即要求审批。
- **指纹绑定**：审批指纹是「工具名 + 全部业务参数（忽略 `_index` 等内部键）」的
  SHA-256 前 16 位。模型只要改了目标或内容，指纹就变，需要重新批准。
- **一次性通行证**：批准后写入 `runtime._approvals`，绑定 `task_id` 且默认
  600 秒有效；下一次同指纹调用消费后立即失效，跨任务无法复用。
  拒绝时只清空「当前任务 + 未绑定任务」的通行证，其他任务的通行证不受影响。
- **批量授权（scope）**：`ctx.confirm(..., scope="...")` 在批准时额外登记一个
  路径级的批量授权，键为 `(task_id, scope)`，与一次性通行证同寿命。之后满足
  「同任务 + 同 scope」的调用直接放行，不再弹审批——内容改一个换行也会被覆盖，
  这是刻意的取舍：模型反复改写同一个文件时，逐字节审批只会把用户训练成
  「闭眼点同意」。窗口由 TTL、任务绑定和路径级 scope 三重收窄；`permission_gate`
  的 scope 形如 `write:<解析后的绝对路径>`，只覆盖该文件，不覆盖同目录其他文件。
- **求值顺序**：门控全部跑完才决定是否消费通行证，避免通行证被后续门控的
  `block` 白烧。
- **暂停与恢复**：门控要求审批时 `dispatch` 返回 `WAIT_FOR_USER`，
  `PendingTaskState.waiting_kind = "approval"`。用户答复被判定为批准
  （「批准/同意/允许/approve/yes/ok」等，允许句末标点）才发放通行证，
  其余一律按拒绝处理——安全优先，不做前缀或模糊匹配（"批准吧"也算拒绝）。
  指纹缺失（暂停状态失效）时按拒绝处理，不会谎报「已批准」。门控给出的
  `candidates` 会透传到暂停数据，前端可直接展示。
- **内置策略**：`permission_gate` 对受保护路径的写入走 `confirm`（可批准），
  问句附带将写入内容的长度与 sha256 摘要，避免盲批；对明确危险的 shell
  （递归强制删除、格式化、关机等）仍直接 `block`。路径判定复用文件工具的
  解析口径（含 `memory_dir` 前缀重定向），避免判定与实际写入位置不一致。
- **写入范围**：落在 workspace、`memory_dir` 与 reflect 模块通过 `WRITE_ROOTS`
  声明的目录（例如定时任务报告目录）之外的写入一律 `confirm`，即使路径不在
  受保护前缀里；门禁不额外放行临时目录（比沙箱更严），判定不了写入目标的
  工具仍然直接拒绝。
- **路径规范化**：`scope` 使用解析后的绝对路径（realpath + 平台大小写归一），因此同一
  文件经软链、相对路径两种写法命中同一个批量授权，不会重复弹审批；
  macOS/Windows 这类大小写不敏感文件系统上，改大小写也绕不过受保护前缀判定。
- **任务严格绑定**：通行证只在 `task_id` 完全一致时可消费；空 `task_id` 不再
  表示「任意任务」（生产路径总是带任务名）。
- **拒绝的清理范围**：用户拒绝时只清除「当前任务 + 未绑定任务」的通行证，
  其他任务已发放的通行证不受影响。
- **已知边界**：没有 `scope` 的门控仍然「一次批准只放行一次具体调用」，模型
  重试时改了内容会重新触发审批；`permission_gate` 有 `scope`，因此在 TTL 内
  同一任务对同一路径的改写不再询问——若需要逐次确认内容，应把 TTL 调小或
  不使用 `scope`。审批只覆盖标准工具路径，`code_run` 内间接写文件仍受
  第九节边界限制。

## 九、安全边界（诚实说明）

上一版曾承诺“写护栏不可绕过”，这不成立，予以撤回。实际分三层：

默认 `permissions.mode: full`：下表中的「审批」与「策略层」只在 `ask` 模式生效
（`full` 下 `evaluate_tool_call` 直接放行）。但**底线层与沙箱层与模式无关**，
永远生效。

| 层 | 手段 | 能挡住什么 | 挡不住什么 |
| --- | --- | --- | --- |
| 策略层（`ask`） | 工具调用门控 + 审批 | 模型通过标准工具越权 | 插件在进程内直接写文件 |
| 底线层（常开） | `zero_agent/core/safety.py`：`file_write`/`file_patch` 拒绝系统目录、凭据、用户重要数据与 `.git`；`code_run` 在沙箱未生效时退回 `shell_policy` 静态判定 | 不可逆的系统损坏与重要数据丢失 | `code_run` 里间接删除（`python -c`）；已被 `safety.allow_paths` 解禁的路径 |
| 流程层 | 注册归属、审批指纹、日志 | 误操作与重复执行 | 恶意代码 |
| 沙箱层（可选，已提供） | `extensions.sandbox` 包装 `code_run`，写入限制在 workspace 与临时目录 | 命令执行时的越界写盘 | 平台差异；Linux 需可用 bwrap；硬链接与白名单外的可写目录仍可绕；降级即失去保护（此时由底线层静态判定兜底） |

文档与提示词中描述的应是**协作规则**，不是强制隔离。需要强制隔离时，走沙箱层。

**沙箱层的已知局限**（务必写进威胁模型，不要当成强隔离）：

- 路径型沙箱无法阻止**硬链接**：白名单外的文件被硬链进 workspace 后仍可写。
- 白名单内的临时目录、以及任何同权限的可写目录，都能被用作落脚点。
- Linux 依赖可用的 `bwrap` 与 unprivileged userns；不可用时整体降级为不隔离。
  macOS 与 Linux 都把临时目录纳入可写白名单（python 模式的脚本落在那里）。
- `auto` 模式的降级只在执行日志里提示，因此不能把"开了 sandbox"当作安全保证。
- 显式写 `macos` / `linux` 时，该平台沙箱不可用即**拒绝执行**（不静默裸跑）；
  需要无隔离执行必须显式写 `off`。

**策略层的判定口径与已知漏洞**（`extensions/builtin/shell_policy.py`）：

- 判定的是「将要执行的那份代码」：`script` / `script_path` 参数，
  以及参数为空时 `code_run` 会退而执行的**回复围栏代码块**。两者都不存在时
  说明本次调用不执行任何代码，交给 `code_run` 自身报 `code_run_missing_input`
  （门控不伪造安全结论，也不打断既有的协议重试预算）。
- `script_path` 用与 `code_run` 完全相同的 `resolve_workspace_path` 解析并读取；
  读不到、越界、超过 200 000 字符一律拒绝——「检查的文件」必须就是「执行的文件」。
- 命令词先剥离引号与反斜杠（`\rm`、`"rm"`、`r''m` 都识别为 `rm`）；
  `sh/bash/zsh -c`、`eval` 的 payload 与 `$(...)`/反引号内容递归判定；
  `find` 只要出现 `-delete`/`-exec`/`-execdir` 就校验遍历起点；
  `cd`/`pushd`/`env -C` 会更新相对路径基准。
- 仍然挡不住的：`sh script.sh` 执行的脚本内容、`python -c`/`perl` 等其它语言里的
  文件操作、以及任何同权限进程可直接做的事。这些是沙箱层（或操作系统隔离）的职责。
- 门控只能判定「参数里声明的代码」与「回复代码块」。如果某个 `execute` 类工具从
  其它参数拼出要执行的代码，它必须把这些参数键全部列进 `execute_code_keys`，
  否则门控看不到——这是工具声明正确性的责任，不是门控能猜出来的。

## 十、配置位置

| 配置项 | 位置 | 说明 |
| --- | --- | --- |
| 扩展根目录 | `~/.zeroagent/` | 由 `extensions.home` 配置（无环境变量覆盖）；测试用临时目录 |
| skill | `~/.zeroagent/skills/` | 与 `~/.dsh/skills` 同构 |
| MCP | `~/.zeroagent/mcp.json` | 单文件，`mcpServers` 键 |
| 插件 | `~/.zeroagent/extensions/` | 单文件或目录 |
| SOP | `memory/sops/`（`memory_dir` 下） | 由记忆模块管理 |
| 运行时配置 | 项目 `config.yaml` 或 `ZA_CONFIG_PATH` | 新增扩展相关字段见下 |

`config.yaml` 新增段（与现有 `config.example.yaml` 一致，**这是当前实际生效的字段**）：

```yaml
extensions:
  enabled: true            # false 时不加载插件、skill 与 MCP（安全门仍生效）
  home: "~/.zeroagent"     # 扩展根目录，可指向临时目录便于测试
  protected_writes: []     # 额外的受保护路径；写入前需人类批准
  sandbox: "auto"          # code_run 沙箱：off | auto | macos | linux（加引号避免 YAML 布尔）
                           # auto 不可用时降级并告警；显式 macos/linux 不可用则拒绝执行
```

生效时机：`sandbox` 随 registry 重建即时生效（配置热重载会触发）；
`protected_writes` 在每次配置热重载后刷新到运行时（策略层按新列表判定）；
`enabled` 与 `home` 变更在下一轮 turn 边界的同步中自动应用（§15.1）。

配置值解析：`enabled` 接受 `true/false` 与 `"true"/"false"`（字符串 `"false"`
不会被当成真值）；`protected_writes` 传单个字符串时按一项处理；`sandbox` 缺省为 `auto`，
非法值会告警并按 `auto` 处理。

**策略说明（与早期设计的差异）**：策略层不再按工具名判断，而是读取
`ToolDefinition.effects`。早期设计里的 `approval.default` / `approval.rules`
三档路径策略仍未实现；当前是固定的能力规则：

| 声明 | 行为 |
| --- | --- |
| `effects` 含 `write`，目标命中 `<extensions.home>/{skills,extensions,mcp.json}`、扩展根目录或 `protected_writes` | `ask`：暂停并请求批准；路径级 `scope`，同任务同路径在 TTL 内不重复询问 |
| `effects` 含 `write`，目标在 workspace 内（即使 workspace 位于扩展根目录之下） | `allow`：不干预 |
| `effects` 含 `write` 但未声明 `write_path_keys` | `deny`：注册错误，无法判定目标 |
| `effects` 含 `write`，声明了键但本次没传，且 `optional_write_paths=False` | `deny`：无法判定目标 |
| 同上但 `optional_write_paths=True` | `allow`：写入落在工具写死的默认位置（通常在 workspace 内），模型控制不到 |
| `effects` 含 `execute`，`execute_code_keys` 收集到格式化/写块设备/关机/fork 炸弹 | `deny`：直接拒绝 |
| `effects` 含 `execute`，参数为空但回复里有围栏代码块 | 按代码块内容判定（与 `script` 同口径） |
| `effects` 含 `execute`，参数与回复代码块里都没有代码 | `allow`：本次不执行任何代码，由工具自身报参数错误 |
| `effects` 含 `execute`，递归强制 `rm` / `find -delete` / `find -exec` 目标全部在 workspace 内 | `allow`：常规清理 |
| 同上，但目标越出 workspace、含 shell 展开、或范围无法静态确认 | `deny` |
| `effects` 含 `unknown`（扩展工具未声明） | `ask`：同任务同工具批量授权 |
| 其余（read / network / memory / user / control） | `allow`：不干预 |

内置工具在注册时声明副作用：`file_write/file_patch` 为 `write`，`code_run` 为
`execute`，`file_read/vision/skill_load` 为 `read`，等等。MCP 工具默认
`unknown`，可在 `mcp.json` 的 server 配置里声明 `effects` /
`write_path_keys` / `execute_code_keys`。

`rm` 判定会先剥掉 `sudo`/`env`/`xargs`/`timeout` 等 wrapper 与 `VAR=value` 前缀，
并沿 `cd` 解析相对路径，因此 `sudo rm -rf /`、`cd /tmp && rm -rf build` 都会被拒绝。
已知边界不变：带 `execute` 的工具内用其它程序（例如 `python -c shutil.rmtree`）
间接删除仍不受策略层约束，需要沙箱层（第九节）。

`memory/sops/` 不在此扩展的管辖范围内，仍由记忆模块的 L0 约束管理。
如果后续需要更细的三档策略，再引入配置化的规则表。

## 十一、分阶段落地

每个阶段都可独立合并、独立验收。

### 阶段 0：工具注册可撤销（✅ 已实现）

- 改动：`tools/registry.py` 在现有 `remove()` 之外增加“覆盖前快照 + 恢复”（不改变现有调用方式）。
- 原因：这是插件运行时与 MCP 卸载的地基，先做不会返工。
- 验收：注册同名工具 → 撤销 → 原工具恢复，现有测试全绿。

### 阶段 1：插件运行时与门控（✅ 已实现）

- 新增：`extensions/runtime.py`、`context.py`、`events.py`。
- 改动：`core/agent.py` 启动时引导运行时；`core/handler.py` 与 `core/loop.py` 消费三类扩展点。
- 内置 `permission_gate.py` 作为第一个使用者，验证门控在异常时 fail-closed。
- 同步：`pyproject.toml` 的 `[tool.setuptools] packages` 增加 `zero_agent.extensions` 与 `zero_agent.extensions.builtin`；补 `zero_agent/extensions/README.md` 作为插件作者的上手文档。
- 验收：
  - 一个外部单文件插件能注册工具并被模型调用。
  - 门控能阻断危险调用，且插件抛异常时仍然是阻断。
  - `on_llm_request` 能修改请求且不破坏流式。

### 阶段 2：skill 与 MCP（✅ 已实现；MCP 暂只支持 stdio）

- 内置 `skills_index.py`：扫描目录、注入描述、支持 `~/.dsh/skills` 软链。
- 内置 `mcp_client.py`：读取 `mcp.json`，按需导入 SDK，注册带前缀的工具。
- 验收：
  - 软链 `~/.dsh/skills` 后 skill 可见，未改一行代码。
  - 新增 `mcp.json` 后工具出现；文件损坏时只跳过该项。
  - 未配置 MCP 时进程内没有 `mcp` 模块。

### 阶段 3：审批与恢复（✅ 已实现）

- 改动：`core/types.py` 的 `waiting_kind`、`core/agent.py` 的恢复路径。
- 验收：批准后只重放被暂停的那一次操作；拒绝后模型不会原地重试。

### 阶段 4（可选）：隔离（✅ 已实现，best-effort）

- 新增 `extensions/sandbox.py`：把 `code_run` 的 argv 包进平台沙箱。
- **写入根固定为 workspace**，不读取工具参数里的 `cwd` —— 否则模型传一个
  `cwd="/"` 就能把隔离变成 `(subpath "/")`，等于关掉沙箱。
- 进入 profile 的路径先做校验：必须是绝对路径、不含引号与换行，否则降级。
- macOS 用系统自带 `sandbox-exec -p <profile>`；Linux 用 `bwrap`。
  两者都要先做**功能探测**（真跑一次最小命令），仅 `which` 命中不算可用：
  Linux 上 unprivileged userns 被禁时 bwrap 会失败。
- 包装后启动失败时回退为直接执行，绝不让正常命令失败。
- 默认 `extensions.sandbox = "off"`，需显式开启。

### 阶段 5：生产硬化（✅ 已实现）

- 安全门安装失败：`ZeroAgent` 构造抛 `ExtensionBootstrapError`，不再静默降级。
- 扩展工具异常：`_wrap_tool` 捕获后返回 `status=error`，任务继续，证据记为 error。
- `on_llm_request` 隔离：在 LLM session 组装请求后执行，只深拷贝当前轮消息，
  不写回 `history`；system prompt 只作用于本次 wire 请求。
- 策略去工具名耦合：`ToolDefinition.effects / write_path_keys /
  execute_code_keys`，`permission_gate` 只按声明判定。
- 事件体系统一：内部插件（langfuse / project_mode / worldline）全部改为
  `setup(ctx)`；删除 `HookSystem` 与 `_trigger_hook`。
- 验收：安全门失败启动失败；扩展工具异常不终止任务；插件改写不污染历史；
  重命名写/执行工具仍受策略保护；全仓无 `HookSystem`。

## 十二、文档与打包同步

本设计要真正进入项目，不只是新增一个 `extensions/` 目录。以下文件必须同步更新，否则该模块会在打包、文档索引和后来者的认知里“不存在”。

| 文件 | 改什么 | 时机 |
| --- | --- | --- |
| `pyproject.toml` | `[tool.setuptools] packages` 增加 `zero_agent.extensions` 与 `zero_agent.extensions.builtin` | 阶段 1 |
| `zero_agent/extensions/README.md` | 插件作者最小上手：目录约定、`setup(ctx)` 签名、一个可直接运行的完整示例 | 阶段 1 |
| `docs/architecture.md` | 项目结构图与核心包表格加入 `extensions/`，并加一条指向本文的说明 | 阶段 1 完成时 |
| `README.md` | 特性列表增加“插件 / skill / MCP 扩展”；项目结构注释补 `extensions/`；文档索引加入本文 | 阶段 2 完成时 |
| 本文 | 状态从“设计稿，未实现”改为“已实现（阶段 N）”，未落地部分保持标注 | 每个阶段完成时 |
| `README.md` / `CONTRIBUTING.md` | 删除已移除的 `core/hooks.py` 相关描述 | 已同步 |
| `docs/architecture.md` | 目录树补充 `extensions/sandbox.py` 与 `builtin/{path_policy,shell_policy}.py` | 已同步 |

`pyproject.toml` 这一项最容易漏且后果隐蔽：editable 安装在开发机上能跑，但构建 sdist 或正式安装时新模块不会被打包，用户安装后 import 失败。验收时应以“从零安装后能 import”为准，而不是只看本地能跑。

## 十三、验收清单

| 编号 | 验收项 | 判定 |
| --- | --- | --- |
| V1 | 新增一个外部 skill 目录 | 无需改代码即出现在提示词索引中 |
| V2 | 删除 `~/.zeroagent/` 全部内容 | 核心工具集与循环照常工作 |
| V3 | 插件在 `setup` 中抛异常 | 仅该插件跳过，启动成功 |
| V4 | 门控插件自身异常 | `ask` 下该工具调用被阻断（fail-closed）；`full` 下门控不执行 |
| V5 | 插件注册同名工具后卸载 | 内置工具恢复 |
| V6 | `mcp.json` 中单个 server 配置错误 | 其余 server 正常，启动成功 |
| V7 | 未配置 MCP | 不导入 MCP SDK |
| V8 | 修改 skill 文件 | `ask` 下需人类同意，默认只对当次变更有效，`permission_gate` 另有路径级批量授权（同任务 + 同路径 + TTL 内不重复询问）；`full` 下直接放行 |
| V9 | SOP 与 skill 同名同主题冲突 | 以 SOP 为准，skill 作为参考 |
| V10 | 切换工具语言触发 registry 重建 | 插件注册被重新应用，工具不消失 |
| V11 | 修改/新增/删除 `extensions/*.py` 或 `extensions/<pkg>/`（含包内任意 `*.py`） | 下一轮自动加载/重载/卸载，`setup` 不重复执行 |
| V12 | 修改 `mcp.json` | 下一轮重建 MCP 客户端，工具集随配置变化 |
| V13 | 修改 `zero_agent/**/*.py` | 先做语法 + 子进程 import 校验，写检查点后 `os.execv` 热重启并自动续跑；同任务 10 分钟内最多 5 次，超限标记 `hot_restart_loop` |
| V14 | 写入 `~/Documents`、`/etc`、`~/.ssh`、任意 `.git` | 两种模式下都被拒（`safety.py` 底线，无「同意」选项）；写仓库源码、workspace、临时目录照常成功 |
| V15 | `sandbox: off` 时执行 `mkfs` / `rm -rf ~/Documents` | 被 `shell_policy` 静态兜底拒绝；沙箱生效时不走这层，命令保持自由 |

## 十四、风险与未决问题

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 插件与共享可变对象 | 现有 hook 传入的是共享列表/字典，原地修改可能产生难以追踪的行为 | 阶段 1 明确“变换通过返回值”，逐步收敛为显式契约 |
| 提示词膨胀 | 插件都能贡献提示词块 | 每块有名字与长度上限，超限截断并告警 |
| 门控误伤 | 门控过严会阻断正常任务 | 门控结果可观测（日志 + 事件），先宽后紧 |
| 审批疲劳 | 过多确认会让用户点“同意”成习惯 | 按路径策略批量授权（`scope` + TTL + 任务绑定），只对高风险单次确认 |
| MCP 工具过多 | `tools/list` 的结果全部注册进 registry，tools schema 会随 server 工具数线性膨胀（**当前未缓解**） | 仅配置实际需要的 server；后续可做按需展开 |
| 写插件即执行 | `full`（默认）+ 每轮插件同步：模型写 `<home>/extensions/*.py` 后下一轮即被导入，等价进程内任意代码执行（`full` 下 `permission_gate` 完全不执行，没有任何门控能拦；`safety.py` 底线只拦系统/凭据/数据/`.git`，不拦仓库源码与插件） | 需要人类把关时设 `permissions.mode: ask`；需要隔离时用 `extensions.sandbox`；不要把 `ZA_DESKTOP_BRIDGE_TOKEN` 之类凭据放进环境变量 |
| 核心代码热重启无监督 | `os.execv` 后新进程若在启动阶段崩溃，没有看护进程会重新拉起，检查点留在磁盘上等人处理 | 重启前的 `validate_core`（语法 + 子进程 import）先挡掉大部分；预算 10 分钟 5 次防重启循环 |
| 每次 turn 的签名成本 | `core_signature` 全量 stat `zero_agent/**/*.py`（本地约 137 文件、~120 ms），桌面多会话叠加 | 命中变化才重启；后续可换目录级 watch |

未决问题：

- 是否需要 `/ext` 之类的 REPL 命令查看已加载插件与冲突？（倾向需要，但不在首版）
- 插件是否需要版本约束与依赖声明？（首版按纯 Python 处理，不做额外约定）
- 是否支持项目级扩展（`.zeroagent/`）：目前先只做用户级 `~/.zeroagent/`，避免概念分裂。

## 十五、热更新与热重启

目标：运行中改 skill / SOP / 插件 / `mcp.json` / config / 核心 Python 代码，都不需要人工审批或手动重启进程。

这些能力写进系统提示词的 `<self_modification>` 块（`zero_agent/core/self_prompt.py`）：
skills / SOP / plugins / `mcp.json` / 核心代码的**真实路径**、生效时机（下一轮 vs 热重启）、
当前 `permissions.mode` 与 `sandbox` 语义、以及常开的底线（系统目录、凭据、用户数据、
`.git` 不可写）。模型不需要猜路径，也不会在改完之后才发现被拦。

### 15.1 每轮同步（`reload_workflow`）

`AgentLoop` 在每次 turn 边界调用 `ZeroAgent.reload_workflow()`：

| 对象 | 判定 | 动作 |
| --- | --- | --- |
| `config.yaml` | mtime 变化 | 原子重载配置、重建 LLM client 与 registry，扩展运行时换绑新 config |
| `extensions.enabled/home/browser` | 拓扑三元组变化 | 换 home、卸/装内置扩展（`browser` 变化强制重装 `browser_tools`）、按需重载全部插件 |
| 插件 | 入口文件（单文件）或包目录内每个 `*.py` 的 `mtime_ns + size` 摘要变化 | 执行 teardown、撤销注册、清 `sys.modules` 与 `pyc`、重新 `setup` |
| `mcp.json` | `mtime_ns + size` 变化 | 重跑 `mcp_client.setup`（关旧子进程、拉新 server） |
| 核心代码 | `zero_agent/**/*.py` 全量摘要变化 | 校验 → 写检查点 → `os.execv` |

只同步有变化的东西：签名未变时一次 turn 只做一轮 stat。`extensions.hot_reload: false` 时插件与 MCP 不参与，config 与核心代码仍生效。

两条容易踩空的细节：

- **内置扩展的代码改动也会被重新执行。** `mcp_client` / `skills_index` /
  `browser_tools` 走 `ExtensionRuntime.load_builtin_module`：清掉该模块的 `pyc`
  后 `importlib.reload`，否则 `import_module` 只是从 `sys.modules` 取回旧对象，
  源码改动永远读不到。拓扑变化（`enabled/home/browser`）触发的重装同样走这条路，
  所以“重装即重读代码”。
- **失败的插件不会每轮重试。** 插件签名在尝试加载**之前**记录，`setup` 抛异常
  也留档：同一份坏代码只尝试一次，文件改动或 `/update workflow`（`force=True`）
  才重试。排错流程因此是「改文件 → 自动重试」或「改完 `/update workflow`」。
- **底线层不挡自我升级。** 仓库源码、workspace、临时目录都不在
  `zero_agent/core/safety.py` 的保护清单里，`file_write` 照常写核心代码；
  被拒的只是系统目录、凭据、用户重要数据与 `.git` 元数据。git 操作走
  `code_run`（沙箱内），因此“改代码 → 重启”与“推远端”两条路都保留。

### 15.2 核心热重启

1. `validate_core()`：先 `compile()` 每个核心模块（语法），再起一个短命子进程
   `import zero_agent, zero_agent.core.agent, zero_agent.runners.cli`（import 期错误）。
   失败则本签名记为 blocked，不再每轮重试，直到下一次文件变化。
2. `save_checkpoint(status="running", restart=True)`：history、`pending_messages`、
   handler working / history_info、task contract、evidence ledger、等待状态、
   模式上下文（task/func 的续跑参数）、重启时间戳。**只在有运行中任务
   （`_is_running_task` 或 `_pending_task_state`）时写**：空闲时没有可续跑的
   对话，写检查点只会让新进程恢复出一段陈旧历史。空闲重启的报告里因此没有
   `checkpoint_path`；有任务但写不了检查点（如未配置 `sessions_dir`）才返回
   `core_restart_error == "checkpoint_failed"` 并拒绝重启。
3. CLI 直接 `os.execv`；桌面 bridge 把 exec 排到事件循环 0.3s 之后，先给前端
   一条 `bridge-log`。没有检查点时（空闲重启）不设 `ZA_RESUME_CHECKPOINT`，
   CLI 提示语也说清「当前没有运行中任务，不恢复对话」。
4. 新进程启动时恢复：CLI 读 `ZA_RESUME_CHECKPOINT`，桌面扫描
   `sessions/checkpoints/*.json`（`source=desktop`）——`running` 的自动续跑，
   `waiting` 的恢复成待答问题。
5. 预算：同一任务 10 分钟内最多 5 次自动重启（`checkpoint.RESTART_BUDGET_*`），
   超限停止自动续跑并标记 `hot_restart_loop`，检查点删除。
6. exec 失败（不支持的平台机制、权限问题）：桌面 bridge 用检查点重建会话
   运行时并提示用户「发送任意消息即可继续」，不会留下指向已关闭 agent 的会话。

### 15.3 检查点约定

- 存放：`<sessions_dir>/checkpoints/<session_id>.json`，目录 `0700`、文件 `0600`，
  写入走临时文件 + `fsync` + `os.replace`。
- 版本：`version` 不匹配直接忽略。
- history 逐条做 JSON 往返，序列化不了的条目丢弃并告警（不用 `default=str`
  兜底，否则多模态内容会被字符串化后回灌给模型接口）。
- `pending_messages`（已构造但未落进 history 的请求）按最长重叠合并，避免
  重启后整段对话重复。
- `/new`、会话删除、任务正常结束都会清检查点；只有热重启路径保留 `running` 检查点。
