# 本地上下文、记忆与技能

ZeroAgent 现在只支持 `local_v1` 单机上下文运行时。个人记忆、项目资料、来源回执和内置 SOP 作为有版本的逻辑对象管理；`context.mode: legacy` 已移除，旧配置会在启动时被拒绝。本项目仍处于开发阶段，不迁移旧用户数据；旧 `global_mem*` 文件不会被自动读取或修改。

## 启用与数据根

`local_v1` 是唯一运行时；配置中可以保留 `mode: local_v1` 作为显式说明：

```yaml
workspace_dir: ./workspace
memory_dir: ./memory
context:
  mode: local_v1
  root: ./context-data
  local_user_id: local
  auto_injection_max_bytes: 8000
  search_backend: lexical
  # 仅在确实需要且宿主允许时授予：
  # skill_capabilities:
  #   - skill:skill_search:network
  #   - skill:skill_search:environment:read
```

相对 `context.root` 以配置文件所在目录解析。上下文根不能与 `memory_dir` 相同、互相嵌套或位于源码/测试目录内；默认 `./context-data/` 已加入 Git 忽略。SQLite manifest 与不可变正文修订共同构成正式数据，备份时必须一并保存整个根目录。该根是本机明文存储，不提供静态加密；不要把 API 凭据导入记忆或资料。

运行时可用 `ZA_CONTEXT_ROOT`、`ZA_LOCAL_USER_ID`、`ZA_CONTEXT_SEARCH_BACKEND` 和 `ZA_CONTEXT_AUTO_INJECTION_MAX_BYTES` 设置对应值。`ZA_CONTEXT_MODE` 只接受 `local_v1`；旧值 `legacy` 会报配置错误。环境变量与 YAML 对同一字段显式给出不同值时启动报配置冲突；技能能力只接受 YAML/宿主显式授予，不支持环境变量隐式开通。

旧 `memory_dir` 中的事实库和导航文件不属于新的记忆运行时，也不会因配置改动而导入；开发期间以空的 local context 根开始工作。历史会话归档仍由 `memory_dir` 配置路径供归档器使用，它不是旧记忆模式。仓库保留显式迁移命令供后续确有需要时使用，但当前开发验收不要求执行迁移。

## 身份、项目和授权

本地入口的个人身份来自 `context.local_user_id`；session binding 在 CLI、桌面、TUI、ACP 入口内持久化。项目通过随机 `project_id` 识别，目录仅是可重绑的位置。管理命令都调用共享 `ContextAdminService`：

```bash
zero-agent context project create --path ./workspace/my-project --name "My project"
zero-agent context project rebind --id <project-id> --path /new/location --expected-revision 1
zero-agent context project grant --id <project-id> --principal <user-id> --role reader --expected-revision 2
zero-agent context project revoke --id <project-id> --principal <user-id> --expected-revision 3
zero-agent context project transfer-owner --id <project-id> --to <user-id> --expected-revision 4
```

只有 owner 可以管理授权或转让所有权；修改使用 expected revision，过期 revision 会冲突。项目 reader 可读，writer 才可导入资源或写入项目记忆。撤销授权会立即影响后续读取和检索。目录重绑不会移动文件、合并项目或更改项目 ID。

Bot 的 principal 按平台、bot account 和平台验证过的 sender ID 解析；未链接的频道身份彼此独立。跨频道链接由当前本地身份发起，并且只能在目标 bot 私聊兑换：

```bash
zero-agent context identity link begin --target telegram/<bot-account>
zero-agent context identity list
zero-agent context identity link revoke --target telegram/<bot-account> --principal <principal-id> --expected-revision <revision>
```

`begin` 返回一个只显示一次的短时代码；服务只保存其哈希，默认 10 分钟过期且只能兑换一次。请在目标 bot 的私聊中发送 `/link <code>`。群聊、未验证 sender、错平台/账号、过期码和重放都会失败。撤销链接停止后续身份共享，不会删除任何已保存对象。群聊消息不会默认写入个人偏好或个人记忆。

## 读取、资源和技能

模型工具使用 `za://` 逻辑 URI 读写经授权的对象，不通过 basename 推断身份。`context_search` 未提供 `scope` 时只搜索当前用户记忆和当前绑定项目；需要跨项目或搜索技能等其他授权集合时，必须显式提供 `scope`，并仍受当前身份的授权限制。结果包含 URI、revision 和来源状态。自动注入受 `auto_injection_max_bytes` 限制，始终规则、偏好、当前项目概览、技能导航和工作便签分块管理；完整技能正文按需加载。

本地资料仅接受受大小、文件数和递归深度限制的 UTF-8 文本/Markdown 导入。项目目录需要 writer 授权：

```bash
zero-agent context resource import-file \
  --project-id <project-id> \
  --path ./docs/design.md \
  --target-uri za://projects/<project-id>/resources/docs/design \
  --title "Design"
zero-agent context resource import-dir \
  --project-id <project-id> --path ./docs \
  --target-prefix za://projects/<project-id>/resources/docs
```

导入会保留来源 hash 和修订；以后宿主文件变化不会静默改变已提交对象。首轮不解析 PDF、不做网页抓取或向量检索。`reindex` 验证已提交正文修订；持久搜索索引尚未实现。
每次模型请求记录实际 system、工具 Schema JSON、messages JSON、各 role 消息内容和 ContextAssembler 区块的 UTF-8 字节数，并以约 4 bytes/token 粗略换算。`api_request_components_bytes` 是 system、tools、messages 三项的 JSON 序列化近似值，不含 HTTP/供应商包装，不能当作服务端计费 token；多模态内容也是序列化估算。未分类 system 字节单列，不计入 context budget；诊断数据不会写回记忆。
上下文访问计数按 principal、逻辑 scope、URI、revision 和 read/list/search 操作分区。管理视图由 ContextService 按当前 RuntimeContext 重新授权；撤销授权会隐藏当前无权访问对象的历史计数。读取不依赖统计写入，统计故障仅产生不含正文的本地告警。

内置技能清单绑定发行包内的精确源 hash。`code_review_principles.md` 是只读参考；`skill_search.md` 委托现有打包模块。后者需要宿主显式授予以下两项能力，且没有默认 grant：
每次执行都会再次校验清单声明的入口依赖 hash，并把验证后的依赖字节写入该调用专属的私有快照；执行环境不继承宿主 `PYTHONPATH` 或用户 site。超时/取消只终止该调用的独立进程组，并清理其临时快照，不影响其他会话的技能执行。

```yaml
context:
  skill_capabilities:
    - skill:skill_search:network
    - skill:skill_search:environment:read
```

技能能力是 ZeroAgent 的调用授权门，不是操作系统沙箱。该调用仅允许清单中审核过的内置入口；代码执行器和通用工作区工具仍拥有宿主赋予进程的权限。不要将权限检查描述成对任意代码的隔离。

清单固定并分类了 29 份发行 SOP 的来源 hash，242 个章节映射绑定原始行号和分类目标；打包前会校验来源清单、hash 和章节覆盖。任务提示注入紧凑触发目录，不注入 SOP 正文。受保护操作前须显式调用 `skill_route`，用户请求命中目录触发条件时必须路由全部匹配技能/规则；没有触发项的操作任务仍须提交空路由。触发技能必须按精确 URI 和修订加载后才能完成任务或执行其受保护动作。写文件、执行代码、浏览器访问、视觉读取、记忆更新和技能执行受动作门禁保护；路由或加载失败时操作会被拒绝。未触发操作规则的普通文本回答无需路由。

## 显式导入、导出和清除

当前开发过程不迁移旧用户数据。若后续确有单独导入需求，可显式生成只读扫描清单；`dry-run` 不初始化目标存储，也不更改源：

```bash
zero-agent context migrate --dry-run \
  --source ./memory --manifest ./migration-plan.json
zero-agent context migrate --apply ./migration-plan.json
zero-agent context verify --manifest ./migration-plan.json
```

Apply 会重新计算源 preimage，任何变化都会拒绝整批导入。逐字节等同的发行 SOP 映射到相应只读技能；其他旧文本保留 `legacy_unverified` 状态，不会升级为已验证事实。疑似秘密进入 `sensitive_quarantined`，不进入模型可读内容或全文检索。该命令是显式工具，不会在启动或升级时自动运行。

检查对象完整性、导出个人或当前身份有权访问的项目：

```bash
zero-agent context reindex
zero-agent context export --output ./context-export.json
zero-agent context export --project-id <project-id> --output ./project-export.json
```

导出是用户可读 JSON，可能包含授权对象正文，应妥善保管；隔离对象、其他用户对象和未经授权的项目对象不会导出。导出由本地 catalog 记录并原子发布。导入、导出和迁移不会删除原始来源。普通 `retract` 保留历史修订但停止默认召回；显式 `purge` 删除当前 store 中对应正文、派生数据和可定位来源，并返回逻辑会话、备份及导出残留标识。来源仍被别的对象引用或旧历史来源不可恢复时，结果会明确报告未完成来源清除。已有备份到期前不保证立即清除。

仅可在维护窗口运行 revision 文件 GC；默认只预览，`--apply` 才删除超过宽限期且未被 manifest 引用的 orphan：

```bash
zero-agent context gc --grace-seconds 3600
zero-agent context gc --grace-seconds 3600 --apply
```

GC 会等待存储写锁；它不清理被 manifest 或任何备份保护的 revision。

`file_read` 对已显式导入且受管的别名通过逻辑对象读取，并附 URI/revision；`file_write`/`file_patch` 命中受管根会返回 `MEMORY_WRITE_REQUIRES_SERVICE`。普通工作区文件操作仍可用。任意 `code_run` 可以拥有宿主文件权限，因此应用层守卫不是沙箱；对修订文件的非服务修改会被 hash 完整性检查发现。

`start_long_term_update` 在 `local_v1` 中保留为兼容入口，只提示改走带来源的 `memory_update`，不会读取或修改旧文件；`update_working_checkpoint` 仍只更新本轮工作便签。完整入口说明和离线契约用例见[记忆评测说明](memory-evaluation.md)。

离线测试覆盖服务契约、身份链接、SOP 来源与章节清单、技能加载/门禁和显式导入故障路径。legacy 模式已移除，因此不再进行 legacy/local_v1 对照，也没有切回 legacy 的回放目标。当前仍未运行真实模型评测；它需要先确定模型和费用上限。
