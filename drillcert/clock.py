"""时间工具。

服务内部统一使用时区感知的 UTC datetime。业务上的「响应截止时间」
一律锚定派发时刻（*_at 落库即定），重复提交不会刷新时钟。
"""

from datetime import datetime, timezone


def now():
    """当前时间，允许测试通过 set_clock 替换。"""
    override = _CLOCK.get()
    return override() if override is not None else datetime.now(timezone.utc)


def parse(value):
    """解析 ISO-8601 字符串，朴素时间按 UTC 处理；非法输入抛 ValueError。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    """datetime -> 带 Z 后缀的 ISO-8601 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Clock:
    def __init__(self):
        self._fn = None

    def get(self):
        return self._fn

    def set(self, fn):
        self._fn = fn

    def reset(self):
        self._fn = None


_CLOCK = _Clock()


def set_clock(fn):
    """固定时钟，fn 返回时区感知 datetime（仅供测试）。"""
    _CLOCK.set(fn)


def reset_clock():
    _CLOCK.reset()
