# zero_agent/reflect/ — 反射式自动唤醒模块
#
# 这些模块实现 check() 接口，被 ReflectRunner 周期性调用。
# 返回任务字符串时触发 agent 执行，返回 None 时休眠，返回 '/exit' 时退出循环。
#
# 模块协议:
#   INTERVAL: int   — check() 调用间隔（秒）
#   ONCE: bool      — 是否触发一次后退出
#   check() -> Optional[str]  — 返回任务 / None / '/exit'
#   init(dict)      — (可选) 初始化配置
#   on_done(result) — (可选) 任务完成回调
