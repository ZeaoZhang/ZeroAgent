# 渠道控制设置设计

## 背景

ZeroAgent 的桌面 Web2/Tauri 前端目前只有会话、模型和 agent 控制，没有渠道管理界面。仓库已经包含微信、企业微信、钉钉、QQ、飞书、Telegram、Discord 七个 bot 前端，但它们只能通过独立进程手动启动，desktop bridge 尚未提供统一的渠道状态或生命周期 API。

本功能为桌面前端增加渠道控制设置，并补齐 bridge 与 bot 侧的最小运行时支持，确保两个开关都对应真实行为，而不是仅改变 UI 状态。

## 目标

1. 在现有桌面顶栏增加渠道设置入口。
2. 展示七个已支持渠道的配置状态和真实运行状态。
3. 通过“运行”开关启动或停止对应 bot 进程。
4. 通过“连接 App”开关控制该渠道是否向 ZeroAgent 投递新消息。
5. 保持现有 bot 配置来源和凭据安全，不向前端返回密钥值。
6. 保持 Web2 和 Tauri 使用同一套静态前端与 bridge API。

## 非目标

- 不在本功能中编辑 Telegram、Discord 等渠道的 token/secret。
- 不替换现有 bot SDK、消息协议或会话隔离实现。
- 不改变用户已经手动启动的外部 bot 进程的生命周期，除非用户通过本界面的“运行”开关操作同一渠道。
- 不自动发现第三方渠道插件；渠道清单固定为仓库当前七个 bot 模块。

## 术语与行为

### 渠道

固定渠道定义如下：

| ID | 展示名 | 入口 | 必要配置 |
| --- | --- | --- | --- |
| `wechat` | 微信 | `bots/wechat_app.py` | 本机微信 bot token |
| `wecom` | 企业微信 | `bots/wecom_app.py` | `wecom_bot_id`、`wecom_secret` |
| `dingtalk` | 钉钉 | `bots/dingtalk_app.py` | `dingtalk_client_id`、`dingtalk_client_secret` |
| `qq` | QQ | `bots/qq_app.py` | `qq_app_id`、`qq_app_secret` |
| `feishu` | 飞书 | `bots/feishu_app.py` | `fs_app_id`、`fs_app_secret` |
| `telegram` | Telegram | `bots/telegram_app.py` | `tg_bot_token` |
| `discord` | Discord | `bots/discord_app.py` | `discord_bot_token` |

### 运行

“运行”表示对应入口脚本当前是否存在被 bridge 识别的 Python 进程。打开开关立即启动进程，关闭开关停止该进程及其子进程。状态显示来自操作系统进程扫描，不使用前端乐观状态。

启动前 bridge 检查必要配置；配置缺失时返回可读错误，前端恢复开关并提示用户。重复启动和重复停止保持幂等。

### 连接 App

“连接 App”表示渠道收到消息后是否允许投递给 ZeroAgent。关闭连接不会停止渠道 transport 进程，但会在消息进入 agent、命令处理和媒体下载之前被忽略；已有任务不强制中止。重新打开后，新消息恢复处理。

连接配置存储在 `temp/channel_settings.json`，采用原子替换写入。没有该文件或缺少某个渠道条目时默认 `linked: true`，兼容现有 bot 行为。配置文件只保存布尔开关，不保存凭据。

## UI 设计

### 入口

在 `#topbar` 右侧、主题切换按钮旁增加渠道设置图标按钮 `#channel-settings-btn`。复用现有 `.icon-btn`，提供 `title`、`aria-label` 和 `aria-expanded`。

### Modal

新增 `#channel-settings-modal`，复用现有 `.modal`、`.modal-backdrop`、`.modal-content`、`.modal-header`、`.modal-body` 和 `.modal-footer` 样式。

标题为“渠道设置”，副标题说明：运行控制进程；连接 App 控制是否接收并处理新消息。右上角提供刷新和关闭按钮。

主体按渠道渲染卡片，每张卡片包含：

- 渠道展示名和入口模块名。
- 配置状态：已配置 / 未配置；不显示任何密钥内容。
- 运行状态：运行中 / 已停止 / 启动失败。
- “运行” checkbox switch。
- “连接 App” checkbox switch。

每个 switch 使用原生 checkbox 保证键盘和辅助功能可用，视觉层复用项目现有颜色变量。请求期间当前渠道的控制项禁用，失败时重新读取状态并通过现有错误 banner 告知原因。

## 数据流与接口

### Bridge API

新增认证接口：

- `GET /channels`：返回所有渠道的元数据、`configured`、`running`、`pid`、`linked`。
- `POST /channels/{id}/start`：启动渠道并返回最新渠道快照。
- `POST /channels/{id}/stop`：停止渠道并返回最新渠道快照。
- `POST /channels/{id}/link`，body `{ "linked": boolean }`：原子保存连接状态并返回最新渠道快照。

`za-web.js` 为以上接口提供 RPC 映射：`channels/list`、`channels/start`、`channels/stop`、`channels/link`。

### 进程管理

复用 desktop command 的 Python 进程扫描与 terminate/kill 流程，增加渠道定义和入口路径匹配。所有启动进程使用当前 Python 解释器、仓库根目录、标准输入输出重定向到 `DEVNULL`，与现有 scheduler 服务启动约定一致。

### Bot 运行时

在 bot 共享控制模块中提供：

- 渠道定义与必要配置检查。
- `get_channel_settings()`、`set_channel_linked()`、`is_channel_linked()`。
- 原子 JSON 写入和损坏文件的安全默认值。

`AgentBotMixin` 的命令和 agent 执行入口读取连接状态；Telegram、微信等不继承该 mixin 的入口也在各自消息入口读取状态。这样连接开关对所有七个渠道生效，并且不需要重启进程。

## 错误处理

- 未知渠道 ID：返回 404/结构化错误，不修改文件或进程。
- 必要配置缺失：启动返回 409，前端保持关闭并显示缺失字段名称。
- 进程启动后立即退出：返回启动失败，前端重新读取实际状态。
- 停止时进程已退出：视为成功，并刷新状态。
- 连接配置文件不可读或格式损坏：读取时使用 `linked: true`；写入失败返回 500，前端不更新开关。
- 任何 API 失败均保留诊断信息并显示现有错误 banner，不吞掉异常。

## 测试策略

- Python：测试渠道清单、默认连接状态、原子设置读写、配置完整性、未知渠道与损坏配置文件；测试进程状态接口使用隔离的 process scanner/subprocess stub。
- JavaScript：测试渠道 modal 渲染、RPC 路由、运行/连接开关调用参数、请求失败后的状态恢复，以及敏感配置不出现在渲染内容中。
- Smoke：启动 bridge，调用 `/channels`，对一个未配置渠道验证启动错误；在前端实际打开渠道 modal 并验证状态来自 API。
