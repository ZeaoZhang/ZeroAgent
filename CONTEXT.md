# ZeroAgent Domain Context

## Gateway and conversation terms

- **Channel**：一个外部消息平台接入，例如 Telegram、微信或 Discord。
- **Channel account**：某个 Channel 上运行的具体 bot/account。它用于区分同一平台上的多个 bot 实例。
- **Conversation**：外部平台上的一段可寻址对话。私聊通常对应一个用户，群聊通常对应一个群组或频道。
- **Actor**：触发消息或控制操作的外部用户身份。Actor 用于授权和审计，不决定共享群聊的上下文归属。
- **Session**：Gateway 中承载一段可恢复对话上下文的实体。
- **Session key**：由 Channel、Channel account、Conversation 类型和 Conversation ID 组成的稳定标识。不同 Channel 的对话默认不共享 Session。
- **Session runtime**：为一个 Session 提供运行时能力的实例，包括对话执行、取消、模型选择和工具上下文。
- **Channel adapter**：负责外部平台协议收发与消息格式转换的适配器，不拥有 Agent 对话状态。
- **Gateway**：负责消息接入、Session 解析、运行时调度、事件发布、持久化和控制授权的中心模块。

## Isolation terms

- **Conversation isolation**：不同 Session 之间不共享对话历史、取消信号、模型选择或输出事件。
- **Group-shared conversation**：同一群组 Session 的成员共享该群组上下文；成员身份仍用于授权和审计。
- **Cross-channel isolation**：即使是同一个自然人，不同 Channel 的对话也属于不同 Session，除非未来显式建立身份绑定。
- **Session resource scope**：Session 专属的工作区、记忆、附件、日志和浏览器上下文。
