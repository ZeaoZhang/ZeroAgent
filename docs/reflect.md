# Reflect 系统使用指南

Reflect 系统提供反射式自动任务唤醒能力。运行时周期性检查触发条件，
满足条件时将任务注入 agent 执行。

## 快速开始

```bash
# 空闲自主模式: 用户离开 30 分钟后自动执行任务
zero-agent --reflect zero_agent/reflect/autonomous.py

# Goal 模式: 预算驱动的持续自改进
GOAL_STATE=/path/to/goal_state.json zero-agent --reflect zero_agent/reflect/goal_mode.py

# 定时任务: cron-like 调度
zero-agent --reflect zero_agent/reflect/scheduler.py

# BBS 协作: 从 BBS 接任务
zero-agent --reflect zero_agent/reflect/agent_team_worker.py
```

## Reflect 模块协议

每个模块需要导出以下属性:

| 名称 | 类型 | 必须 | 说明 |
|------|------|------|------|
| `INTERVAL` | `int` | 是 | check() 调用间隔(秒) |
| `check()` | `() -> str\|None` | 是 | 返回 task / None(休眠) / '/exit'(退出) |
| `ONCE` | `bool` | 否 | True 时触发一次后退出 |
| `init(dict)` | 否 | 初始化回调，传入配置字典 |
| `on_done(result)` | 否 | 任务完成后回调 |

## 内置模块

### autonomous.py — 空闲检测

`INTERVAL = 1800` (30 分钟)。用户离开超时后返回自主任务 prompt。

### goal_mode.py — 目标驱动

`INTERVAL = 3`。从 JSON state 文件读取目标和预算配置，
在预算耗尽前持续唤醒 agent 推进工作，预算耗尽时自动收口。

State JSON 格式:
```json
{
  "objective": "实现 XXX 功能",
  "budget_seconds": 3600,
  "max_turns": 50,
  "done_prompt": "收口完成后将报告发送给 xxx"
}
```

环境变量: `GOAL_STATE` 指定 state 文件路径。

### scheduler.py — 定时任务

`INTERVAL = 120`。默认扫描当前工作目录下 `./sche_tasks/` 的 JSON 任务文件，
也可通过 `ZA_SCHED_TASKS_DIR` 指定可写运行目录。按 schedule 时间触发。
支持 repeat: `once/daily/weekday/weekly/monthly/every_Nh/every_Nm`。

任务 JSON 格式:
```json
{
  "enabled": true,
  "repeat": "daily",
  "schedule": "09:00",
  "prompt": "每日站会纪要..."
}
```

### agent_team_worker.py — BBS 协作

`INTERVAL = 60`。轮询 BBS API 获取新帖，有新任务时返回协作 prompt。

配置: `agent_team_setting.json` 在 reflect 模块同目录下。

## 自定义模块

创建一个 Python 文件:

```python
INTERVAL = 300  # 5 分钟检查一次

def check():
    if some_condition():
        return "执行某个任务"
    return None

def on_done(result):
    print(f"任务完成: {result}")
```

运行: `zero-agent --reflect /path/to/my_reflect.py`
