"""待下发任务清单：爬虫引擎消费调度批次清单后生成的可下发任务。

规则：

* 只消费「待执行 / 执行中」的调度批次，已完成或失败的批次不再重复下发；
* 下发时间必须落在批次声明的执行窗口内，否则拒绝生成任务；
* 任务清单与批次校验结论同事务写入，读取方不会读到半份数据；
* 批次校验结论变化时同步刷新对应任务的结论，两份台账始终一致。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.batches import BATCH_LEDGER
from app.services.windows import format_dt, parse_dt
from app.store import store

DISPATCH_LEDGER = "crawler_dispatches"
DISPATCH_STATUSES = ["待下发", "已下发", "已作废"]
CONSUMABLE_BATCH_STATUSES = ["待执行", "执行中"]


class DispatchError(Exception):
    """任务清单生成被业务规则拒绝。"""


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _batches() -> list[dict[str, Any]]:
    return store.ledger(BATCH_LEDGER)


def _dispatch_for_batch(batch_id: int) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in store.ledger(DISPATCH_LEDGER)
            if int(row.get("批次ID", 0)) == batch_id
        ),
        None,
    )


def _dispatch_time(batch: dict[str, Any], requested: Any) -> datetime:
    start = parse_dt(batch.get("窗口开始"))
    end = parse_dt(batch.get("窗口结束"))
    if start is None or end is None:
        raise DispatchError("批次缺少可判定的执行窗口，不能生成下发任务")
    moment = parse_dt(requested) or start
    if not (start <= moment <= end):
        raise DispatchError(
            f"下发时间 {format_dt(moment)} 不在批次窗口 "
            f"{format_dt(start)} ~ {format_dt(end)} 内"
        )
    return moment


def _build_dispatch(batch: dict[str, Any], moment: datetime) -> dict[str, Any]:
    rows = store.ledger(DISPATCH_LEDGER)
    return {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "任务清单编号": f"DISP-{batch.get('批次编号', '')}-{moment:%H%M}",
        "批次ID": int(batch.get("id", 0)),
        "批次编号": batch.get("批次编号", ""),
        "计划ID": int(batch.get("计划ID", 0)),
        "计划编号": batch.get("计划编号", ""),
        "关联任务": batch.get("关联任务", ""),
        "执行窗口": batch.get("执行窗口", ""),
        "窗口开始": batch.get("窗口开始", ""),
        "窗口结束": batch.get("窗口结束", ""),
        "触发来源": batch.get("触发来源", ""),
        "合并触发来源": list(batch.get("合并触发来源") or []),
        "下发时间": format_dt(moment),
        "状态": DISPATCH_STATUSES[0],
        "校验结论": batch.get("校验结论", ""),
        "生成时间": format_dt(moment),
        "pending": True,
    }


def generate_dispatches(
    values: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """消费调度批次清单生成待下发任务清单；已有任务的批次只同步结论。"""
    values = values or {}
    moment = now or _now()
    batch_id = values.get("批次ID") or values.get("batch_id")
    plan_id = values.get("计划ID") or values.get("plan_id")
    requested_time = values.get("下发时间") or values.get("dispatch_at")
    simulate_failure = bool(values.get("模拟失败") or values.get("simulate_failure"))

    with store.transaction():
        candidates = list(_batches())
        if batch_id is not None:
            try:
                wanted = int(batch_id)
            except (TypeError, ValueError) as error:
                raise DispatchError("批次ID 必须是整数") from error
            candidates = [row for row in candidates if int(row.get("id", 0)) == wanted]
            if not candidates:
                raise DispatchError(f"调度批次 {batch_id} 不存在")
        if plan_id is not None:
            try:
                wanted_plan = int(plan_id)
            except (TypeError, ValueError) as error:
                raise DispatchError("计划ID 必须是整数") from error
            candidates = [
                row for row in candidates if int(row.get("计划ID", 0)) == wanted_plan
            ]

        generated: list[dict[str, Any]] = []
        synced = 0
        for batch in sorted(candidates, key=lambda row: row.get("窗口开始", "")):
            existing = _dispatch_for_batch(int(batch.get("id", 0)))
            if existing is not None:
                if existing.get("校验结论") != batch.get("校验结论"):
                    existing["校验结论"] = batch.get("校验结论", "")
                    existing["触发来源"] = batch.get("触发来源", "")
                    existing["合并触发来源"] = list(batch.get("合并触发来源") or [])
                    synced += 1
                continue
            if batch.get("状态") not in CONSUMABLE_BATCH_STATUSES:
                continue
            try:
                moment_for_batch = _dispatch_time(batch, requested_time)
            except DispatchError:
                # 未指定具体批次时，跳过窗口内放不下下发时间的批次，不影响其他批次。
                if batch_id is None and plan_id is None:
                    continue
                raise
            dispatch = _build_dispatch(batch, moment_for_batch)
            store.append(DISPATCH_LEDGER, dispatch)
            generated.append(dispatch)
            if simulate_failure:
                # 任务清单与批次写入同属一份数据：落库失败就整体回退。
                raise DispatchError("待下发任务清单落库失败，事务已回退")

        total = len(generated)
        message = (
            f"已生成 {total} 条待下发任务" if total else "没有需要新生成的待下发任务"
        )
        if synced:
            message += f"，同步 {synced} 条批次校验结论"
        return {"items": generated, "created": total, "synced": synced, "message": message}


def sync_conclusions(*, now: datetime | None = None) -> int:
    """把调度批次清单的最新校验结论同步到待下发任务清单。"""
    moment = now or _now()
    updated = 0
    with store.transaction():
        batches = {
            int(row.get("id", 0)): row for row in store.ledger(BATCH_LEDGER)
        }
        for dispatch in store.ledger(DISPATCH_LEDGER):
            batch = batches.get(int(dispatch.get("批次ID", 0)))
            if batch is None:
                continue
            if dispatch.get("校验结论") != batch.get("校验结论"):
                dispatch["校验结论"] = batch.get("校验结论", "")
                dispatch["触发来源"] = batch.get("触发来源", "")
                dispatch["合并触发来源"] = list(batch.get("合并触发来源") or [])
                dispatch["同步时间"] = format_dt(moment)
                updated += 1
    return updated


def list_dispatches(
    *,
    plan_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows = sorted(
        store.ledger(DISPATCH_LEDGER),
        key=lambda row: (row.get("下发时间", ""), int(row.get("id", 0))),
    )
    if plan_id is not None:
        rows = [row for row in rows if int(row.get("计划ID", 0)) == plan_id]
    if status:
        rows = [row for row in rows if row.get("状态") == status]
    total = len(rows)
    start = (page - 1) * size
    return rows[start:start + size], total


def get_dispatch(dispatch_id: int) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in store.ledger(DISPATCH_LEDGER)
            if int(row.get("id", 0)) == dispatch_id
        ),
        None,
    )


def mark_dispatched(dispatch_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    """把待下发任务标记为已下发；下发时间仍必须落在窗口内。"""
    moment = now or _now()
    with store.transaction():
        dispatch = get_dispatch(dispatch_id)
        if dispatch is None:
            raise DispatchError(f"待下发任务 {dispatch_id} 不存在")
        if dispatch.get("状态") == "已下发":
            return dict(dispatch)
        if dispatch.get("状态") != "待下发":
            raise DispatchError(f"任务当前为「{dispatch.get('状态')}」，不能下发")
        _dispatch_time(dispatch, format_dt(moment))
        dispatch["状态"] = "已下发"
        dispatch["下发时间"] = format_dt(moment)
        dispatch["pending"] = False
        return dict(dispatch)
