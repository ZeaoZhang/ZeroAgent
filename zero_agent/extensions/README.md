# ZeroAgent 扩展（Extensions）

这里放插件运行时。你可以不改 ZeroAgent 源码，就在
`~/.zeroagent/extensions/` 下添加通用能力（工具、提示词、门控、请求变换）。

完整设计见 [`docs/extension-design.md`](../../docs/extension-design.md)。

## 30 秒上手

```text
~/.zeroagent/extensions/
└── my_guard.py
```

```python
# ~/.zeroagent/extensions/my_guard.py
def setup(ctx):
    @ctx.tool(name="greet", description="向某人打招呼",
              parameters={
                  "type": "object",
                  "properties": {"name": {"type": "string"}},
                  "required": ["name"],
              })
    def greet(args):
        return f"你好，{args['name']}"

    @ctx.on_tool_call
    def guard(call):
        # 返回 None 放行；返回 ctx.block(...) 拒绝；返回 ctx.confirm(...) 请求人类批准
        # 注意 code_run 的真实参数是 script / script_path，不是 code
        script = str(call.args.get("script") or "")
        if call.tool_name == "code_run" and "rm -rf" in script:
            return ctx.block("危险命令，已拦截")
```

重启 ZeroAgent 后，模型就能看到 `greet` 工具。

## 目录规则

- 只扫描一层，不递归。
- `xxx.py`（不以 `_`/`.` 开头）→ 单文件插件。
- `xxx/__init__.py` → 目录插件，目录内可自由拆分模块。
- `_` 开头的文件或目录会跳过，可以放公共代码。

## 插件形态

必须导出 `setup(ctx)`。`setup` 内可以注册：

| 调用 | 用途 |
| --- | --- |
| `ctx.tool(...)` | 注册模型可调用的工具；可声明 `effects` / `write_path_keys` / `execute_code_keys` |
| `ctx.on_tool_call` | 门控：放行 / 改参 / 否决 / 要求审批 |
| `ctx.on_tool_result` | 变换：改写工具结果 |
| `ctx.on_llm_request` | 变换：改写当前轮模型请求（不写回历史） |
| `ctx.prompt_block(name, factory)` | 贡献系统提示词块 |
| `ctx.on_agent_start` / `on_turn_start` / `on_turn_end` / `on_agent_end` | 通知：只观察 |
| `ctx.on_llm_before` / `on_llm_after` | 通知：观察请求与响应 |
| `ctx.on_tool_before` / `on_tool_after` | 通知：观察工具生命周期 |

## 工具副作用声明

`ctx.tool(...)` 默认 `effects=("unknown",)`。未声明副作用的工具在调用前会请求
人类批准。声明后由 `permission_gate` 按能力判定：

```python
@ctx.tool(
    name="deploy_file",
    description="写部署文件",
    parameters={...},
    effects=("write",),
    write_path_keys=("target",),      # 参数里哪个键是写入目标
)
def deploy_file(args): ...

@ctx.tool(
    name="run_script",
    description="执行脚本",
    parameters={...},
    effects=("execute",),
    execute_code_keys=("script",),    # 参数里哪个键是待执行代码
)
def run_script(args): ...
```

- 只读工具声明 `effects=("read",)` 可避免每次审批。
- 声明 `write` 但未声明 `write_path_keys`，或声明 `execute` 但未声明
  `execute_code_keys`，会被门控拒绝（注册错误）。
- 受保护路径的写入会请求批准；workspace 内写入放行。

## 三类回调的语义

- **通知**：返回值被忽略，回调抛异常只记日志，不影响执行。通知是只读契约，
  需要修改请用变换或门控。
- **变换**：返回新值，链式传递；返回 `None` 表示不干预，抛异常则退回原值。
- **门控**：返回 `None` 放行，返回 `ctx.block(原因)` 拒绝，
  返回 `ctx.confirm(原因)` 要求人类批准。
  **fail-closed**：门控自身抛异常时，本次调用会被拒绝而不是放行。
  批准默认只放行「参数完全一致」的一次调用；如果同一个目标会被模型反复改写，
  可以传 `scope`（例如 `ctx.confirm("...", scope=f"{tool}:{path}")`）登记一个
  同任务、同路径的批量授权，有效期内不再重复询问。

## 常见问题

**加载失败会怎样？** 记录日志并跳过该插件，不影响启动和其他插件。

**能覆盖内置工具吗？** 可以，同名注册会覆盖；但请在插件里明确声明，避免误伤。

**需要装依赖吗？** 插件自己 `import`；框架不代为安装。缺少依赖按加载失败处理。

**修改 skill / 插件代码要审批吗？** 会。写入 `~/.zeroagent/` 下的受保护路径时，
内置 `permission_gate` 会让任务暂停并向用户提问。批准绑定「工具名 + 参数」的指纹，
一次性通行证用过即失效；`permission_gate` 另外登记路径级 `scope`，因此同一任务内
对同一文件的后续改写（内容有变化）在 600 秒内不再重复询问。不要这个行为时，
可以在自己的门控里不传 `scope`。

**插件能主动要求审批吗？** 可以，在门控里 `return ctx.confirm("说明")`。
若同一目标会被反复改写（每次内容略有变化），再加 `scope` 参数避免审批轰炸。
