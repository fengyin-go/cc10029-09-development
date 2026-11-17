"""定时调度 时间线接口：返回作业时间序列结构，并维护调度批次清单。

批次序列接口把计划和执行窗口展开成批次清单，状态按时间顺序推进；批次清单由
定时调度独占控制，爬虫引擎只读取、消费。原有时间线、导出、登记和动作接口保持
既有返回结构，新能力以独立路由挂载，不改变调用契约。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, BatchActionPayload, EntryPayload
from app.services.batches import (
    BATCH_STATUSES,
    BatchError,
    advance,
    generate_sequence,
    get_batch,
    list_batches,
    plan_sequence,
    sync_by_clock,
)
from app.services.schedule import ScheduleService

router = APIRouter(prefix="/api/schedule", tags=["定时调度"])
service = ScheduleService()
LIST_FIELDS = ["计划编号", "关联任务", "Cron表达式", "执行窗口", "重试策略", "失败通知", "最大并发", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]
BATCH_CSV_FIELDS = [
    "批次编号", "计划编号", "关联任务", "Cron表达式", "执行窗口",
    "触发来源", "主触发来源", "合并触发来源", "状态", "重试次数",
    "校验结论", "创建时间", "更新时间",
]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _batch_csv_cell(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value)
    return str(value if value is not None else "")


@router.get("")
def list_entries() -> dict[str, Any]:
    items = service.get_timeline()
    return {"items": items}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.get_timeline()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=schedule.csv"})


@router.get("/plans")
def list_plans() -> dict[str, Any]:
    """批次面板用的计划下拉：只暴露编号、任务与状态。"""
    items = [
        {
            "id": int(row.get("id", 0)),
            "计划编号": row.get("计划编号", ""),
            "关联任务": row.get("关联任务", ""),
            "status": row.get("status", ""),
            "执行窗口": row.get("执行窗口", ""),
            "Cron表达式": row.get("Cron表达式", ""),
        }
        for row in service.list_rows()
    ]
    return {"items": items}


@router.get("/batches")
def list_batch_entries(
    plan_id: int | None = Query(default=None, description="按调度计划过滤批次"),
    status: str | None = Query(default=None, description="待执行、执行中、已完成、失败"),
    trigger_source: str | None = Query(default=None, description="定时触发、手动触发"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """调度批次清单：待执行、执行中、已完成按时间顺序推进。"""
    if status and status not in BATCH_STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在批次状态序列里")
    items, total = list_batches(
        plan_id=plan_id, status=status, trigger_source=trigger_source, page=page, size=size
    )
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/batches/export")
def export_batches(
    plan_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    trigger_source: str | None = Query(default=None),
) -> StreamingResponse:
    """下载调度批次清单 CSV；必须声明在 /batches/{batch_id} 之前。"""
    items, _ = list_batches(
        plan_id=plan_id, status=status, trigger_source=trigger_source, page=1, size=1000000
    )
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *BATCH_CSV_FIELDS])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in BATCH_CSV_FIELDS)]:
            text = _batch_csv_cell(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="schedule-batches.csv"'},
    )


@router.post("/batches")
def create_batches(plan_id: int = Query(description="调度计划 ID"), payload: EntryPayload | None = None) -> ActionResult:
    """按计划展开批次序列；重复送交同一窗口只返回已存在批次。"""
    values = payload.values if payload else {}
    try:
        result = generate_sequence(plan_id, values)
    except BatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(
        ok=True,
        message=result["message"],
        entry={
            "items": result["items"],
            "created": result["created"],
            "merged": result["merged"],
            "synced": result["synced"],
        },
    )


@router.post("/batches/sync")
def sync_batches() -> ActionResult:
    """按当前时间推进批次状态：待执行 → 执行中 → 已完成，不跳级。"""
    changed = sync_by_clock()
    return ActionResult(
        ok=True,
        message=f"已推进 {len(changed)} 个调度批次",
        entry={"items": changed},
    )


@router.get("/batches/{batch_id}")
def get_batch_entry(batch_id: int) -> dict[str, Any]:
    batch = get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"调度批次 {batch_id} 不存在")
    return batch


@router.post("/batches/{batch_id}/actions")
def run_batch_action(batch_id: int, payload: BatchActionPayload) -> ActionResult:
    """批次状态只允许按生命周期依次推进，已完成批次不能再重试。"""
    try:
        batch = advance(batch_id, payload.action)
    except BatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message=f"批次已执行「{payload.action}」", entry=batch)


@router.get("/{entry_id}/batches")
def get_plan_batches(entry_id: int) -> dict[str, Any]:
    """按调度计划查看批次序列、窗口和当前状态，核对手动/定时触发是否合并。"""
    try:
        return plan_sequence(entry_id)
    except BatchError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"调度计划 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="调度计划已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
