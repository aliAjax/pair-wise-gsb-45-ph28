"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import (
    Actor,
    Conflict,
    RecomputeFailed,
    ValidationError,
    boolean,
    choice,
    integer,
    number,
    optional_int,
    text,
    text_list,
)


INITIAL_STATE = "draft"
DIRECTIONS = ["inbound", "outbound"]
CREATE_ROLES = {'port_controller', 'dispatcher'}
ACTION_ROLES = {
    'confirm': {'port_controller'},
    'berth': {'port_controller'},
    'depart': {'port_controller'},
    'cancel': {'port_controller'},
    'reschedule': {'port_controller', 'duty_officer'},
}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}
RESCHEDULE_STATES = {'draft', 'confirmed'}
BATCH_PLAN_STATES = {'draft', 'confirmed', 'berthed'}
DEFAULT_TOLERANCE_HOURS = 1


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update({'duty_officer', 'pilot'})
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "vessel")
        text(p, "berth")
        number(p, "vessel_length_m", 1)
        number(p, "berth_length_m", 1)
        number(p, "draft_m", 0)
        number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        p["direction"] = choice(p, "direction", DIRECTIONS) if p.get("direction") is not None else "inbound"
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if p["berth_length_m"] < p["vessel_length_m"]:
            raise ValidationError("泊位长度不足")
        if p["berth_depth_m"] - p["draft_m"] < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    # ---- 潮位窗口 ----

    def validate_tide_window(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "name")
        text(p, "date")
        if len(p["date"]) != 10 or p["date"][4] != "-" or p["date"][7] != "-":
            raise ValidationError("date必须为YYYY-MM-DD")
        year, month, day = (int(part) for part in p["date"].split("-"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ValidationError("date不是合法日期")
        if not 1 <= year <= 9999:
            raise ValidationError("date不是合法日期")
        direction = choice(p, "direction", DIRECTIONS)
        open_hour = integer(p, "open_hour", 0, 23)
        close_hour = integer(p, "close_hour", 1, 24)
        if close_hour <= open_hour:
            raise ValidationError("close_hour必须晚于open_hour")
        integer(p, "vessel_quota", 1, 100)
        number(p, "tide_level_m", 0)
        return {"name": p["name"], "date": p["date"], "direction": direction,
                "open_hour": open_hour, "close_hour": close_hour,
                "vessel_quota": p["vessel_quota"], "tide_level_m": p["tide_level_m"]}

    def plan_required_hour(self, plan_payload: Dict[str, Any]) -> int:
        return int(plan_payload["etd_hour"]) if plan_payload.get("direction") == "outbound" else int(plan_payload["eta_hour"])

    def assert_plan_fits_window(self, plan_payload: Dict[str, Any], window: Dict[str, Any]) -> None:
        if plan_payload.get("direction", "inbound") != window["direction"]:
            raise RecomputeFailed(
                "计划流向与窗口不一致",
                {"reason": "direction_mismatch", "vessel": plan_payload.get("vessel"), "window": window["name"]},
            )
        required_hour = self.plan_required_hour(plan_payload)
        if not (int(window["open_hour"]) <= required_hour <= int(window["close_hour"])):
            raise RecomputeFailed(
                "船舶%s的通行时刻落在潮位窗口之外" % plan_payload.get("vessel"),
                {"reason": "outside_window", "vessel": plan_payload.get("vessel"),
                 "required_hour": required_hour, "window": window["name"]},
            )
        if float(plan_payload.get("draft_m", 0)) > float(window["tide_level_m"]):
            raise RecomputeFailed(
                "潮位无法满足船舶%s的吃水" % plan_payload.get("vessel"),
                {"reason": "tide_too_low", "vessel": plan_payload.get("vessel"),
                 "draft_m": plan_payload.get("draft_m"), "tide_level_m": window["tide_level_m"]},
            )

    def assert_no_berth_conflict(self, plans: List[Dict[str, Any]]) -> None:
        active = [p for p in plans if p["state"] in BATCH_PLAN_STATES]
        active.sort(key=lambda item: int(item["payload"]["eta_hour"]))
        for i, item in enumerate(active):
            payload = item["payload"]
            for other in active[i + 1:]:
                theirs = other["payload"]
                if theirs.get("berth") != payload.get("berth"):
                    continue
                if int(payload["eta_hour"]) < int(theirs["etd_hour"]) and int(payload["etd_hour"]) > int(theirs["eta_hour"]):
                    raise RecomputeFailed(
                        "重算后泊位%s时间窗冲突：%s/%s" % (payload.get("berth"), payload.get("vessel"), theirs.get("vessel")),
                        {"reason": "berth_conflict", "berth": payload.get("berth"),
                         "vessels": [payload.get("vessel"), theirs.get("vessel")]},
                    )

    # ---- 航道批次编排 ----

    @staticmethod
    def _is_dangerous(payload: Dict[str, Any]) -> bool:
        return bool(payload.get("dangerous_goods"))

    def plan_batches(self, plans: List[Dict[str, Any]], window: Dict[str, Any],
                     pinned: List[Dict[str, Any]] = None) -> List[List[Dict[str, Any]]]:
        """按窗口把可编排计划贪心分组：危险品单船单批次，普通船受配额限制。

        pinned 为已闸口放行/落地的旧批次条目，重算时继续占用额度并排在新批次前部。
        """
        quota = int(window["vessel_quota"])
        groups: List[List[Dict[str, Any]]] = []

        def flush(items: List[Dict[str, Any]]) -> None:
            if items:
                groups.append(items)

        current: List[Dict[str, Any]] = []
        pinned_ids = set()
        for entry in pinned or []:
            pinned_ids.add(int(entry["record_id"]))
            item = {"record_id": int(entry["record_id"]), "vessel": entry["vessel"],
                    "dangerous_goods": bool(entry["dangerous_goods"]), "pinned": True,
                    "status": entry["status"], "released_at": entry.get("released_at"),
                    "landed_at": entry.get("landed_at")}
            if item["dangerous_goods"]:
                flush(current)
                current = []
                flush([item])
            else:
                if len(current) >= quota:
                    flush(current)
                    current = []
                current.append(item)

        candidates = [
            p for p in plans
            if int(p.get("tide_window_id") or 0) == int(window["id"])
            and p["state"] in BATCH_PLAN_STATES
            and int(p["id"]) not in pinned_ids
        ]
        candidates.sort(key=lambda p: (self.plan_required_hour(p["payload"]), int(p["id"])))
        for plan in candidates:
            payload = plan["payload"]
            item = {"record_id": int(plan["id"]), "vessel": payload.get("vessel"),
                    "dangerous_goods": self._is_dangerous(payload), "pinned": False}
            if item["dangerous_goods"]:
                flush(current)
                current = []
                flush([item])
            else:
                if len(current) >= quota:
                    flush(current)
                    current = []
                current.append(item)
        flush(current)
        return groups

    # ---- 改期 ----

    def build_reschedule_payload(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        data = data or {}
        unknown = set(data) - {"eta_hour", "etd_hour"}
        if unknown:
            raise ValidationError("改期只允许调整eta_hour/etd_hour，异常字段：%s" % ",".join(sorted(unknown)))
        merged = dict(record["payload"])
        if "eta_hour" in data:
            merged["eta_hour"] = integer(data, "eta_hour", 0, 23)
        if "etd_hour" in data:
            merged["etd_hour"] = integer(data, "etd_hour", 1, 24)
        return self.prepare_create(merged)

    def require_reschedule_state(self, record: Dict[str, Any]) -> None:
        if record["state"] not in RESCHEDULE_STATES:
            raise Conflict("当前状态不允许改期，仅draft/confirmed可改期")

    # ---- 状态转换 ----

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 闸口放行 ----

    def check_gate_release(self, entry: Dict[str, Any], batch: Dict[str, Any],
                           window: Dict[str, Any], record: Dict[str, Any], now_hour: int) -> None:
        if batch["status"] != "active":
            raise Conflict("批次已失效，闸口不得放行")
        if entry["status"] != "waiting":
            raise Conflict("该船批次条目已处理，状态：%s" % entry["status"])
        if int(entry["record_id"]) != int(record["id"]):
            raise Conflict("批次条目与靠泊计划不匹配")
        if record["state"] not in BATCH_PLAN_STATES:
            raise Conflict("靠泊计划状态为%s，不得放行" % record["state"])
        if window["status"] != "open":
            raise Conflict("潮位窗口未开放")
        if not (int(window["open_hour"]) <= now_hour <= int(window["close_hour"])):
            raise Conflict("当前时刻不在窗口开放时段内")

    # ---- 引航对账 ----

    def validate_pilot_report(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        text(p, "notice_id")
        text(p, "pilot_id")
        raw_items = p.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValidationError("items至少包含一条回传")
        items: List[Dict[str, Any]] = []
        seen = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValidationError("回传条目必须是对象")
            vessel = text(raw, "vessel")
            flow = choice(raw, "direction", ["in", "out"])
            actual_hour = integer(raw, "actual_hour", 0, 24)
            if vessel in seen:
                raise ValidationError("同一回传单中船舶%s重复" % vessel)
            seen.add(vessel)
            items.append({"vessel": vessel, "direction": flow, "actual_hour": actual_hour})
        tolerance = optional_int(p, "tolerance_hours", 0, 12, DEFAULT_TOLERANCE_HOURS)
        return {"notice_id": p["notice_id"], "pilot_id": p["pilot_id"],
                "items": items, "tolerance_hours": tolerance}

    def reconcile_item(self, plan: Optional[Dict[str, Any]], item: Dict[str, Any],
                       tolerance: int = DEFAULT_TOLERANCE_HOURS) -> Dict[str, Any]:
        if plan is None:
            return {"state": "suspended", "reason": "plan_not_found"}
        payload = plan["payload"]
        want_direction = "in" if payload.get("direction", "inbound") == "inbound" else "out"
        if item["direction"] != want_direction:
            return {"state": "suspended", "reason": "direction_mismatch",
                    "planned_direction": want_direction}
        planned_hour = int(payload["eta_hour"]) if want_direction == "in" else int(payload["etd_hour"])
        diff = abs(int(item["actual_hour"]) - planned_hour)
        if diff > tolerance:
            return {"state": "suspended", "reason": "time_mismatch",
                    "planned_hour": planned_hour, "actual_hour": int(item["actual_hour"]),
                    "diff_hours": diff, "tolerance_hours": tolerance}
        return {"state": "matched", "planned_hour": planned_hour,
                "actual_hour": int(item["actual_hour"]), "diff_hours": diff}
