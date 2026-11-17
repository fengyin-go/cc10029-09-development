"""调度批次清单：把调度计划与执行窗口展开成按时间推进的批次序列。

批次由定时调度独占控制：

* 批次必须携带明确的执行窗口，窗口边界不清晰的计划不会产生「待执行」批次；
* 同一计划 + 同一窗口只保留一个批次，手动触发命中同一窗口时合并触发来源；
* 状态只能 待执行 → 执行中 → 已完成 / 失败 依次推进，禁止越级；
* 已完成的批次不能通过重试回退；
* 批次与序号索引在同一事务里写入，写入失败整体回退。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.windows import (
    WindowError,
    cron_occurrences,
    format_dt,
    parse_cron,
    parse_dt,
    parse_window,
)
from app.store import store

BATCH_LEDGER = "schedule_batches"
SEQUENCE_LEDGER = "schedule_batch_sequences"
BATCH_STATUSES = ["待执行", "执行中", "已完成", "失败"]
ACTIVE_STATUSES = ["待执行", "执行中"]
TRIGGER_SOURCES = ["定时触发", "手动触发"]
# 只允许相邻状态推进，重试也必须经过执行中，不能直接落到已完成。
TRANSITIONS = {
    "开始执行": ("待执行", "执行中"),
    "标记完成": ("执行中", "已完成"),
    "标记失败": ("执行中", "失败"),
    "重试执行": ("失败", "执行中"),
}


class BatchError(Exception):
    """批次生成或状态推进被业务规则拒绝。"""


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _plan(plan_id: int) -> dict[str, Any]:
    plan = store.find("schedule", plan_id)
    if plan is None:
        raise BatchError(f"调度计划 {plan_id} 不存在或已归档")
    return plan


def _batch_key(plan_id: int, start: datetime, end: datetime) -> str:
    return f"{plan_id}|{format_dt(start)}|{format_dt(end)}"


def _apply_transition(
    batch: dict[str, Any], target: str, *, at: datetime, note: str
) -> None:
    """按相邻状态推进一次，并把这一步写进生命周期。"""
    batch["状态"] = target
    batch["更新时间"] = format_dt(at)
    batch["pending"] = target in ACTIVE_STATUSES
    batch["abnormal"] = target == "失败"
    batch.setdefault("生命周期", []).append(
        {"状态": target, "时间": format_dt(at), "说明": note}
    )


def _new_batch(
    plan: dict[str, Any],
    window,
    *,
    trigger_source: str,
    now: datetime,
    conclusion: str,
) -> dict[str, Any]:
    plan_id = int(plan.get("id", 0))
    plan_no = str(plan.get("计划编号") or f"SCHE-{plan_id:04d}")
    start = window.start
    created = now
    return {
        "id": 0,  # 入库时分配
        "批次编号": f"BATCH-{plan_no}-{start:%Y%m%d%H%M}",
        "批次键": "",
        "计划ID": plan_id,
        "计划编号": plan_no,
        "关联任务": str(plan.get("关联任务") or ""),
        "Cron表达式": str(plan.get("Cron表达式") or ""),
        "执行窗口": f"{format_dt(start)} ~ {format_dt(window.end)}",
        "窗口开始": format_dt(start),
        "窗口结束": format_dt(window.end),
        "触发来源": trigger_source,
        "主触发来源": trigger_source,
        "合并触发来源": [trigger_source],
        "状态": BATCH_STATUSES[0],
        "重试次数": 0,
        "校验结论": conclusion,
        "创建时间": format_dt(created),
        "更新时间": format_dt(created),
        "pending": True,
        "abnormal": False,
        "生命周期": [{
            "状态": BATCH_STATUSES[0],
            "时间": format_dt(created),
            "说明": "批次已生成，窗口边界校验通过",
        }],
    }


def _next_id() -> int:
    rows = store.ledger(BATCH_LEDGER)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _find_existing(plan_id: int, start: datetime, end: datetime) -> dict[str, Any] | None:
    key = _batch_key(plan_id, start, end)
    for row in store.ledger(BATCH_LEDGER):
        if row.get("批次键") == key:
            return row
    return None


def _merge_trigger(batch: dict[str, Any], trigger_source: str, at: datetime) -> bool:
    """把新的触发来源并入既有批次，返回是否发生合并。"""
    merged = list(batch.get("合并触发来源") or [batch.get("主触发来源", "定时触发")])
    if trigger_source in merged:
        return False
    merged.append(trigger_source)
    batch["合并触发来源"] = merged
    batch["触发来源"] = "+".join(merged)
    batch["更新时间"] = format_dt(at)
    batch.setdefault("生命周期", []).append({
        "状态": batch.get("状态", BATCH_STATUSES[0]),
        "时间": format_dt(at),
        "说明": f"{trigger_source}命中同一执行窗口，合并到既有批次",
    })
    return True


def _occurrence_windows(
    plan: dict[str, Any],
    *,
    now: datetime,
    count: int,
    start_hint: Any = None,
    end_hint: Any = None,
) -> tuple[list[Any], str]:
    """展开计划的执行窗口，返回窗口列表与校验结论；边界不清晰时抛 BatchError。"""
    if start_hint or end_hint:
        try:
            window = parse_window(
                plan.get("执行窗口"),
                start_hint=start_hint,
                end_hint=end_hint,
                base_date=now.date(),
            )
        except WindowError as error:
            raise BatchError(f"窗口边界不清晰：{error}") from error
        return [window], "窗口边界有效（调用方指定），批次可进入待执行"

    try:
        window = parse_window(plan.get("执行窗口"), base_date=now.date())
    except WindowError as error:
        raise BatchError(f"窗口边界不清晰：{error}") from error

    cron_text = str(plan.get("Cron表达式") or "").strip()
    if parse_cron(cron_text) is None:
        raise BatchError(f"Cron 表达式无法解析：{cron_text or '（空）'}")

    if window.date_explicit:
        # 明确写了日期的窗口就是一条批次，重复触发由批次键去重。
        probe = cron_occurrences(cron_text, window.start - timedelta(minutes=1), 1)
        if probe and probe[0] > window.end:
            raise BatchError("Cron 表达式与执行窗口对不上，窗口边界不清晰")
        return [window], "窗口边界有效（显式日期），批次可进入待执行"

    occurrences = cron_occurrences(cron_text, now - timedelta(minutes=1), count)
    if not occurrences:
        raise BatchError("Cron 表达式在未来一段时间内没有触发时刻，无法展开批次序列")
    windows = [window.on_date(moment.date()) for moment in occurrences]
    return windows, "窗口边界有效（按 Cron 展开），批次可进入待执行"


def generate_sequence(
    plan_id: int,
    values: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    sync_ledger: bool = True,
) -> dict[str, Any]:
    """生成或复用一批调度批次；返回值中的 items 是本次涉及的批次清单。

    整个过程在一个事务里完成：批次行、序号索引、爬虫引擎的待下发任务清单一起
    提交，任何一步失败都回退，不会给待下发任务清单留下半份数据。重复送交只读取
    已存在的批次，不重复写入。
    """
    values = values or {}
    moment = parse_dt(values.get("触发时间")) or now or _now()
    trigger_source = str(values.get("触发来源") or "定时触发").strip()
    if trigger_source not in TRIGGER_SOURCES:
        raise BatchError(f"触发来源「{trigger_source}」不受支持")
    try:
        count = int(values.get("批次数量") or 3)
    except (TypeError, ValueError) as error:
        raise BatchError("批次数量必须是整数") from error
    count = max(1, min(count, 20))
    start_hint = values.get("窗口开始") or values.get("window_start")
    end_hint = values.get("窗口结束") or values.get("window_end")
    simulate_failure = bool(values.get("模拟失败") or values.get("simulate_failure"))

    with store.transaction():
        plan = _plan(plan_id)
        if plan.get("status") == "已停用":
            raise BatchError("调度计划已停用，不能生成待执行批次")
        windows, conclusion = _occurrence_windows(
            plan, now=moment, count=count, start_hint=start_hint, end_hint=end_hint
        )
        items: list[dict[str, Any]] = []
        created = 0
        merged = 0
        for window in windows:
            existing = _find_existing(plan_id, window.start, window.end)
            if existing is not None:
                if _merge_trigger(existing, trigger_source, moment):
                    merged += 1
                items.append(existing)
                continue
            batch = _new_batch(
                plan,
                window,
                trigger_source=trigger_source,
                now=moment,
                conclusion=conclusion,
            )
            batch["批次键"] = _batch_key(plan_id, window.start, window.end)
            batch["id"] = _next_id()
            store.append(BATCH_LEDGER, batch)
            store.append(SEQUENCE_LEDGER, {
                "id": len(store.ledger(SEQUENCE_LEDGER)) + 1,
                "计划ID": plan_id,
                "计划编号": batch["计划编号"],
                "批次编号": batch["批次编号"],
                "窗口开始": batch["窗口开始"],
                "窗口结束": batch["窗口结束"],
                "触发来源": trigger_source,
                "创建时间": format_dt(moment),
            })
            items.append(batch)
            created += 1
        items.sort(key=lambda row: (row.get("窗口开始", ""), int(row.get("id", 0))))

        synced = 0
        if sync_ledger:
            # 同一事务内消费批次清单，生成爬虫引擎的待下发任务清单；任何一步失败
            # 都会连同批次一起回退，任务清单不会读到半份批次。
            from app.services.dispatches import DispatchError, generate_dispatches

            try:
                result = generate_dispatches({"计划ID": plan_id}, now=moment)
            except DispatchError as error:
                raise BatchError(f"待下发任务清单同步失败：{error}") from error
            synced = result["created"]
            if simulate_failure:
                raise BatchError("批次与任务清单落库失败，事务已回退")

        message = (
            f"已生成 {created} 个调度批次"
            if created
            else "同一执行窗口已存在批次，未重复写入"
        )
        if merged:
            message += f"，合并 {merged} 次重复触发"
        if synced:
            message += f"，同步 {synced} 条待下发任务"
        return {
            "items": items,
            "created": created,
            "merged": merged,
            "synced": synced,
            "message": message,
        }


def advance(batch_id: int, action: str, *, now: datetime | None = None) -> dict[str, Any]:
    """按动作推进单个批次状态；越级或回退都会被拒绝。"""
    action = str(action or "").strip()
    if action not in TRANSITIONS:
        raise BatchError(f"动作「{action}」不属于调度批次可执行范围")
    source, target = TRANSITIONS[action]
    moment = now or _now()
    with store.transaction():
        batch = next(
            (row for row in store.ledger(BATCH_LEDGER) if int(row.get("id", 0)) == batch_id),
            None,
        )
        if batch is None:
            raise BatchError(f"调度批次 {batch_id} 不存在")
        current = str(batch.get("状态"))
        if current == "已完成":
            raise BatchError("批次已完成，生命周期不允许回退或重试")
        if current != source:
            raise BatchError(
                f"批次当前为「{current}」，不能直接执行「{action}」；状态需依次推进"
            )
        at = moment
        if action == "标记完成":
            window_end = parse_dt(batch.get("窗口结束"))
            if window_end and window_end > moment:
                at = window_end
        if action == "重试执行":
            batch["重试次数"] = int(batch.get("重试次数", 0)) + 1
        _apply_transition(batch, target, at=at, note=f"定时调度执行「{action}」")
        return dict(batch)


def sync_by_clock(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """按当前时间把批次沿相邻状态依次推进；每次最多走到窗口对应的状态。

    待执行 → 执行中（窗口开始已到）→ 已完成（窗口结束已过），不会直接跳到已完成。
    """
    moment = now or _now()
    changed: list[dict[str, Any]] = []
    with store.transaction():
        for batch in store.ledger(BATCH_LEDGER):
            start = parse_dt(batch.get("窗口开始"))
            end = parse_dt(batch.get("窗口结束"))
            if start is None or end is None:
                continue
            touched = False
            if batch.get("状态") == "待执行" and start <= moment:
                _apply_transition(
                    batch, "执行中", at=start,
                    note="窗口开始时间已到，批次进入执行中",
                )
                touched = True
            if batch.get("状态") == "执行中" and end <= moment:
                _apply_transition(
                    batch, "已完成", at=end,
                    note="窗口结束时间已过，批次按既有结果标记完成",
                )
                touched = True
            if touched:
                changed.append(dict(batch))
    return changed


def list_batches(
    *,
    plan_id: int | None = None,
    status: str | None = None,
    trigger_source: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = sorted(
        store.ledger(BATCH_LEDGER),
        key=lambda row: (row.get("窗口开始", ""), int(row.get("id", 0))),
    )
    if plan_id is not None:
        rows = [row for row in rows if int(row.get("计划ID", 0)) == plan_id]
    if status:
        rows = [row for row in rows if row.get("状态") == status]
    if trigger_source:
        rows = [
            row for row in rows
            if trigger_source in (row.get("合并触发来源") or [row.get("触发来源")])
        ]
    total = len(rows)
    start = (page - 1) * size
    return rows[start:start + size], total


def get_batch(batch_id: int) -> dict[str, Any] | None:
    return next(
        (row for row in store.ledger(BATCH_LEDGER) if int(row.get("id", 0)) == batch_id),
        None,
    )


def plan_sequence(plan_id: int) -> dict[str, Any]:
    """按计划返回批次序列、窗口与当前状态，供页面核对触发来源合并情况。"""
    plan = store.find("schedule", plan_id)
    if plan is None:
        raise BatchError(f"调度计划 {plan_id} 不存在或已归档")
    rows = [
        row for row in store.ledger(BATCH_LEDGER)
        if int(row.get("计划ID", 0)) == plan_id
    ]
    rows.sort(key=lambda row: (row.get("窗口开始", ""), int(row.get("id", 0))))
    merged = [row for row in rows if len(row.get("合并触发来源") or []) > 1]
    return {
        "plan": plan,
        "items": rows,
        "total": len(rows),
        "counts": {
            status: sum(1 for row in rows if row.get("状态") == status)
            for status in BATCH_STATUSES
        },
        "merged_count": len(merged),
    }
