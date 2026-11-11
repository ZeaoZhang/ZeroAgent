# 扩展体系设计（Extension Design）

> 状态：**阶段 0–3 已实现；阶段 4（隔离）未实现**。
> 实现代码位于 `zero_agent/extensions/`，测试见 `tests/test_extension_runtime.py`
> 与 `tests/test_mcp_client.py`。
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
| `core/hooks.py` | 8 个事件；`trigger()` 丢弃返回值并在异常处 `pass` | 只能“通知”，无法可靠阻断 |
| `core/handler.py` `_trigger_hook` | 外层再包一次 `try/except` | 插件异常不会中断工具执行 |
| `core/handler.py` `dispatch` | `tool_before` 在调用前触发；`tool_after` 有两个触发点（主路径在 `_finalize_tool_dispatch`，未知工具分支在 `dispatch` 内直接触发） | 有介入位置，但参数是共享可变对象 |
| `core/loop.py` | `agent_before` 与 `llm_before` 传入 `messages`、`tools`、`system_prompt`；`turn_before` 不传 `system_prompt` | 原地修改可能生效，但语义未定义 |
| `core/agent.py` `_register_builtin_plugins` | 硬编码 import 三个插件 | 没有真正的第三方插件入口 |
| `core/agent.py` `_build_prompt_assembly` | 块序列 base / reply_language / date / memory / task_control / backend_extra / peer_hint / capabilities；其中 `backend_extra` 依赖后端额外提示词非空、`peer_hint` 依赖配置开关，均为条件块 | 外部无法贡献提示词块 |
| `tools/registry.py` `register` | 同名直接覆盖；已有 `remove()`，但没有覆盖前快照与回滚 | 插件替换内置工具后无法恢复原定义 |
| `core/types.py` `PendingTaskState` | `waiting_kind` 仅 `ask_user` / `plan_partial_acceptance` | 没有“待批准操作”的语义 |
| `memory/manager.py` | SOP 是 L3；提示词只注入结构说明与 L1 索引（SOP 名单来自 L1 文件内容），正文按需 `file_read` | SOP 已经是渐进式加载 |
| `zero_agent/skills/` | 无源文件，仅存 `__pycache__` 残留 | skill 体系尚未存在 |
| MCP | 可核查范围内仅在 `frontends/acp_bridge.py` 声明能力为 false | 完全未实现 |

### 已具备的基础

- `ToolRegistry` + `ToolDefinition` 已经是统一的工具声明格式（含 `evidence_kind`）。
- `HookSystem` 已有注册、卸载、`discover_and_load`，是插件系统的雏形。
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
│   ├── agent.py                # 组装（新增：引导插件运行时；提示词补充块）
│   └── hooks.py                # 保留 8 事件；新增语义化的扩展接口
│
├── extensions/                 # 新增：插件运行时（深模块，唯一扩展入口）
│   ├── runtime.py              # 加载、注册归属、顺序、卸载、异常隔离
│   ├── context.py              # 面向插件的稳定 API 对象
│   ├── events.py               # 三类扩展点的契约定义
│   └── builtin/                # 内置扩展（它们和第三方用同一套接口）
│       ├── permission_gate.py  # 写入需人类同意的策略
│       ├── skills_index.py     # skill 发现与描述注入
│       └── mcp_client.py       # MCP 工具来源
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
| 时机 | `ZeroAgent.__init__` 中，`ToolRegistry.with_builtins()` 之后、`BaseHandler` 创建之前 |
| 理由 | 内置工具先就位，插件才能覆盖或补充；handler 依赖最终的 registry |
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

`on_llm_request` 的改写只作用于本次请求：`messages` / `tools` 传入副本，不回写
跨轮状态；`system_prompt` 在下一轮开始时恢复原值，避免逐轮累积。`model` 与
`headers` 字段保留在契约中，但当前版本不会生效（切换模型与自定义请求头需要
LLM 会话层配合，留待后续）。

依据：`agent.py` 的 `run()` 每次都调用 `registry.generate_openai_schema()` 重新生成工具 schema，因此启动时注册的工具不需要额外刷新。

### 6.5 热重载边界

`reload_config()` 在工具语言变化时会重建 `ToolRegistry`（见 `core/agent.py`）。此时插件注册的工具会随旧 registry 一起消失。

运行时必须在 registry 重建后重新应用插件注册，否则表现为“改了配置，插件工具突然不见了”。这是实现阶段必须覆盖的边界，验收要有对应用例。

### 6.6 `ctx` 接口（对外唯一契约）

| 方法 | 语义 | 注册归属 |
| --- | --- | --- |
| `ctx.tool(...)` | 注册工具进 `ToolRegistry` | 插件 |
| `ctx.on_agent_start / on_turn_start / on_turn_end / on_agent_end` | 通知型，返回值忽略 | 插件 |
| `ctx.on_llm_request` | 变换型，可改 `messages / tools / system_prompt`（只作用于本次请求） | 插件 |
| `ctx.on_tool_call` | 门控型，可放行、改参、阻断 | 插件 |
| `ctx.on_tool_result` | 变换型，可改结果 | 插件 |
| `ctx.prompt_block(name, factory)` | 贡献系统提示词块 | 插件 |
| `ctx.skill_paths(...)` | 追加 skill 搜索路径 | 插件 |
| `ctx.ui.confirm(...)` | 请求人类确认（异步，走审批通道） | — |

接口刻意保持小：**工具、提示词、请求、工具调用、工具结果，五类**。其余能力通过这五类组合实现。

### 6.7 三类扩展点语义

这是本方案的核心契约，替代“所有事件都走 waterfall”的错误做法。

| 类别 | 事件 | 返回值 | 链式 | 异常处理 |
| --- | --- | --- | --- | --- |
| 通知 | `agent_start`、`turn_start`、`turn_end`、`agent_end`、`llm_after` | 忽略 | 否，全部执行 | 记录后继续 |
| 变换 | `llm_request`、`tool_result`、`prompt_block` | 返回新值 | 是，后一个收到前一个的结果 | 视为不干预，继续链 |
| 门控 | `tool_call` | `allow` / `modify(args)` / `block(reason)` | 否，按优先级求值 | **fail-closed：异常即 block** |

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

### 8.1 触发场景

- agent 修改 skill / 插件代码 / `mcp.json`。
- agent 执行被策略判为高危的工具调用（如越界写入、危险命令）。
- 插件主动请求确认（`ctx.ui.confirm`）。

### 8.2 机制

1. 运行时生成一个待批准操作描述（工具、目标、参数摘要、内容指纹）。
2. 复用现有等待链路：返回 `StepAction.WAIT_FOR_USER`，在 `PendingTaskState` 中记录。
3. 用户答复后，只有**与指纹匹配**的那一次操作被放行，随后作废；拒绝则返回“禁止重试”提示。

需要扩展的地方：

- `core/types.py` 的 `waiting_kind` 增加 `"approval"` 取值。
- `core/agent.py` 的恢复路径能识别批准/拒绝，并重放被暂停的操作。
- 审批必须绑定具体变更，不能仅凭“模型收到了同意的文本”放行。

### 8.3 与“受信任插件”的区分

用户启用一个插件，意味着允许它正常改变行为（提示词、请求、工具选择）。这与“agent 自行修改插件代码”是两件事，只有后者才走审批。

### 8.4 实现形态（当前）

- **三态门控**：`evaluate_tool_call()` 返回 `allow` / `block` / `approve`。
  插件在门控里 `return ctx.confirm("原因")` 即要求审批。
- **指纹绑定**：审批指纹是「工具名 + 全部业务参数（忽略 `_index` 等内部键）」的
  SHA-256 前 16 位。模型只要改了目标或内容，指纹就变，需要重新批准。
- **一次性通行证**：批准后写入 `runtime._approvals`，绑定 `task_id` 且默认
  600 秒有效；下一次同指纹调用消费后立即失效，跨任务无法复用。
  拒绝则清空全部未使用通行证。
- **求值顺序**：门控全部跑完才决定是否消费通行证，避免通行证被后续门控的
  `block` 白烧。
- **暂停与恢复**：门控要求审批时 `dispatch` 返回 `WAIT_FOR_USER`，
  `PendingTaskState.waiting_kind = "approval"`。用户答复被判定为批准
  （「批准/同意/允许/approve/yes/ok」等）才发放通行证，其余一律按拒绝处理
  ——安全优先，不做模糊匹配。指纹缺失（暂停状态失效）时按拒绝处理，
  不会谎报「已批准」。
- **内置策略**：`permission_gate` 对受保护路径的写入走 `confirm`（可批准），
  问句附带将写入内容的长度与 sha256 摘要，避免盲批；对明确危险的 shell
  （递归强制删除、格式化、关机等）仍直接 `block`。路径判定复用文件工具的
  解析口径（含 `memory_dir` 前缀重定向），避免判定与实际写入位置不一致。
- **已知边界**：批准只放行一次具体调用；如果模型重试时改了内容，
  会重新触发审批。审批只覆盖标准工具路径，`code_run` 内间接写文件仍受
  第九节边界限制。

## 九、安全边界（诚实说明）

上一版曾承诺“写护栏不可绕过”，这不成立，予以撤回。实际分三层：

| 层 | 手段 | 能挡住什么 | 挡不住什么 |
| --- | --- | --- | --- |
| 策略层 | 工具调用门控 + 审批 | 模型通过标准工具越权 | 插件在进程内直接写文件 |
| 流程层 | 注册归属、审批指纹、日志 | 误操作与重复执行 | 恶意代码 |
| 沙箱层（可选，后续） | 子进程/文件系统隔离 | 恶意插件与 `code_run` | 需额外部署成本 |

文档与提示词中描述的应是**协作规则**，不是强制隔离。需要强制隔离时，走第三层。

## 十、配置位置

| 配置项 | 位置 | 说明 |
| --- | --- | --- |
| 扩展根目录 | `~/.zeroagent/` | 可用 `ZA_HOME` 覆盖，便于测试 |
| skill | `~/.zeroagent/skills/` | 与 `~/.dsh/skills` 同构 |
| MCP | `~/.zeroagent/mcp.json` | 单文件，`mcpServers` 键 |
| 插件 | `~/.zeroagent/extensions/` | 单文件或目录 |
| SOP | `memory/sops/`（`memory_dir` 下） | 由记忆模块管理 |
| 运行时配置 | 项目 `config.yaml` 或 `ZA_CONFIG_PATH` | 新增扩展相关字段见下 |

`config.yaml` 新增段（与现有 `config.example.yaml` 一致，**这是当前实际生效的字段**）：

```yaml
extensions:
  enabled: true            # false 时不加载插件、skill 与 MCP
  home: "~/.zeroagent"     # 扩展根目录，可指向临时目录便于测试
  protected_writes: []     # 额外的受保护路径；写入前需人类批准
```

**策略说明（与早期设计的差异）**：早期设计里的 `approval.default` / `approval.rules`
三档路径策略（allow / ask / deny）**未实现**。当前形态是固定的两条内置规则：

| 场景 | 行为 |
| --- | --- |
| 写入 `~/.zeroagent/{skills,extensions,mcp.json}` 或 `protected_writes` 中的路径 | `ask`：暂停并请求批准，批准只覆盖这一次调用 |
| `code_run` 命中明确危险的 shell（递归强制删除、格式化、关机等） | `deny`：直接拒绝，不提供批准入口 |
| 其余写入与命令 | `allow`：不干预 |

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

### 阶段 4（可选）：隔离（⏳ 未实现）

- 文件系统/进程沙箱，独立评估，不在本设计范围内。

## 十二、文档与打包同步

本设计要真正进入项目，不只是新增一个 `extensions/` 目录。以下文件必须同步更新，否则该模块会在打包、文档索引和后来者的认知里“不存在”。

| 文件 | 改什么 | 时机 |
| --- | --- | --- |
| `pyproject.toml` | `[tool.setuptools] packages` 增加 `zero_agent.extensions` 与 `zero_agent.extensions.builtin` | 阶段 1 |
| `zero_agent/extensions/README.md` | 插件作者最小上手：目录约定、`setup(ctx)` 签名、一个可直接运行的完整示例 | 阶段 1 |
| `docs/architecture.md` | 项目结构图与核心包表格加入 `extensions/`，并加一条指向本文的说明 | 阶段 1 完成时 |
| `README.md` | 特性列表增加“插件 / skill / MCP 扩展”；项目结构注释补 `extensions/`；文档索引加入本文 | 阶段 2 完成时 |
| 本文 | 状态从“设计稿，未实现”改为“已实现（阶段 N）”，未落地部分保持标注 | 每个阶段完成时 |

`pyproject.toml` 这一项最容易漏且后果隐蔽：editable 安装在开发机上能跑，但构建 sdist 或正式安装时新模块不会被打包，用户安装后 import 失败。验收时应以“从零安装后能 import”为准，而不是只看本地能跑。

## 十三、验收清单

| 编号 | 验收项 | 判定 |
| --- | --- | --- |
| V1 | 新增一个外部 skill 目录 | 无需改代码即出现在提示词索引中 |
| V2 | 删除 `~/.zeroagent/` 全部内容 | 核心工具集与循环照常工作 |
| V3 | 插件在 `setup` 中抛异常 | 仅该插件跳过，启动成功 |
| V4 | 门控插件自身异常 | 对应工具调用被阻断（fail-closed） |
| V5 | 插件注册同名工具后卸载 | 内置工具恢复 |
| V6 | `mcp.json` 中单个 server 配置错误 | 其余 server 正常，启动成功 |
| V7 | 未配置 MCP | 不导入 MCP SDK |
| V8 | 修改 skill 文件 | 需人类同意，且批准只对当次变更有效 |
| V9 | SOP 与 skill 同名同主题冲突 | 以 SOP 为准，skill 作为参考 |
| V10 | 切换工具语言触发 registry 重建 | 插件注册被重新应用，工具不消失 |

## 十四、风险与未决问题

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 插件与共享可变对象 | 现有 hook 传入的是共享列表/字典，原地修改可能产生难以追踪的行为 | 阶段 1 明确“变换通过返回值”，逐步收敛为显式契约 |
| 提示词膨胀 | 插件都能贡献提示词块 | 每块有名字与长度上限，超限截断并告警 |
| 门控误伤 | 门控过严会阻断正常任务 | 门控结果可观测（日志 + 事件），先宽后紧 |
| 审批疲劳 | 过多确认会让用户点“同意”成习惯 | 按路径策略批量授权，只对高风险单次确认 |
| MCP 工具过多 | tools schema 爆炸 | 提示词只列 server 摘要，工具按需展开 |

未决问题：

- 是否需要 `/ext` 之类的 REPL 命令查看已加载插件与冲突？（倾向需要，但不在首版）
- 插件是否需要版本约束与依赖声明？（首版按纯 Python 处理，不做额外约定）
- 是否支持项目级扩展（`.zeroagent/`）：目前先只做用户级 `~/.zeroagent/`，避免概念分裂。
