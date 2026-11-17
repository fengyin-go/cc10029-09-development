"""爬虫引擎 时间线接口：返回作业时间序列结构，并维护待下发任务清单。

待下发任务清单由爬虫引擎消费调度批次清单生成，下发时间必须落在批次执行窗口内；
原有时间线、导出、登记和动作接口保持既有返回结构。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.crawler import CrawlerService
from app.services.dispatches import (
    DispatchError,
    generate_dispatches,
    get_dispatch,
    list_dispatches,
    mark_dispatched,
    sync_conclusions,
)

router = APIRouter(prefix="/api/crawler", tags=["爬虫引擎"])
service = CrawlerService()
LIST_FIELDS = ["任务编号", "任务名称", "关联站点", "关联规则", "并发线程", "调度周期", "超时秒数", "下发批次"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]
DISPATCH_CSV_FIELDS = [
    "任务清单编号", "批次编号", "计划编号", "关联任务", "执行窗口",
    "触发来源", "合并触发来源", "下发时间", "状态", "校验结论", "生成时间",
]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _csv_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value)
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
                             headers={"Content-Disposition": "attachment; filename=crawler.csv"})


@router.get("/dispatches")
def list_dispatch_entries(
    plan_id: int | None = Query(default=None, description="按调度计划过滤任务"),
    status: str | None = Query(default=None, description="待下发、已下发、已作废"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """待下发任务清单：只读取已完整提交的调度批次，不会读到半份数据。"""
    items, total = list_dispatches(plan_id=plan_id, status=status, page=page, size=size)
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/dispatches/export")
def export_dispatches(
    plan_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    items, _ = list_dispatches(plan_id=plan_id, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *DISPATCH_CSV_FIELDS])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in DISPATCH_CSV_FIELDS)]:
            text = _csv_text(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="crawler-dispatches.csv"'},
    )


@router.post("/dispatches")
def create_dispatches(payload: EntryPayload | None = None) -> ActionResult:
    """消费调度批次清单生成待下发任务；下发时间必须落在批次窗口内。"""
    values = payload.values if payload else {}
    try:
        result = generate_dispatches(values)
    except DispatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(
        ok=True,
        message=result["message"],
        entry={"items": result["items"], "created": result["created"], "synced": result["synced"]},
    )


@router.post("/dispatches/sync")
def sync_dispatch_conclusions() -> ActionResult:
    """把批次校验结论同步到任务清单，保证两份台账一致。"""
    updated = sync_conclusions()
    return ActionResult(ok=True, message=f"已同步 {updated} 条任务结论")


@router.get("/dispatches/{dispatch_id}")
def get_dispatch_entry(dispatch_id: int) -> dict[str, Any]:
    dispatch = get_dispatch(dispatch_id)
    if dispatch is None:
        raise HTTPException(status_code=404, detail=f"待下发任务 {dispatch_id} 不存在")
    return dispatch


@router.post("/dispatches/{dispatch_id}/actions")
def run_dispatch_action(dispatch_id: int, payload: ActionPayload) -> ActionResult:
    """下发任务：下发时间仍必须落在批次窗口内。"""
    if payload.action.strip() != "下发任务":
        return ActionResult(ok=False, message=f"动作「{payload.action}」不属于待下发任务可执行范围")
    try:
        dispatch = mark_dispatched(dispatch_id)
    except DispatchError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message="任务已下发", entry=dispatch)


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"爬虫任务 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="爬虫任务已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
