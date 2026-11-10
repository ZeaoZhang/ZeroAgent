import os, json, time as _time, socket as _socket, logging
from datetime import datetime, timedelta

from zero_agent.core.config import project_root

# 端口锁：防止重复启动，bind失败时runner会直接崩溃退出
# reload时mod.__dict__保留_lock，跳过重复绑定
try: _lock
except NameError:
    _lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _lock_port = int(os.environ.get("ZA_SCHED_LOCK_PORT", "45762"))
    _lock.bind(('127.0.0.1', _lock_port)); _lock.listen(1)

INTERVAL = 120
ONCE = False

_dir = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.abspath(
    os.environ.get("ZA_SCHED_TASKS_DIR") or str(project_root() / "sche_tasks")
)
DONE  = os.path.join(TASKS, 'done')
_LOG  = os.path.join(TASKS, 'scheduler.log')

os.makedirs(DONE, exist_ok=True)

# --- 日志 ---
_logger = logging.getLogger('scheduler')
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(_LOG, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                                        datefmt='%Y-%m-%d %H:%M'))
    _logger.addHandler(_fh)

# 默认最大延迟窗口（小时），超过此时间不触发
DEFAULT_MAX_DELAY = 6
_l4_t = 0  # last L4 archive time

def _parse_cooldown(repeat):
    """解析repeat为冷却时间(比实际周期略短,防漂移)"""
    if repeat == 'once': return timedelta(days=999999)
    if repeat in ('daily', 'weekday'): return timedelta(hours=20)
    if repeat == 'weekly': return timedelta(days=6)
    if repeat == 'monthly': return timedelta(days=27)
    if repeat.startswith('every_'):
        try:
            parts = repeat.split('_')
            n = int(parts[1].rstrip('hdm'))
            u = parts[1][-1]
            if u == 'h': return timedelta(hours=n)
            if u == 'm': return timedelta(minutes=n)
            if u == 'd': return timedelta(days=n)
        except (ValueError, IndexError):
            pass  # fall through to warning below
    if repeat == 'cron':
        return timedelta(seconds=INTERVAL)
    _logger.warning(f'Unknown repeat type: {repeat}, fallback to 20h cooldown')
    return timedelta(hours=20)


def _match_cron(cron_expr: str, now: datetime) -> bool:
    """检查当前时间是否匹配 cron 表达式 (5 字段: M H DoM Mon DoW).

    Args:
        cron_expr: "M H DoM Mon DoW" 格式的 cron 字符串.
        now: 当前 datetime.

    Returns:
        True 表示匹配.
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        minutes, hours, dom, month, dow = parts
        if not _match_field(minutes, now.minute, 0, 59):
            return False
        if not _match_field(hours, now.hour, 0, 23):
            return False
        if not _match_field(dom, now.day, 1, 31):
            return False
        if not _match_field(month, now.month, 1, 12):
            return False
        if not _match_field(dow, now.isoweekday() % 7, 0, 6):
            return False
        return True
    except Exception:
        return False


def _match_field(pattern: str, value: int, lo: int, hi: int) -> bool:
    """匹配单个 cron 字段，支持 * , - / 语法.

    Args:
        pattern: cron 字段模式（如 "1-5", "*/15", "1,3,5"）.
        value: 当前时间值.
        lo: 合法范围下界.
        hi: 合法范围上界.

    Returns:
        True 表示匹配.
    """
    if pattern == "*":
        return True
    for part in pattern.split(","):
        step = 1
        if "/" in part:
            part, step_str = part.split("/", 1)
            step = int(step_str)
        if "-" in part:
            start, end = map(int, part.split("-", 1))
            if start <= value <= end and (value - start) % step == 0:
                return True
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False

def _last_run(tid, done_files):
    """找最近一次执行时间"""
    latest = None
    for df in done_files:
        if not df.endswith(f'_{tid}.md'): continue
        try:
            t = datetime.strptime(df[:15], '%Y-%m-%d_%H%M')
            if latest is None or t > latest: latest = t
        except: continue
    return latest

def check():
    # L4 archive cron (silent, every 12h)
    global _l4_t
    if _time.time() - _l4_t > 43200:
        _l4_t = _time.time()
        try:
            from zero_agent.memory.compress_session import batch_process
            from zero_agent.bots.shared.continue_cmd import _sessions_dir
            raw_dir = os.path.abspath(
                os.environ.get("ZA_MODEL_RESPONSES_DIR")
                or _sessions_dir
            )
            l4_dir = os.path.abspath(
                os.environ.get("ZA_L4_DIR")
                or str(project_root() / "memory" / "L4_raw_sessions")
            )
            r = batch_process(raw_dir, l4_dir, dry_run=False)
            print(f'[L4 cron] {r}')
        except Exception as e:
            _logger.error(f'L4 archive failed: {e}')

    if not os.path.isdir(TASKS): return None
    now = datetime.now()
    os.makedirs(DONE, exist_ok=True)
    done_files = set(os.listdir(DONE))
    for f in sorted(os.listdir(TASKS)):
        if not f.endswith('.json'): continue
        tid = f[:-5]
        try:
            with open(os.path.join(TASKS, f), encoding='utf-8') as fp:
                task = json.loads(fp.read())
        except Exception as e:
            _logger.error(f'JSON parse error for {f}: {e}')
            continue
        if not task.get('enabled', False): continue

        repeat = task.get('repeat', 'daily')
        cron_expr = task.get('cron', '')

        if cron_expr:
            # cron 模式：使用 cron 表达式匹配
            if not _match_cron(cron_expr, now):
                continue
            repeat = 'cron'
        else:
            # schedule 模式：HH:MM 时间匹配
            sched = task.get('schedule', '00:00')
            try:
                h, m = map(int, sched.split(':'))
            except Exception as e:
                _logger.error(f'Invalid schedule format in {f}: {sched!r} ({e})')
                continue

            # weekday任务：周末跳过
            if repeat == 'weekday' and now.weekday() >= 5:
                continue

            # 还没到schedule时间就跳过
            if now.hour < h or (now.hour == h and now.minute < m):
                continue

            # 执行窗口检查：超过max_delay小时则跳过
            max_delay = task.get('max_delay_hours', DEFAULT_MAX_DELAY)
            sched_minutes = h * 60 + m
            now_minutes = now.hour * 60 + now.minute
            if (now_minutes - sched_minutes) > max_delay * 60:
                _logger.info(f'SKIP {tid}: {now_minutes - sched_minutes}min past schedule, '
                             f'exceeds max_delay={max_delay}h')
                continue

        if cron_expr:
            max_delay = task.get('max_delay_hours', DEFAULT_MAX_DELAY)
            sched = cron_expr
        else:
            sched = task.get('schedule', '00:00')

        # 检查冷却
        last = _last_run(tid, done_files)
        cooldown = _parse_cooldown(repeat)
        if last and (now - last) < cooldown: continue

        # 触发
        _logger.info(f'TRIGGER {tid} (repeat={repeat}, schedule={sched}, '
                     f'last_run={last})')
        ts = now.strftime('%Y-%m-%d_%H%M')
        rpt = os.path.join(DONE, f'{ts}_{tid}.md')
        prompt = task.get('prompt', '')
        return (f'[定时任务] {tid}\n'
                f'[报告路径] {rpt}\n\n'
                f'先读 scheduled_task_sop 了解执行流程，然后执行以下任务：\n\n'
                f'{prompt}\n\n'
                f'完成后将执行报告写入 {rpt}。')

    return None
