"""配置持久化：主题偏好、自定义 STUN 服务器、禁用列表。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .servers import DEFAULT_SERVERS, StunServer, parse_server

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NatScope")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

THEME_AUTO = "auto"
THEME_LIGHT = "light"
THEME_DARK = "dark"


@dataclass
class Config:
    theme: str = THEME_AUTO
    custom_servers: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    last_dir: str = ""
    timeout: float = 1.5

    # ---- 服务器列表 ----

    def all_servers(self) -> list[StunServer]:
        """内置 + 自定义，去重，剔除被禁用项。"""
        out: list[StunServer] = []
        seen: set[str] = set()
        disabled = {d.lower() for d in self.disabled}
        for host, port in DEFAULT_SERVERS:
            s = StunServer(host, port)
            if s.key.lower() in disabled or s.key.lower() in seen:
                continue
            seen.add(s.key.lower())
            out.append(s)
        for raw in self.custom_servers:
            try:
                s = parse_server(raw)
            except ValueError:
                continue
            if s.key.lower() in disabled or s.key.lower() in seen:
                continue
            seen.add(s.key.lower())
            out.append(s)
        return out

    def add_server(self, raw: str) -> StunServer:
        """添加自定义服务器；已存在则直接返回。"""
        s = parse_server(raw)
        key = s.key.lower()
        if key in {d.lower() for d in self.disabled}:
            self.disabled = [d for d in self.disabled if d.lower() != key]
        if key not in {c.lower() for c in self.custom_servers} and key not in {
            f"{h}:{p}".lower() for h, p in DEFAULT_SERVERS
        }:
            self.custom_servers.append(s.key)
        return s

    def remove_server(self, key: str) -> None:
        low = key.lower()
        self.custom_servers = [c for c in self.custom_servers if c.lower() != low]
        if low not in {f"{h}:{p}".lower() for h, p in DEFAULT_SERVERS}:
            self.disabled = [d for d in self.disabled if d.lower() != low]
        else:
            if low not in {d.lower() for d in self.disabled}:
                self.disabled.append(key)

    def set_enabled(self, key: str, enabled: bool) -> None:
        low = key.lower()
        if enabled:
            self.disabled = [d for d in self.disabled if d.lower() != low]
        elif low not in {d.lower() for d in self.disabled}:
            self.disabled.append(key)

    # ---- 持久化 ----

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cfg
        if isinstance(data, dict):
            theme = data.get("theme")
            if theme in (THEME_AUTO, THEME_LIGHT, THEME_DARK):
                cfg.theme = theme
            cs = data.get("custom_servers")
            if isinstance(cs, list):
                cfg.custom_servers = [str(x) for x in cs if isinstance(x, str)]
            dis = data.get("disabled")
            if isinstance(dis, list):
                cfg.disabled = [str(x) for x in dis if isinstance(x, str)]
            ld = data.get("last_dir")
            if isinstance(ld, str):
                cfg.last_dir = ld
            to = data.get("timeout")
            if isinstance(to, (int, float)) and 0.3 <= to <= 10:
                cfg.timeout = float(to)
        return cfg

    def save(self, path: str | None = None) -> None:
        path = path or CONFIG_PATH
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "theme": self.theme,
                        "custom_servers": self.custom_servers,
                        "disabled": self.disabled,
                        "last_dir": self.last_dir,
                        "timeout": self.timeout,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, path)
        except OSError:
            pass
