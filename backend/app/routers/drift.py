"""数据漂移检测接口：漂移记录归并、采样样本登记、窗口归并与分组定位。

归并规则：按表、字段和采样窗口聚合重复记录，保留首次偏差、最近偏差、样本数和
结论版本；只对跨过噪声阈值的变化推进结论；重复采样幂等；中断后从失败窗口继续。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drift import DriftService

router = APIRouter(prefix="/api/drift", tags=["数据漂移检测"])

service = DriftService()

STATUSES = ["已记录", "已确认", "已处理", "已忽略"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    field: str | None = Query(default=None, description="按漂移字段检索"),
    status: str | None = Query(default=None, description="已记录、已确认、已处理、已忽略"),
    window_start: str | None = Query(default=None, description="检测时间起，如 2026-11-10"),
    window_end: str | None = Query(default=None, description="检测时间止，如 2026-11-12"),
    hide_noise: bool = Query(default=False, description="过滤噪声：隐藏未确认的采样噪声记录"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按字段、状态和检测时间窗口过滤漂移列表；已确认的历史偏差不因噪声过滤而丢失。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, field=field, status=status,
        window_start=window_start, window_end=window_end,
        hide_noise=hide_noise, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/locate")
def locate_entry(
    field: str | None = Query(default=None, description="按漂移字段检索"),
    status: str | None = Query(default=None, description="已记录、已确认、已处理、已忽略"),
    window_start: str | None = Query(default=None, description="检测时间起"),
    window_end: str | None = Query(default=None, description="检测时间止"),
    hide_noise: bool = Query(default=False, description="过滤噪声"),
    after: str | None = Query(default=None, description="锚点记录编号或 id，定位到下一组"),
) -> dict[str, Any]:
    """在筛选结果里定位最新未收口记录；带 after 时定位到下一组记录。"""
    result, ok, message = service.locate(
        field=field, status=status,
        window_start=window_start, window_end=window_end,
        hide_noise=hide_noise, after=after,
    )
    return {
        "ok": ok,
        "message": message,
        "entry": (result or {}).get("entry"),
        "group": (result or {}).get("group"),
    }


@router.get("/history", response_model=PageResult[dict])
def list_history(
    table: str | None = Query(default=None, description="按表名称检索"),
    field: str | None = Query(default=None, description="按漂移字段检索"),
    window: str | None = Query(default=None, description="按采样窗口检索，如 2026-11-11"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """采样历史：重复样本、噪声样本与原基线都保留可查。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_history(table=table, field=field, window=window, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据漂移检测清单：归并记录全量 + 采样历史全量。"""
    items, total = service.list_entries(page=1, size=10000)
    samples, sample_total = service.list_history(page=1, size=10000)
    return {
        "module": "drift",
        "total": total,
        "items": items,
        "sample_total": sample_total,
        "samples": samples,
    }


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记入口：带「当前数值」的按采样样本归并（幂等），否则按人工记录登记。"""
    entry, ok, message = service.create_entry(payload.values)
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/samples", response_model=ActionResult)
def register_sample(payload: EntryPayload) -> ActionResult:
    """暂存采样样本等待批量归并；同一窗口相同数值的重复样本幂等忽略。"""
    sample, ok, message = service.register_sample(payload.values)
    return ActionResult(ok=ok, message=message, entry=sample)


@router.post("/merge", response_model=ActionResult)
def merge_pending() -> ActionResult:
    """归并所有待归并窗口；中断后从失败窗口继续，已收口证据不重复累计。"""
    summary, ok, message = service.merge_pending()
    return ActionResult(ok=ok, message=message, entry=summary)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条漂移记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"漂移记录 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条漂移记录执行确认漂移、处理漂移、忽略漂移；状态按序推进，确认后冻结基线。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
