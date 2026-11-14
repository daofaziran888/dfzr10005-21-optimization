"""数据漂移检测业务规则：采样归并、冻结基线、噪声阈值与状态流转都收在这里。

归并断点：原来的 create_entry 每来一个采样窗口就追加一条新记录，同一漂移字段
在连续窗口里反复生成记录，历史均值不断被最新值覆盖，健康度报告也分不清真实
漂移和采样噪声。现在同一「表名称 + 漂移字段」的连续采样会归并到最新的未收口
记录上：保留首次偏差、最近偏差、样本数与结论版本，只对跨过噪声阈值的变化推进
结论；重复采样按指纹幂等忽略，并发窗口以冻结基线为准，中断后从失败窗口继续，
已收口的证据不重复累计。归并结论会同步到健康度报告（只保留最终字段结论）和
查询性能报告（受影响表）。
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "drift"
SAMPLE_MODULE = "drift_samples"
WINDOW_MODULE = "drift_windows"
SCORE_MODULE = "score"
BENCHMARK_MODULE = "benchmark"

REQUIRED_FIELDS = ["表名称", "漂移字段"]
STATUS_ORDER = ["已记录", "已确认", "已处理", "已忽略"]
ACTION_RULES = {"确认漂移": "已记录", "处理漂移": "已确认", "忽略漂移": "已处理"}
NEGATIVE_ACTIONS = ["忽略漂移"]
OPEN_STATUS = "已记录"
FROZEN_STATUSES = {"已确认", "已处理", "已忽略"}  # 证据收口：不再被新采样改写

# 归并口径：窗口长度、噪声阈值、归并顺序都可按需调整，历史样本始终保留可查。
WINDOW_HOURS = 24  # 采样窗口长度：24 小时即按自然天归并
NOISE_THRESHOLD = 0.20  # 噪声阈值：|偏差比例| 低于 20% 视为采样噪声
MERGE_ORDER = "采样窗口升序"

CONCLUSION_REAL = "真实漂移"
CONCLUSION_NOISE = "采样噪声"
CONCLUSION_WATCH = "观察中"

_MERGE_LOCK = threading.Lock()  # 两个窗口同时写同一字段时串行化，以冻结基线为准


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_window_bound(value: Any, *, is_end: bool) -> datetime | None:
    """解析时间窗口边界；只给日期时，结束边界按当天 23:59:59 收口。"""
    text = str(value or "").strip()
    if not text:
        return None
    moment = _parse_time(text)
    if moment is not None and is_end and len(text) <= 10:
        moment = moment.replace(hour=23, minute=59, second=59)
    return moment


def _time_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _window_key(moment: datetime) -> str:
    """把检测时间落到采样窗口；24 小时窗口即自然天。"""
    if WINDOW_HOURS >= 24:
        return moment.strftime("%Y-%m-%d")
    hour = (moment.hour // WINDOW_HOURS) * WINDOW_HOURS
    return f"{moment.strftime('%Y-%m-%d')} {hour:02d}:00"


def _window_sort_key(window: str) -> tuple[datetime, str]:
    return (_parse_time(window) or datetime.min, window)


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_ratio(value: Any) -> float | None:
    """解析偏差比例：兼容 18%、18、0.18 三种写法，统一返回小数。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    is_percent = text.endswith("%")
    number = _to_float(text.rstrip("%"))
    if number is None:
        return None
    if is_percent or abs(number) > 1:
        return number / 100.0
    return number


def _format_ratio(ratio: float) -> str:
    text = f"{ratio * 100:.1f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _format_number(value: float) -> str:
    return f"{value:.2f}"


def _direction(ratio: float) -> str:
    if ratio > 0.0001:
        return "上升"
    if ratio < -0.0001:
        return "下降"
    return "持平"


class DriftService:
    # ---------- 列表、筛选与定位 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        field: str | None = None,
        status: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        hide_noise: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = self._filter_rows(
            keyword=keyword, field=field, status=status,
            window_start=window_start, window_end=window_end, hide_noise=hide_noise,
        )
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _filter_rows(
        self,
        *,
        keyword: str | None,
        field: str | None,
        status: str | None,
        window_start: str | None,
        window_end: str | None,
        hide_noise: bool,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if field:
            rows = [row for row in rows if field in str(row.get("漂移字段", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        start_dt = _parse_window_bound(window_start, is_end=False)
        end_dt = _parse_window_bound(window_end, is_end=True)
        if start_dt or end_dt:
            rows = [row for row in rows if self._in_window(row, start_dt, end_dt)]
        if hide_noise:
            # 只滤掉未确认的噪声记录；已确认的历史偏差保留检测时间和原基线，不丢失
            rows = [row for row in rows if not self._is_noise_row(row)]
        return rows

    @staticmethod
    def _in_window(row: dict[str, Any], start_dt: datetime | None, end_dt: datetime | None) -> bool:
        moment = _parse_time(row.get("检测时间"))
        if moment is None:
            return False
        if start_dt and moment < start_dt:
            return False
        if end_dt and moment > end_dt:
            return False
        return True

    @staticmethod
    def _is_noise_row(row: dict[str, Any]) -> bool:
        return row.get("结论") == CONCLUSION_NOISE and row.get("status") == OPEN_STATUS

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def locate(
        self,
        *,
        field: str | None = None,
        status: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        hide_noise: bool = False,
        after: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        """在筛选结果里定位最新未收口记录；带 after 时定位到下一组记录。"""
        rows = self._filter_rows(
            keyword=None, field=field, status=status,
            window_start=window_start, window_end=window_end, hide_noise=hide_noise,
        )
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault((str(row.get("表名称")), str(row.get("漂移字段"))), []).append(row)
        # 每组取最新未收口记录作为代表，组间按代表记录的检测时间升序
        reps: list[tuple[tuple[str, str], dict[str, Any]]] = []
        for key, members in groups.items():
            open_members = [m for m in members if m.get("status") == OPEN_STATUS]
            if not open_members:
                continue
            rep = max(open_members, key=lambda r: (_parse_time(r.get("检测时间")) or datetime.min, int(r.get("id", 0))))
            reps.append((key, rep))
        reps.sort(key=lambda item: (_parse_time(item[1].get("检测时间")) or datetime.min, int(item[1].get("id", 0))))
        if not reps:
            return None, False, "当前筛选条件下没有未收口记录"
        index = len(reps) - 1
        if after:
            anchor = str(after).strip()
            current = next(
                (i for i, (_, rep) in enumerate(reps)
                 if str(rep.get("记录编号")) == anchor or str(rep.get("id")) == anchor),
                None,
            )
            if current is None:
                return None, False, f"锚点 {anchor} 不在当前筛选结果里，无法定位下一组"
            if current + 1 >= len(reps):
                return None, False, "已经是最后一组，没有更多未收口记录"
            index = current + 1
        key, rep = reps[index]
        members = sorted(groups[key], key=lambda r: int(r.get("id", 0)))
        result = {
            "entry": rep,
            "group": {
                "表名称": key[0],
                "漂移字段": key[1],
                "组序号": index + 1,
                "组总数": len(reps),
                "组内记录": [row.get("记录编号") for row in members],
            },
        }
        return result, True, f"已定位到第 {index + 1}/{len(reps)} 组的最新未收口记录 {rep.get('记录编号')}"

    def list_history(
        self,
        *,
        table: str | None = None,
        field: str | None = None,
        window: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """采样历史：重复样本、噪声样本与原基线都保留可查。"""
        samples = list(store.rows(SAMPLE_MODULE))
        if table:
            samples = [s for s in samples if table in str(s.get("表名称", ""))]
        if field:
            samples = [s for s in samples if field in str(s.get("漂移字段", ""))]
        if window:
            samples = [s for s in samples if str(s.get("采样窗口", "")) == window]
        windows = {
            (w.get("表名称"), w.get("漂移字段"), w.get("采样窗口")): w
            for w in store.rows(WINDOW_MODULE)
        }
        items = []
        ordered = sorted(samples, key=lambda s: (_parse_time(s.get("检测时间")) or datetime.min, int(s.get("id", 0))))
        for sample in ordered:
            state = windows.get((sample.get("表名称"), sample.get("漂移字段"), sample.get("采样窗口"))) or {}
            items.append({
                **sample,
                "窗口状态": state.get("状态", "待归并"),
                "是否收口": state.get("状态") == "已收口",
            })
        total = len(items)
        start = max(page - 1, 0) * size
        return items[start:start + size], total

    # ---------- 登记：归并断点修在这里 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """登记入口：带「当前数值」的按采样样本归并（幂等），否则按人工记录登记。"""
        if str(values.get("当前数值") or "").strip():
            return self._ingest_sample(values, merge_now=True)
        return self._create_manual(values)

    def register_sample(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        """只暂存采样样本，等待批量归并；中断后从失败窗口继续。"""
        return self._ingest_sample(values, merge_now=False)

    def _create_manual(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, bool, str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, False, f"缺少必填字段：{'、'.join(missing)}"
        rows = store.rows(MODULE)
        code = str(values.get("记录编号") or "").strip() or self._next_code(rows)
        if any(row.get("记录编号") == code for row in rows):
            return None, False, f"记录编号 {code} 已存在，请换号或改走采样归并"
        ratio_text = str(values.get("偏差比例") or "").strip()
        parsed = _parse_ratio(ratio_text)
        if parsed is not None:
            ratio_text = _format_ratio(parsed)
        moment_text = str(values.get("检测时间") or "").strip() or _now_text()
        window = _window_key(_parse_time(moment_text) or datetime.now())
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "记录编号": code,
            "表名称": values.get("表名称"),
            "漂移字段": values.get("漂移字段"),
            "历史均值": values.get("历史均值") or "—",
            "当前数值": "—",
            "偏差比例": ratio_text or "—",
            "漂移方向": values.get("漂移方向") or "持平",
            "检测时间": moment_text,
            "首次偏差": ratio_text or "—",
            "最近偏差": ratio_text or "—",
            "样本数": 0,
            "结论版本": 1,
            "结论": CONCLUSION_WATCH,
            "采样窗口": window,
            "基线冻结": False,
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
        }
        rows.append(entry)
        self._sync_downstream()
        return entry, True, "漂移记录已登记"

    def _ingest_sample(self, values: dict[str, Any], *, merge_now: bool) -> tuple[dict[str, Any] | None, bool, str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, False, f"缺少必填字段：{'、'.join(missing)}"
        table = str(values["表名称"]).strip()
        field = str(values["漂移字段"]).strip()
        current = _to_float(values.get("当前数值"))
        if current is None:
            return None, False, "当前数值必须是数字，才能计算偏差比例"
        moment = _parse_time(values.get("检测时间")) or datetime.now()
        with _MERGE_LOCK:
            duplicate = self._find_duplicate(table, field, _window_key(moment), current)
            if duplicate is not None:
                record = self._record_by_code(duplicate.get("归属记录编号"))
                return record, True, (
                    f"重复采样已幂等忽略：{table}.{field} 在窗口 {duplicate.get('采样窗口')} "
                    "已有相同数值的样本，归并结果未变化"
                )
            baseline = self._frozen_baseline(table, field)
            if baseline is None:
                baseline = _to_float(values.get("历史均值"))
            sample = self._append_sample(table, field, baseline, current, moment)
            window = self._ensure_window(table, field, str(sample["采样窗口"]))
            window["样本数"] = int(window.get("样本数", 0)) + 1
            if not merge_now:
                return sample, True, f"采样样本已暂存：{table}.{field} 窗口 {sample['采样窗口']}，等待归并"
            record = self._merge_sample(sample)
            self._refresh_window(window)
            self._sync_downstream()
            return record, True, f"采样样本已登记并归并到 {record['记录编号']}（{table}.{field}）"

    def merge_pending(self) -> tuple[dict[str, Any], bool, str]:
        """按采样窗口升序归并待归并样本；已收口窗口跳过，失败窗口下次继续。"""
        with _MERGE_LOCK:
            windows = [w for w in store.rows(WINDOW_MODULE) if w.get("状态") != "已收口"]
            windows.sort(key=lambda w: _window_sort_key(str(w.get("采样窗口", ""))))
            done: list[str] = []
            failed: str | None = None
            merged_samples = 0
            for window in windows:
                window["状态"] = "归并中"
                try:
                    samples = [
                        s for s in store.rows(SAMPLE_MODULE)
                        if not s.get("已归并")
                        and s.get("表名称") == window.get("表名称")
                        and s.get("漂移字段") == window.get("漂移字段")
                        and s.get("采样窗口") == window.get("采样窗口")
                    ]
                    samples.sort(key=lambda s: (_parse_time(s.get("检测时间")) or datetime.min, int(s.get("id", 0))))
                    for sample in samples:
                        self._merge_sample(sample)
                        merged_samples += 1
                    self._refresh_window(window)
                    done.append(str(window.get("采样窗口")))
                except Exception as exc:  # 中断：窗口标记失败，下次归并从它继续
                    window["状态"] = "失败"
                    window["失败原因"] = str(exc)
                    failed = str(window.get("采样窗口"))
                    break
            self._sync_downstream()
            remaining = sum(1 for w in store.rows(WINDOW_MODULE) if w.get("状态") != "已收口")
            summary = {
                "已归并窗口": done,
                "失败窗口": failed,
                "归并样本数": merged_samples,
                "待归并窗口数": remaining,
                "归并顺序": MERGE_ORDER,
                "窗口长度小时": WINDOW_HOURS,
                "噪声阈值": _format_ratio(NOISE_THRESHOLD),
            }
            if failed:
                return summary, False, f"归并在窗口 {failed} 中断，修复后重新归并会从失败窗口继续"
            if not done:
                return summary, True, "没有待归并的采样窗口，已收口证据未重复累计"
            return summary, True, f"已按采样窗口归并 {len(done)} 个窗口、{merged_samples} 条样本"

    # ---------- 归并内部步骤 ----------

    def _merge_sample(self, sample: dict[str, Any]) -> dict[str, Any] | None:
        """把一条采样样本归并到「表 + 字段」的最新未收口记录上。"""
        if sample.get("已归并"):
            return self._record_by_code(sample.get("归属记录编号"))
        table = str(sample.get("表名称"))
        field = str(sample.get("漂移字段"))
        current = _to_float(sample.get("当前数值")) or 0.0
        baseline = self._frozen_baseline(table, field)
        if baseline is None:
            baseline = _to_float(sample.get("历史均值"))
        if baseline is None:
            baseline = current
        ratio = 0.0 if baseline == 0 else (current - baseline) / baseline
        crossed = abs(ratio) >= NOISE_THRESHOLD
        ratio_text = _format_ratio(ratio)
        sample["偏差比例"] = ratio_text
        sample["历史均值"] = _format_number(baseline)  # 原基线留痕，后续不被覆盖
        sample["是否噪声"] = not crossed

        record = self._latest_open_record(table, field)
        if record is None:
            record = self._new_merged_record(table, field, baseline, sample, ratio, ratio_text, crossed)
        else:
            record["样本数"] = int(record.get("样本数", 0)) + 1
            if crossed:
                # 只对跨过阈值的变化推进结论；噪声样本不覆盖最近偏差与当前数值
                changed = record.get("结论") != CONCLUSION_REAL or record.get("最近偏差") != ratio_text
                record["最近偏差"] = ratio_text
                record["偏差比例"] = ratio_text
                record["当前数值"] = sample.get("当前数值")
                record["漂移方向"] = _direction(ratio)
                record["结论"] = CONCLUSION_REAL
                sample_time = _parse_time(sample.get("检测时间")) or datetime.min
                record_time = _parse_time(record.get("检测时间")) or datetime.min
                if sample_time >= record_time:
                    record["检测时间"] = sample.get("检测时间")
                    record["采样窗口"] = sample.get("采样窗口")
                if changed:
                    record["结论版本"] = int(record.get("结论版本", 1)) + 1
        sample["已归并"] = True
        sample["归属记录编号"] = record.get("记录编号")
        return record

    def _new_merged_record(
        self,
        table: str,
        field: str,
        baseline: float,
        sample: dict[str, Any],
        ratio: float,
        ratio_text: str,
        crossed: bool,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        record = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "记录编号": self._next_code(rows),
            "表名称": table,
            "漂移字段": field,
            "历史均值": _format_number(baseline),
            "当前数值": sample.get("当前数值"),
            "偏差比例": ratio_text,
            "漂移方向": _direction(ratio),
            "检测时间": sample.get("检测时间"),
            "首次偏差": ratio_text,
            "最近偏差": ratio_text,
            "样本数": 1,
            "结论版本": 1,
            "结论": CONCLUSION_REAL if crossed else CONCLUSION_NOISE,
            "采样窗口": sample.get("采样窗口"),
            "基线冻结": False,
            "status": OPEN_STATUS,
            "pending": True,
            "abnormal": False,
        }
        rows.append(record)
        return record

    def _latest_open_record(self, table: str, field: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("表名称") == table and row.get("漂移字段") == field and row.get("status") == OPEN_STATUS
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: (_parse_time(r.get("检测时间")) or datetime.min, int(r.get("id", 0))))

    def _frozen_baseline(self, table: str, field: str) -> float | None:
        """字段的冻结基线：已收口记录的历史均值，并发窗口都以它为准。"""
        frozen = [
            row for row in store.rows(MODULE)
            if row.get("表名称") == table and row.get("漂移字段") == field and row.get("基线冻结")
        ]
        if not frozen:
            return None
        latest = max(frozen, key=lambda r: (_parse_time(r.get("检测时间")) or datetime.min, int(r.get("id", 0))))
        return _to_float(latest.get("历史均值"))

    def _find_duplicate(self, table: str, field: str, window: str, current: float) -> dict[str, Any] | None:
        """幂等指纹：同一表、字段、采样窗口里的相同数值只计一次。"""
        for sample in store.rows(SAMPLE_MODULE):
            if (
                sample.get("表名称") == table
                and sample.get("漂移字段") == field
                and sample.get("采样窗口") == window
                and _to_float(sample.get("当前数值")) == current
            ):
                return sample
        return None

    def _append_sample(self, table: str, field: str, baseline: float | None, current: float, moment: datetime) -> dict[str, Any]:
        samples = store.rows(SAMPLE_MODULE)
        sample = {
            "id": max((int(s.get("id", 0)) for s in samples), default=0) + 1,
            "样本编号": self._next_sample_code(samples),
            "表名称": table,
            "漂移字段": field,
            "历史均值": _format_number(baseline) if baseline is not None else "—",
            "当前数值": _format_number(current),
            "偏差比例": "",
            "采样窗口": _window_key(moment),
            "检测时间": _time_text(moment),
            "是否噪声": False,
            "已归并": False,
            "归属记录编号": None,
        }
        samples.append(sample)
        return sample

    def _ensure_window(self, table: str, field: str, window_key: str) -> dict[str, Any]:
        for window in store.rows(WINDOW_MODULE):
            if (
                window.get("表名称") == table
                and window.get("漂移字段") == field
                and window.get("采样窗口") == window_key
            ):
                return window
        windows = store.rows(WINDOW_MODULE)
        window = {
            "id": max((int(w.get("id", 0)) for w in windows), default=0) + 1,
            "表名称": table,
            "漂移字段": field,
            "采样窗口": window_key,
            "状态": "待归并",
            "样本数": 0,
            "收口时间": None,
            "失败原因": None,
        }
        windows.append(window)
        return window

    def _refresh_window(self, window: dict[str, Any]) -> None:
        samples = [
            s for s in store.rows(SAMPLE_MODULE)
            if s.get("表名称") == window.get("表名称")
            and s.get("漂移字段") == window.get("漂移字段")
            and s.get("采样窗口") == window.get("采样窗口")
        ]
        window["样本数"] = len(samples)
        if samples and all(s.get("已归并") for s in samples):
            window["状态"] = "已收口"
            window["收口时间"] = _now_text()
            window["失败原因"] = None
        elif window.get("状态") == "已收口":
            window["状态"] = "待归并"
            window["收口时间"] = None

    # ---------- 状态流转 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"漂移记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据漂移检测可执行范围"
        source = ACTION_RULES[action]
        if entry.get("status") != source:
            return None, f"当前状态「{entry.get('status')}」不能执行「{action}」，需先处于「{source}」"
        target = STATUS_ORDER[STATUS_ORDER.index(source) + 1]
        entry["status"] = target
        entry["pending"] = target in ("已记录", "已确认")
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target in FROZEN_STATUSES:
            # 确认即收口：冻结基线，后续同字段采样以这条基线为准
            entry["基线冻结"] = True
        self._sync_downstream()
        return entry, f"漂移记录已{action}，状态推进为「{target}」"

    # ---------- 下游同步：健康度报告 + 查询性能报告 ----------

    def _sync_downstream(self) -> None:
        """把归并结论同步到健康度报告（最终字段结论）和查询性能报告（受影响表）。"""
        rows = store.rows(MODULE)
        chains: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            chains.setdefault((str(row.get("表名称")), str(row.get("漂移字段"))), []).append(row)
        conclusions = []
        for (table, field), chain in chains.items():
            ordered = sorted(chain, key=lambda r: (_parse_time(r.get("检测时间")) or datetime.min, int(r.get("id", 0))))
            first, latest = ordered[0], ordered[-1]
            baseline = self._frozen_baseline(table, field)
            conclusions.append({
                "表名称": table,
                "漂移字段": field,
                "结论": latest.get("结论", CONCLUSION_WATCH),
                "状态": latest.get("status"),
                "首次偏差": first.get("首次偏差"),
                "最近偏差": latest.get("最近偏差"),
                "样本数": sum(int(r.get("样本数", 0)) for r in ordered),
                "结论版本": latest.get("结论版本", 1),
                "历史均值": _format_number(baseline) if baseline is not None else latest.get("历史均值"),
                "当前数值": latest.get("当前数值"),
                "检测时间": latest.get("检测时间"),
            })
        conclusions.sort(key=lambda item: (item["表名称"], item["漂移字段"]))
        score_report = self._pick_report(SCORE_MODULE, "评估中")
        if score_report is not None:
            score_report["字段结论"] = conclusions  # 只保留最终字段结论，每次整体覆盖
            score_report["结论同步时间"] = _now_text()
        affected = sorted({
            str(row.get("表名称"))
            for row in rows
            if row.get("结论") == CONCLUSION_REAL and row.get("status") in ("已记录", "已确认")
        })
        benchmark_report = self._pick_report(BENCHMARK_MODULE, "分析中")
        if benchmark_report is not None:
            benchmark_report["受影响表"] = affected
            benchmark_report["影响同步时间"] = _now_text()

    @staticmethod
    def _pick_report(module: str, preferred_status: str) -> dict[str, Any] | None:
        rows = store.rows(module)
        for row in rows:
            if row.get("status") == preferred_status and row.get("pending"):
                return row
        for row in rows:
            if row.get("pending"):
                return row
        return None

    # ---------- 编号与查找 ----------

    @staticmethod
    def _next_code(rows: list[dict[str, Any]]) -> str:
        serial = 0
        for row in rows:
            match = re.match(r"DRIF-(\d+)$", str(row.get("记录编号", "")))
            if match:
                serial = max(serial, int(match.group(1)))
        return f"DRIF-{serial + 1:04d}"

    @staticmethod
    def _next_sample_code(samples: list[dict[str, Any]]) -> str:
        serial = 0
        for sample in samples:
            match = re.match(r"SAMP-(\d+)$", str(sample.get("样本编号", "")))
            if match:
                serial = max(serial, int(match.group(1)))
        return f"SAMP-{serial + 1:04d}"

    @staticmethod
    def _record_by_code(code: Any) -> dict[str, Any] | None:
        if not code:
            return None
        for row in store.rows(MODULE):
            if row.get("记录编号") == code:
                return row
        return None
