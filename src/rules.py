"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, RecomputeError, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
# 引航员不走计划状态机，通过独立的回传对账接口工作，因此在known_role中单独登记。
KNOWN_EXTRA_ROLES = {'pilot'}
ACTION_ROLES = {
    'confirm': {'port_controller'},
    'berth': {'port_controller'},
    'depart': {'port_controller'},
    'cancel': {'port_controller'},
    'reschedule': {'port_controller'},
}
TRANSITIONS = {
    'confirm': {'draft': 'confirmed'},
    'berth': {'confirmed': 'berthed'},
    'depart': {'berthed': 'departed'},
    'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'},
    # 改期不改变计划状态，只挪动潮位窗口内的时间并重排通行批次。
    'reschedule': {'draft': 'draft', 'confirmed': 'confirmed'},
}
RISK_ORDER = {'high': 0, 'medium': 1, 'low': 2}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(KNOWN_EXTRA_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        direction = str(p.get("direction", "in"))
        if direction not in {"in", "out"}:
            raise ValidationError("direction只能是in/out")
        p["direction"] = direction
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

    def validate_reschedule(self, data: Dict[str, Any]) -> Dict[str, int]:
        eta = integer(data, "eta_hour", 0, 23)
        etd = integer(data, "etd_hour", 1, 24)
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        return {"eta_hour": eta, "etd_hour": etd}

    def validate_window(self, data: Dict[str, Any]) -> Dict[str, Any]:
        code = text(data, "code")
        start = integer(data, "start_hour", 0, 23)
        end = integer(data, "end_hour", 1, 24)
        if end <= start:
            raise ValidationError("end_hour必须晚于start_hour")
        quota = integer(data, "batch_quota", 1, 1000)
        max_batches = integer(data, "max_batches", 1, 1000)
        return {"code": code, "start_hour": start, "end_hour": end,
                "batch_quota": quota, "max_batches": max_batches}

    # ---- 通行批次排程（纯计算，不碰持久化） ----

    @staticmethod
    def _eligible(plan: Dict[str, Any], window: Dict[str, Any]) -> bool:
        """已取消/已离港的船不再进闸口批次；船必须落在该潮位窗口的小时区间内。"""
        if plan.get("state") in {"cancelled", "departed"}:
            return False
        eta = int(plan["payload"].get("eta_hour", -1))
        return int(window["start_hour"]) <= eta < int(window["end_hour"])

    def plan_batches(self, plans: List[Dict[str, Any]], window: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按潮位窗口排出航道通行批次。

        顺位规则：先到先排（eta_hour升序），同时刻高风险船优先；每批最多batch_quota艘，
        窗口最多max_batches批，排不下即视为重算不可行。
        """
        queued = [p for p in plans if self._eligible(p, window)]
        queued.sort(key=lambda p: (
            int(p["payload"]["eta_hour"]),
            RISK_ORDER.get(p["payload"].get("risk_level", "low"), 2),
            int(p["id"]),
        ))
        quota = int(window["batch_quota"])
        max_batches = int(window["max_batches"])
        if len(queued) > quota * max_batches:
            raise RecomputeError(
                "潮位窗口%s容量不足：%s艘船超出%s批×%s艘的上限"
                % (window["code"], len(queued), max_batches, quota)
            )
        batches: List[Dict[str, Any]] = []
        for index in range(0, len(queued), quota):
            chunk = queued[index:index + quota]
            batches.append({
                "seq": len(batches) + 1,
                "members": [{
                    "plan_id": int(p["id"]),
                    "reference": p.get("reference", ""),
                    "vessel": p["payload"].get("vessel", ""),
                    "direction": p["payload"].get("direction", "in"),
                    "eta_hour": int(p["payload"]["eta_hour"]),
                    "risk_level": p["payload"].get("risk_level", "low"),
                } for p in chunk],
            })
        return batches

    # ---- 引航回传逐船对账（纯计算） ----

    def reconcile_items(self, items: List[Dict[str, Any]], plans: List[Dict[str, Any]],
                        active_plan_ids: set, window_hours: Dict[int, int]) -> List[Dict[str, Any]]:
        """对回传单中的每条船给出对账结论。

        active_plan_ids：当前有效批次中的计划id集合。批次一旦重算，旧批次上的船
        在新批次落地前会被判为"不在有效批次中"而挂起，闸口不会放行改期船。
        """
        if not isinstance(items, list) or not items:
            raise ValidationError("items至少包含一条船的回传")
        plans_by_vessel: Dict[str, Dict[str, Any]] = {}
        for plan in plans:
            if plan.get("state") == "cancelled":
                continue
            vessel = plan["payload"].get("vessel")
            # 同一船名取最新一条计划，且未离港的优先。
            old = plans_by_vessel.get(vessel)
            if old is None or (old["state"] == "departed" and plan["state"] != "departed") or int(plan["id"]) > int(old["id"]):
                plans_by_vessel[vessel] = plan
        results: List[Dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                raise ValidationError("items每一项必须是对象")
            vessel = text(raw, "vessel")
            movement = str(raw.get("movement", ""))
            if movement not in {"in", "out"}:
                raise ValidationError("movement只能是in/out")
            actual = integer(raw, "actual_hour", 0, 24)
            pilot_id = str(raw.get("pilot_id", "")).strip()
            plan = plans_by_vessel.get(vessel)
            if plan is None:
                results.append({"vessel": vessel, "movement": movement, "actual_hour": actual,
                                "pilot_id": pilot_id, "landed": False,
                                "reason": "未找到该船的靠泊计划"})
                continue
            if int(plan["id"]) not in active_plan_ids:
                results.append({"vessel": vessel, "movement": movement, "actual_hour": actual,
                                "pilot_id": pilot_id, "landed": False, "plan_id": int(plan["id"]),
                                "reason": "该船不在当前有效通行批次中，批次可能已随潮位窗口重算"})
                continue
            payload = plan["payload"]
            if movement == "in":
                expected = int(payload.get("eta_hour", -1))
                label = "进港"
            else:
                # 离港小时取计划etd；窗口字典允许24点这种越界表示。
                expected = window_hours.get(int(plan["id"]), int(payload.get("etd_hour", -1)))
                label = "出港"
            if actual != expected:
                results.append({"vessel": vessel, "movement": movement, "actual_hour": actual,
                                "pilot_id": pilot_id, "landed": False, "plan_id": int(plan["id"]),
                                "expected_hour": expected,
                                "reason": "%s时间与计划不一致（计划%s时，回传%s时）" % (label, expected, actual)})
                continue
            results.append({"vessel": vessel, "movement": movement, "actual_hour": actual,
                            "pilot_id": pilot_id, "landed": True, "plan_id": int(plan["id"]),
                            "expected_hour": expected, "reason": ""})
        return results

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
        elif action == "reschedule":
            times = self.validate_reschedule(data)
            changes.update(times)
            changes["window_hours"] = times["etd_hour"] - times["eta_hour"]
            summary = "计划改期至%s-%s时，通行批次已重算" % (times["eta_hour"], times["etd_hour"])
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
