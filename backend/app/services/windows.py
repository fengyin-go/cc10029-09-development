"""执行窗口与 Cron 展开：把调度计划里的执行窗口解析成明确的时间边界。

调度批次只有在窗口边界可判定时才能进入「待执行」；这里集中收敛所有窗口格式，
避免各调用方各自猜测时间格式。支持的写法：

- ``2026-11-17 09:00 ~ 2026-11-17 12:00``
- ``2026-11-17T09:00:00/2026-11-17T12:00:00``
- ``2026-11-17 09:00-12:00``
- ``09:00-12:00``（日期取基准日）
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


class WindowError(ValueError):
    """执行窗口缺失、格式不清晰或边界倒置时抛出。"""


_DATETIME_RE = re.compile(
    r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")


def format_dt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def parse_dt(value: object) -> datetime | None:
    """尽量解析调用方传来的时间值。"""
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace(" ", "T")):
        try:
            return datetime.fromisoformat(candidate).replace(microsecond=0)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class ExecutionWindow:
    """一段已判定边界、可直接用于生成批次的执行窗口。"""

    start: datetime
    end: datetime
    text: str
    date_explicit: bool

    @property
    def overnight(self) -> bool:
        return self.start.date() != self.end.date()

    def on_date(self, day: date) -> "ExecutionWindow":
        """把「每天 HH:MM-HH:MM」的窗口套到指定日期上。"""
        start = datetime.combine(day, self.start.time())
        end = datetime.combine(day, self.end.time())
        if end <= start or self.overnight:
            end += timedelta(days=1)
        return ExecutionWindow(
            start=start,
            end=end,
            text=f"{format_dt(start)} ~ {format_dt(end)}",
            date_explicit=False,
        )


def _tokenize(text: str) -> list[tuple[str, datetime | time]]:
    tokens: list[tuple[int, str, datetime | time]] = []
    masked = list(text)
    for match in _DATETIME_RE.finditer(text):
        year, month, day, hour, minute, second = match.groups()
        value = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0)
        )
        tokens.append((match.start(), "datetime", value))
        for index in range(match.start(), match.end()):
            masked[index] = " "
    remainder = "".join(masked)
    for match in _TIME_RE.finditer(remainder):
        hour, minute, second = match.groups()
        value = time(int(hour), int(minute), int(second or 0))
        tokens.append((match.start(), "time", value))
    tokens.sort(key=lambda item: item[0])
    return [(kind, value) for _, kind, value in tokens]


def parse_window(
    text: object,
    *,
    start_hint: object = None,
    end_hint: object = None,
    base_date: date | None = None,
) -> ExecutionWindow:
    """解析执行窗口；边界不清晰时抛出 :class:`WindowError`。"""
    base = base_date or date.today()
    hinted_start = parse_dt(start_hint)
    hinted_end = parse_dt(end_hint)
    for raw_hint, parsed_hint in ((start_hint, hinted_start), (end_hint, hinted_end)):
        if str(raw_hint or "").strip() and parsed_hint is None:
            raise WindowError(f"无法识别的窗口时间：{raw_hint}")
    if bool(hinted_start) != bool(hinted_end):
        raise WindowError("窗口开始与结束时间必须成对提供")
    if hinted_start and hinted_end:
        if hinted_end <= hinted_start:
            raise WindowError("窗口结束时间必须晚于开始时间")
        return ExecutionWindow(
            start=hinted_start,
            end=hinted_end,
            text=f"{format_dt(hinted_start)} ~ {format_dt(hinted_end)}",
            date_explicit=True,
        )

    raw = str(text or "").strip()
    if not raw:
        raise WindowError("缺少执行窗口或窗口边界")
    tokens = _tokenize(raw)
    if len(tokens) != 2:
        raise WindowError(f"无法从「{raw}」识别出唯一的开始与结束时间")

    first_kind, first_value = tokens[0]
    second_kind, second_value = tokens[1]

    if first_kind == "datetime" and second_kind == "datetime":
        start, end = first_value, second_value
        explicit = True
    elif first_kind == "datetime" and second_kind == "time":
        start = first_value
        end = datetime.combine(start.date(), second_value)
        if end <= start:
            end += timedelta(days=1)
        explicit = True
    elif first_kind == "time" and second_kind == "datetime":
        end = second_value
        start = datetime.combine(end.date(), first_value)
        if start >= end:
            start -= timedelta(days=1)
        explicit = True
    elif first_kind == "time" and second_kind == "time":
        start = datetime.combine(base, first_value)
        end = datetime.combine(base, second_value)
        if end <= start:
            end += timedelta(days=1)
        explicit = False
    else:  # pragma: no cover - 类型组合已被枚举完
        raise WindowError("执行窗口边界不清晰")

    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise WindowError("执行窗口边界不清晰")
    if end <= start:
        raise WindowError("窗口结束时间必须晚于开始时间")

    display = raw if tokens and len(raw) > 0 else f"{format_dt(start)} ~ {format_dt(end)}"
    return ExecutionWindow(
        start=start, end=end, text=display, date_explicit=explicit
    )


# ---------------------------------------------------------------------------
# Cron 展开
# ---------------------------------------------------------------------------

_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _parse_cron_field(part: str, low: int, high: int) -> set[int] | None:
    values: set[int] = set()
    for chunk in part.split(","):
        chunk = chunk.strip()
        if not chunk:
            return None
        step = 1
        if "/" in chunk:
            span, step_text = chunk.split("/", 1)
            if not step_text.isdigit() or int(step_text) <= 0:
                return None
            step = int(step_text)
        else:
            span = chunk
        if span in ("*", "?"):
            begin, finish = low, high
        elif "-" in span:
            left, right = span.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                return None
            begin, finish = int(left), int(right)
        elif span.isdigit():
            begin = finish = int(span)
        else:
            return None
        if begin < low or finish > high or begin > finish:
            return None
        values.update(range(begin, finish + 1, step))
    if high == 7 and 7 in values:
        values.discard(7)
        values.add(0)
    return values or None


def parse_cron(expression: object) -> tuple[set[int], set[int], set[int], set[int], set[int]] | None:
    """解析五段式 Cron；无法解析时返回 ``None``（不抛异常，交给调用方决定）。"""
    text = " ".join(str(expression or "").split())
    parts = text.split(" ") if text else []
    if len(parts) != 5:
        return None
    fields: list[set[int]] = []
    for part, (low, high) in zip(parts, _CRON_RANGES):
        parsed = _parse_cron_field(part, low, high)
        if parsed is None:
            return None
        fields.append(parsed)
    return tuple(fields)  # type: ignore[return-value]


def cron_occurrences(expression: object, after: datetime, count: int) -> list[datetime]:
    """返回 ``after`` 之后最近 ``count`` 个 Cron 触发时刻（按时间正序）。"""
    parsed = parse_cron(expression)
    if parsed is None or count <= 0:
        return []
    minutes, hours, days, months, weekdays = parsed
    dom_restricted = days != set(range(1, 32))
    dow_restricted = weekdays != set(range(0, 7))
    cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = cursor.date()
    last_day = day + timedelta(days=400)
    found: list[datetime] = []
    while day <= last_day and len(found) < count:
        dom_ok = day.day in days
        # Python: 周一=0；Cron: 周日=0
        dow_ok = ((day.weekday() + 1) % 7) in weekdays
        if dom_restricted and dow_restricted:
            matched = dom_ok or dow_ok
        else:
            matched = dom_ok and dow_ok
        if day.month in months and matched:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    moment = datetime.combine(day, time(hour, minute))
                    if moment >= cursor:
                        found.append(moment)
                        if len(found) >= count:
                            return found
        day += timedelta(days=1)
    return found
