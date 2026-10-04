"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import (
    Actor,
    Conflict,
    DomainError,
    DraftConflict,
    PermissionDenied,
    RecomputeError,
    ValidationError,
    integer,
    text,
)
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        # 通行批次独立于靠泊计划：新计划落库后只触发其所属窗口重排。
        # 重排不可行不回滚计划本身，旧批次保留，原因留痕，值班员可重试。
        self._replan(actor, self._window_ids_for([record]), trigger="create")
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 潮位窗口 ----

    def create_window(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权维护潮位窗口")
        window = self.repository.create_window(self.rules.validate_window(data or {}), actor.user_id)
        self._replan(actor, [int(window["id"])], trigger="window_create")
        return window

    def list_windows(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_windows()

    def replan(self, actor: Actor, window_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权触发批次重算")
        window = self.repository.get_window(int(window_id))
        return self._replan(actor, [int(window["id"])], trigger="manual", required=True)

    def _window_ids_for(self, plans: List[Dict[str, Any]]) -> List[int]:
        ids = []
        for plan in plans:
            eta = int(plan["payload"].get("eta_hour", 0))
            window = self.repository.window_for_hour(eta)
            if window is None:
                raise RecomputeError("小时%s没有可用的潮位窗口" % eta)
            if int(window["id"]) not in ids:
                ids.append(int(window["id"]))
        return ids

    def _make_planner(self):
        def planner(connection, window):
            rows = connection.execute("SELECT * FROM records").fetchall()
            plans = [self.repository._row(row) for row in rows]
            return self.rules.plan_batches(plans, window)
        return planner

    def _replan(self, actor: Actor, window_ids: List[int], trigger: str, required: bool = False) -> Dict[int, List[Dict[str, Any]]]:
        try:
            return self.repository.rebatch_windows(
                window_ids=window_ids,
                actor_id=actor.user_id,
                trigger=trigger,
                planner=self._make_planner(),
            )
        except DomainError as exc:
            # 失败原因落库；旧批次仍是active，闸口继续按旧有效批次放行。
            self.repository.record_failed_recompute(window_ids, str(exc), actor.user_id, trigger)
            if required:
                raise
            return {}

    def list_batches(self, actor: Actor, window_id: Optional[int] = None, active_only: bool = True) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(window_id=window_id, active_only=active_only)

    def gate_list(self, actor: Actor, window_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.gate_list(window_id=window_id)

    def latest_runs(self, actor: Actor, window_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.latest_runs(window_id=window_id)

    # ---- 计划动作与改期 ----

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)

        if action == "reschedule":
            return self._reschedule(actor, record, int(expected_version), data or {})

        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        try:
            return self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            )
        except Conflict:
            # 两个调度员同时改同一计划：先到版本生效，后到者保留冲突草稿，不覆盖。
            draft_id = self._save_conflict_draft(actor, record, int(expected_version), action, data or {},
                                                 "操作时版本已被他人抢先提交")
            raise DraftConflict("计划已被他人修改，你的改动已保留为冲突草稿", draft_id)

    def _reschedule(self, actor: Actor, record: Dict[str, Any], expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        times = self.rules.validate_reschedule(data)
        new_payload = dict(record["payload"])
        new_payload.update(times)
        new_payload["window_hours"] = times["etd_hour"] - times["eta_hour"]
        # 改泊位时间窗冲突仍按同一泊位检查。
        hypothetical = dict(record)
        hypothetical["payload"] = new_payload
        for other in self.repository.list_records(limit=500):
            if other["id"] == record["id"]:
                continue
            if other["state"] in {"cancelled", "departed"}:
                continue
            if other["payload"].get("berth") != new_payload.get("berth"):
                continue
            if times["eta_hour"] < int(other["payload"].get("etd_hour", 0)) and times["etd_hour"] > int(other["payload"].get("eta_hour", 24)):
                raise ValidationError("同一泊位时间窗冲突")

        old_window = self.repository.window_for_hour(int(record["payload"]["eta_hour"]))
        new_window = self.repository.window_for_hour(times["eta_hour"])
        if old_window is None or new_window is None:
            raise RecomputeError("新旧潮位窗口缺失，无法重算批次")
        window_ids = []
        for window in (old_window, new_window):
            if int(window["id"]) not in window_ids:
                window_ids.append(int(window["id"]))

        try:
            outcome = self.repository.reschedule_and_rebatch(
                plan_id=int(record["id"]),
                expected_version=expected_version,
                new_payload=new_payload,
                actor_id=actor.user_id,
                window_ids=window_ids,
                planner=self._make_planner(),
            )
        except Conflict:
            draft_id = self._save_conflict_draft(actor, record, expected_version, "reschedule", data,
                                                 "改期时版本已被他人抢先提交")
            raise DraftConflict("计划已被他人修改，你的改期草稿已保留", draft_id)
        except RecomputeError as exc:
            # 事务已回滚：原批次和原顺位保留，原因留痕供值班员修正后重试。
            self.repository.record_failed_recompute(window_ids, str(exc), actor.user_id, "reschedule")
            raise
        return outcome["record"]

    # ---- 冲突草稿 ----

    def _save_conflict_draft(self, actor: Actor, record: Dict[str, Any], base_version: int,
                             action: str, data: Dict[str, Any], note: str) -> int:
        return self.repository.save_conflict_draft(
            plan_id=int(record["id"]),
            action=action,
            base_version=base_version,
            server_version=int(record["version"]),
            payload=data,
            actor_id=actor.user_id,
            note=note,
        )

    def list_drafts(self, actor: Actor, plan_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_conflict_drafts(plan_id=plan_id)

    def get_draft(self, actor: Actor, draft_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_conflict_draft(int(draft_id))

    def apply_draft(self, actor: Actor, draft_id: int) -> Dict[str, Any]:
        """值班员基于最新版本重新提交草稿；仍冲突则再次保留新草稿。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        draft = self.repository.get_conflict_draft(int(draft_id))
        if draft["status"] != "pending":
            raise Conflict("该冲突草稿已处理")
        record = self.repository.get(int(draft["plan_id"]))
        return self.act(actor, int(draft["plan_id"]), int(record["version"]), draft["action"], draft["payload"])

    def discard_draft(self, actor: Actor, draft_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.dismiss_conflict_draft(int(draft_id))
        return {"id": int(draft_id), "status": "discarded"}

    # ---- 引航回传逐船对账 ----

    def submit_pilot_report(self, actor: Actor, ticket: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._actor(actor)
        if not (self.rules.known_role(actor.role) and (actor.role == "pilot" or actor.role == "admin")):
            raise PermissionDenied("只有引航员可以回传进出港时间")
        ticket = text({"ticket": ticket}, "ticket")
        if not isinstance(items, list) or not items:
            raise ValidationError("items至少包含一条船的回传")
        cleaned = []
        for raw in items:
            if not isinstance(raw, dict):
                raise ValidationError("items每一项必须是对象")
            vessel = text(raw, "vessel")
            movement = raw.get("movement")
            if movement not in {"in", "out"}:
                raise ValidationError("movement只能是in/out")
            cleaned.append({"vessel": vessel, "movement": movement,
                            "actual_hour": integer(raw, "actual_hour", 0, 24),
                            "pilot_id": actor.user_id})

        def reconcile(connection, plans, pending):
            active_ids = self.repository.active_plan_ids(connection)
            window_hours: Dict[int, int] = {}
            return self.rules.reconcile_items(pending, plans, active_ids, window_hours)

        return self.repository.submit_pilot_report(ticket, actor.user_id, cleaned, reconcile)

    def get_pilot_report(self, actor: Actor, ticket: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_pilot_report(text({"ticket": ticket}, "ticket"))

    def list_pilot_reports(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_pilot_reports()

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
