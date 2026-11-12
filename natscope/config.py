# SPDX-FileCopyrightText: 2026 ZouNing
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""配置持久化：主题偏好、自定义 STUN 服务器、禁用列表、窗口状态。

向后兼容策略：读取时逐字段做类型校验，未知字段直接忽略，缺失字段走默认，
因此 v1.0 写下的 config.json（没有 schema 字段）能被 v1.1 正常加载，并在
下次保存时自动升级成 schema 2。
"""

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

#: 当前配置格式版本；写入文件时带上，便于将来做迁移
CURRENT_SCHEMA = 2
#: 允许的日志级别
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class Config:
    theme: str = THEME_AUTO
    custom_servers: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    last_dir: str = ""
    timeout: float = 1.5
    # ---- v1.1 新增（全部带默认值，老调用方无需改动）----
    #: 配置格式版本
    schema: int = CURRENT_SCHEMA
    #: 主窗口几何串，形如 "980x720+120+80"
    window_geometry: str = ""
    #: 紧凑模式（日志折叠、卡片收紧）
    compact: bool = False
    #: 是否探测网关端口映射能力
    gateway_enabled: bool = True
    #: 首次运行免责/提示是否已确认
    first_run_ack: bool = False
    #: 日志级别，取值见 LOG_LEVELS
    log_level: str = "INFO"

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
        """从磁盘读取配置；文件缺失、损坏、字段类型不符时一律回落到默认值。"""
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
            # ---- v1.1 字段：旧文件里没有这些键，取默认值即可 ----
            geom = data.get("window_geometry")
            if isinstance(geom, str) and len(geom) <= 64:
                cfg.window_geometry = geom
            for key in ("compact", "gateway_enabled", "first_run_ack"):
                if isinstance(data.get(key), bool):
                    setattr(cfg, key, data[key])
            level = data.get("log_level")
            if isinstance(level, str) and level.upper() in LOG_LEVELS:
                cfg.log_level = level.upper()
        # 无论源文件是哪个版本，内存中的对象总是当前 schema
        cfg.schema = CURRENT_SCHEMA
        return cfg

    def to_dict(self) -> dict[str, object]:
        """导出成可直接 json.dump 的字典（键名即文件格式）。"""
        return {
            "schema": CURRENT_SCHEMA,
            "theme": self.theme,
            "custom_servers": self.custom_servers,
            "disabled": self.disabled,
            "last_dir": self.last_dir,
            "timeout": self.timeout,
            "window_geometry": self.window_geometry,
            "compact": self.compact,
            "gateway_enabled": self.gateway_enabled,
            "first_run_ack": self.first_run_ack,
            "log_level": self.log_level,
        }

    def save(self, path: str | None = None) -> bool:
        """原子写入配置，返回是否成功。

        v1.0 这里是 ``except OSError: pass``——OneDrive 只读目录、磁盘满、
        杀软锁文件都会让用户设置**悄无声息地**丢失。现在失败必须能被调用方
        看见（GUI 可据此提示"配置未能保存"）。
        """
        path = path or CONFIG_PATH
        tmp = path + ".tmp"
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError):
            # 半截的 .tmp 留在盘上会干扰下次写入，尽力清掉
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False
        return True
