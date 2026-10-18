"""只在配置边界校验，内部使用确定的配置契约。"""
from dataclasses import dataclass, field, fields
import math


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    enabled_groups: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    idle_seconds: float = 90
    join_seconds: float = 20
    debounce_seconds: float = 1.2
    max_wait_seconds: float = 3
    image_followup: bool = True
    user_batches_per_minute: int = 8
    group_batches_per_minute: int = 20
    join_batches_per_window: int = 2
    end_phrases: list[str] = field(default_factory=lambda: ["不用回了", "先这样", "不用回复了", "先不聊了"])
    debug_stats: bool = False

    @classmethod
    def load(cls, config):
        defaults = cls()
        values = {f.name: config.get(f.name, getattr(defaults, f.name)) for f in fields(cls)}
        for name in ("enabled", "image_followup", "debug_stats"):
            if not isinstance(values[name], bool):
                raise ValueError(f"{name} 必须为布尔值")
        for name in ("enabled_groups", "aliases", "end_phrases"):
            value = values[name]
            if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
                raise ValueError(f"{name} 必须为非空字符串列表")
            values[name] = list(dict.fromkeys(x.strip() for x in value))
        bounds = {"idle_seconds": (10, 300), "join_seconds": (0, 60),
                  "debounce_seconds": (0, 3), "max_wait_seconds": (0, 10)}
        for name, (low, high) in bounds.items():
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low}–{high} 之间")
        if values["debounce_seconds"] > values["max_wait_seconds"]:
            raise ValueError("最大收集等待不能小于短消息合并等待")
        if values["join_seconds"] > values["idle_seconds"]:
            raise ValueError("群友加入窗口不能大于本人空闲超时")
        for name in ("user_batches_per_minute", "group_batches_per_minute", "join_batches_per_window"):
            if type(values[name]) is not int or not 1 <= values[name] <= 120:
                raise ValueError(f"{name} 必须为 1–120 的整数")
        return cls(**values)
