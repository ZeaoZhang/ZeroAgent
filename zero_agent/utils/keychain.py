"""Keychain — XOR 加密凭据存储.

将敏感凭据以 XOR 加密方式持久化到磁盘，通过 SecretStr 类型
防止 `print` / `repr` 意外泄露明文。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
from typing import Dict, List


def _get_mask() -> bytes:
    """基于用户名生成 XOR 密钥.

    Returns:
        32 字节的 XOR 密钥.
    """
    import getpass

    try:
        user = os.getlogin()
    except OSError:
        user = getpass.getuser()
    return hashlib.sha256(f"{user}@zero_agent_keychain".encode()).digest()


_MASK = _get_mask()


def _xor(data: bytes) -> bytes:
    """对数据应用 XOR 混淆.

    Args:
        data: 原始数据.

    Returns:
        XOR 混淆后的数据.
    """
    return bytes(b ^ _MASK[i % len(_MASK)] for i, b in enumerate(data))


class SecretStr:
    """封装敏感字符串，防止 print/repr 意外泄露明文.

    Attributes:
        _name: 凭据名称.
        _val: 凭据明文值.
    """

    def __init__(self, name: str, val: str) -> None:
        """初始化 SecretStr.

        Args:
            name: 凭据名称.
            val: 凭据明文值.
        """
        self._name = name
        self._val = val

    def use(self) -> str:
        """获取明文值.

        调用方需自行负责不将返回值打印到日志/UI.

        Returns:
            凭据明文.
        """
        return self._val

    def __repr__(self) -> str:
        n = len(self._val)
        if n <= 4:
            preview = "***"
        elif n <= 16:
            preview = f"{self._val[:3]}···{self._val[-3:]}"
        elif n <= 40:
            preview = f"{self._val[:6]}···{self._val[-6:]} len={n}"
        else:
            preview = f"{self._val[:10]}···{self._val[-6:]} len={n}"
        return f"SecretStr({self._name}={preview})"

    __str__ = __repr__


class Keychain:
    """XOR 加密的凭据存储.

    持久化到 ~/.zero_agent_keychain.enc，使用 XOR+用户哈希
    进行基本混淆（非强加密，仅防止明文泄露）.

    Usage:
        kc = Keychain()
        kc.set("api_key", "sk-xxxx")
        api_key = kc.api_key.use()  # 返回明文
        kc.ls()  # 列出所有凭据名

    Attributes:
        _store: 凭据名称 → SecretStr 的映射.
        _path: 持久化文件路径.
    """

    _DEFAULT_PATH = pathlib.Path.home() / ".zero_agent_keychain.enc"

    def __init__(self, path: str = "") -> None:
        """初始化 Keychain.

        Args:
            path: 持久化文件路径，空字符串使用默认路径.
        """
        self._path = pathlib.Path(path) if path else self._DEFAULT_PATH
        self._store: Dict[str, SecretStr] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘加载凭据."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(_xor(self._path.read_bytes()))
            self._store = {k: SecretStr(k, v) for k, v in raw.items()}
        except Exception as e:
            print(f"[keychain] 加载 {self._path} 失败: {e}")
            print(f"[keychain] 备份旧文件为 .bak，从空 keychain 开始")
            bak = self._path.with_suffix(".enc.bak")
            if bak.exists():
                bak.unlink()
            self._path.rename(bak)

    def _save(self) -> None:
        """持久化到磁盘."""
        raw = {k: v.use() for k, v in self._store.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(_xor(json.dumps(raw).encode()))

    def set(self, name: str, value: str = "", *, file: str = "") -> None:
        """设置凭据.

        Args:
            name: 凭据名称.
            value: 凭据明文值（与 file 互斥，优先 file）.
            file: 包含凭据值的文件路径.
        """
        if file:
            value = pathlib.Path(file).read_text().strip()
        self._store[name] = SecretStr(name, value)
        self._save()

    def ls(self) -> List[str]:
        """列出所有凭据名称.

        Returns:
            凭据名称列表.
        """
        return list(self._store.keys())

    def remove(self, name: str) -> None:
        """删除指定凭据.

        Args:
            name: 凭据名称.

        Raises:
            KeyError: 凭据不存在.
        """
        if name not in self._store:
            raise KeyError(f"凭据不存在: {name}")
        del self._store[name]
        self._save()

    def __getattr__(self, name: str) -> SecretStr:
        """通过属性访问凭据.

        Args:
            name: 凭据名称.

        Returns:
            对应的 SecretStr.

        Raises:
            KeyError: 凭据不存在.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._store:
            raise KeyError(f"凭据不存在: {name}. 可用: {self.ls()}")
        return self._store[name]

    def __repr__(self) -> str:
        return f"Keychain({len(self._store)} secrets: {', '.join(self._store.keys())})"
