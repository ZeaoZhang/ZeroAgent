# 自主行动 SOP

⚠️ **路径警告**：helper 的路径常量绑定 **项目根目录**（不是 cwd）——已验证 `autonomous_operation_helper.py`：TODO = `<project>/temp/TODO.txt`；报告 = `<project>/temp/autonomous_reports/`（history.txt 也在其中）。cwd 通常是 `<project>/workspace`，故相对路径为 `../temp/TODO.txt`、`../temp/autonomous_reports/`；**`./autonomous_reports/`、`workspace/temp/autonomous_reports/`、`../memory/autonomous_reports/` 都不存在**。另注意 `workspace/temp/TODO.txt` 是 helper 不读的历史副本，别在那里打勾。**最稳妥：以 `set_todo()` / `complete_task()` 返回的绝对路径为准，别凭记忆拼相对路径。**
报告存于 `<project>/temp/autonomous_reports/`，文件名 `RXX_简短描述.md`（XX 从 history.txt 推断自增，`complete_task` 会自动编号并移动）。

授权你进行自主行动，只要不对环境造成副作用都可进行。

## 启动（第一步）
- update_working_checkpoint: `自主行动｜收尾时重读SOP | from zero_agent.utils.autonomous_operation_helper import *; set_todo()/complete_task(tasktitle, historyline, report_path)`

第二步：
```python
from zero_agent.utils.autonomous_operation_helper import *
print(get_history(40))  # 了解历史避免重复
print(get_todo())       # 查看待办
```

## 任务选择
- 有未完成条目 → 取**一条**，直接进入执行，其他条目下次执行
- 无 TODO → 读 `autonomous_operation_task_planning.md` 规划，下次执行
- 不连续两次选相同子任务
- 价值公式：**「AI训练数据无法覆盖」×「对未来协作有持久收益」**

## 执行
- 选定任务后 update_working_checkpoint，将选中的TODO条目和执行注意事项 append 到 checkpoint 中
- 调用 add_done_hook(text="重读自主任务sop，检查你刚刚的收尾工作是否正确，不正确则改正") 追加收尾自检。
- ≤30回合，小步快跑，边探测边实验
- 用临时脚本验证假设；禁只读即下结论，完整验证再写报告
- 即使失败也记录实验过程和结果，失败报告同样有价值
- 用户不在线，遇到需要决策的问题写入报告待审，不要卡住

**收尾（4件事缺一不可）**：
0. 重读本sop
1. 在cwd写报告（文件名任意），若有记忆更新建议，附在报告末尾
2. `from zero_agent.utils.autonomous_operation_helper import *; complete_task(tasktitle, historyline, report_path)` → 自动编号+移报告到 autonomous_reports/+prepend history（historyline 格式：`类型 | 主题 | 结论`，严格单行）
3. `set_todo()` 获取TODO路径 → 将已完成条目标记为 `[x]`（注意前缀）
4. 结束，剩余TODO留到下次再做

## 权限边界
- 无需批准：只读探测、cwd内写操作/脚本实验
- 需写入报告待审：修改 global_mem / memory下SOP、安装软件、外部API调用、删除非临时文件
- 绝对禁止：读取密钥、修改核心代码库、不可逆危险操作

## 等待用户审查
- 用户归来后审查报告，决定批准、修改或拒绝方案
