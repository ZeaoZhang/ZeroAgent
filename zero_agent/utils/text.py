"""文本格式化工具函数.

smart_format: 对过长字符串进行首尾保留截断.
format_error: 从 traceback 提取 文件:行号:函数名 的紧凑错误描述.
json_default: JSON 序列化时的默认转换器（处理 set 等非标准类型）.
"""

import sys
import traceback
from typing import Any, Optional


def smart_format(
    data: Any,
    max_str_len: int = 100,
    omit_str: str = " ... ",
) -> str:
    """对过长字符串进行首尾保留截断.

    保留头部和尾部各一半 max_str_len 的内容，中间用 omit_str 替换.
    适用于日志输出和 UI 展示时限制字符串长度.

    Args:
        data: 待格式化的数据，非 str 类型会先转为 str.
        max_str_len: 保留的字符总数上限（不含省略符）.
        omit_str: 省略占位符.

    Returns:
        格式化后的字符串。若原串长度未超阈值则原样返回.

    Example:
        >>> smart_format("a" * 200, max_str_len=20)
        'aaaaaaaaaa ... aaaaaaaaaa'
    """
    if not isinstance(data, str):
        data = str(data)

    threshold = max_str_len + len(omit_str) * 2
    if len(data) < threshold:
        return data

    half = max_str_len // 2
    return f"{data[:half]}{omit_str}{data[-half:]}"


def format_error(exc: Optional[Exception] = None) -> str:
    """从异常或当前 traceback 中提取紧凑的单行错误描述.

    格式: ExceptionType: message @ filename:lineno, funcName -> `code line`

    Args:
        exc: 异常对象，若为 None 则使用 sys.exc_info() 获取当前异常.

    Returns:
        紧凑错误描述字符串.
    """
    if exc is None:
        _, exc_value, exc_tb = sys.exc_info()
    else:
        exc_value = exc
        exc_tb = exc.__traceback__

    if exc_value is None:
        return str(exc) if exc else "Unknown error"

    if exc_tb is None:
        return f"{type(exc_value).__name__}: {exc_value}"

    tb = traceback.extract_tb(exc_tb)
    if tb:
        frame = tb[-1]
        filename = frame.filename.split("/")[-1] if "/" in frame.filename else frame.filename
        line = frame.line or ""
        return (
            f"{type(exc_value).__name__}: {exc_value} "
            f"@ {filename}:{frame.lineno}, {frame.name} -> `{line}`"
        )
    return f"{type(exc_value).__name__}: {exc_value}"


def json_default(obj: Any) -> Any:
    """JSON 序列化时的默认类型转换器.

    处理 json.dumps 不支持的 Python 类型:
    - set → list
    - 其他不可序列化对象 → str

    Args:
        obj: 待转换的对象.

    Returns:
        可被 json 模块序列化的等价对象.
    """
    if isinstance(obj, set):
        return list(obj)
    return str(obj)
