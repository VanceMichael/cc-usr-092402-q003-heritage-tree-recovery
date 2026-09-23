"""时间工具：所有持久化时间统一为 UTC ISO-8601 字符串（…Z），可直接按字典序比较。"""
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse(ts):
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_or_none(ts):
    return None if ts is None else iso(parse(ts))
