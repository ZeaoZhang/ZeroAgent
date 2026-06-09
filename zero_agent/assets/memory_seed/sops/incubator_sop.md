# Incubator SOP — 远程部署 ZeroAgent 实例
自我复制到任意节点的 agent 网络。每个节点有独立记忆，可以通过编辑 memory 干预它们的行为

## 文件清单
```
pyproject.toml
zero_agent/assets/*.txt
memory/sops/             # SOP 和可直接干预的记忆文件
config.yaml              # 本地配置文件，不提交
```

## 依赖
requests beautifulsoup4

## 通信
同 subagent 协议：写入 `temp/{name}/input.md` 后运行 `zero-agent --task temp/{name}`
或起reflect worker并设置bbs信息

## 干预记忆
直接编辑远端 memory/ 下的文件（SOP/全局记忆）
