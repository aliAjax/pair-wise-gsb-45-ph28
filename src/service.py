"""业务用例编排、权限检查与审计。"""
import json
import sqlite3
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, RecomputeFailed, ValidationError, text
from .repository import Repository
from .rules import BATCH_PLAN_STATES, DomainRules


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

    def _require_role(self, actor: Actor, roles: set) -> None:
        if actor.role != "admin" and actor.role not in roles:
            raise PermissionDenied("角色无权执行该操作")

    # ---- 靠泊计划 ----

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any],
               tide_window_id: Optional[int] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))

        window = None
        if tide_window_id is not None:
            window = self.repository.get_tide_window(int(tide_window_id))
            if window["direction"] != prepared.get("direction", "inbound"):
                raise ValidationError("计划流向与潮位窗口不一致")
            try:
                self.rules.assert_plan_fits_window(prepared, window)
            except RecomputeFailed as exc:
                # 建计划阶段属于输入校验错误；重算阶段才是可重试的重算失败
                raise ValidationError(str(exc)) from exc

        connection = self.repository._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            record = self.repository.create(
                reference, self.rules.INITIAL_STATE, prepared, actor.user_id,
                tide_window_id=tide_window_id, connection=connection,
            )
            if window is not None:
                self._recompute_locked(window["id"], "plan_created", actor.user_id, connection)
            connection.commit()
        except RecomputeFailed as exc:
            connection.rollback()
            if window is not None:
                self.repository.insert_run(window["id"], "plan_created", "failed",
                                           actor.user_id, reason=str(exc), details=exc.extra)
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str,
            data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        if action == "reschedule":
            return self.reschedule(actor, record_id, int(expected_version), data or {}, save_draft_on_conflict=True)
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action in ("cancel", "depart"):
            self._drop_from_batches(actor, record_id, action)
        return updated

    # ---- 改期 + 同窗口批次重算 ----

    def reschedule(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any],
                   save_draft_on_conflict: bool = False) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'port_controller', 'duty_officer', 'admin'})
        record = self.repository.get(record_id)
        self.rules.require_reschedule_state(record)
        new_payload = self.rules.build_reschedule_payload(record, data)

        connection = self.repository._connect()
        connection.execute("BEGIN IMMEDIATE")
        closed = False
        try:
            try:
                updated = self.repository.mutate(
                    record_id=record_id,
                    expected_version=int(expected_version),
                    state=record["state"],
                    payload=new_payload,
                    actor_id=actor.user_id,
                    action="reschedule",
                    details={"summary": "计划改期，同潮位窗口批次重算", "input": data or {},
                             "from": {"eta_hour": record["payload"]["eta_hour"], "etd_hour": record["payload"]["etd_hour"]}},
                    connection=connection,
                )
            except Conflict as exc:
                connection.rollback()
                connection.close()
                closed = True
                draft_id = None
                if save_draft_on_conflict:
                    draft_id = self.repository.save_conflict_draft(
                        "record", record_id, "reschedule", int(expected_version),
                        actor.user_id, data or {}, "改期时版本冲突，已保留为冲突草稿",
                    )
                raise Conflict(
                    str(exc),
                    {"draft_id": draft_id, "current_version": self.repository.get(record_id)["version"]},
                ) from exc
            window_id = updated.get("tide_window_id")
            if window_id is not None:
                self._recompute_locked(int(window_id), "reschedule", actor.user_id, connection)
            connection.commit()
        except RecomputeFailed as exc:
            if not closed:
                connection.rollback()
            failed_window_id = record.get("tide_window_id")
            if failed_window_id is not None:
                self.repository.insert_run(int(failed_window_id), "reschedule", "failed",
                                           actor.user_id, reason=str(exc), details=exc.extra)
            raise
        finally:
            if not closed:
                connection.close()
        return updated

    def _drop_from_batches(self, actor: Actor, record_id: int, reason: str) -> None:
        window_id = self.repository.invalidate_entries_for_record(record_id, actor.user_id, reason)
        if window_id is None:
            return
        try:
            self.recompute_window(actor, window_id, trigger=reason)
        except RecomputeFailed:
            # 主操作已生效；失败重算留痕，闸口按失败重算拦截，值班员重试
            pass

    # ---- 潮位窗口 ----

    def create_tide_window(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'port_controller', 'admin'})
        prepared = self.rules.validate_tide_window(payload or {})
        return self.repository.create_tide_window(prepared, actor.user_id)

    def list_tide_windows(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_tide_windows()

    def update_tide_window(self, actor: Actor, window_id: int, expected_version: int,
                           data: Dict[str, Any], save_draft_on_conflict: bool = False) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'port_controller', 'admin'})
        data = data or {}
        allowed = {"name", "open_hour", "close_hour", "vessel_quota", "tide_level_m"}
        unknown = set(data) - allowed
        if unknown:
            raise ValidationError("窗口仅允许修改%s，异常字段：%s" % ("/".join(sorted(allowed)), ",".join(sorted(unknown))))
        current = self.repository.get_tide_window(window_id)
        candidate = {key: current[key] for key in ("name", "open_hour", "close_hour", "vessel_quota", "tide_level_m")}
        candidate.update(data)
        self.rules.validate_tide_window({
            "name": candidate["name"], "date": current["date"], "direction": current["direction"],
            "open_hour": candidate["open_hour"], "close_hour": candidate["close_hour"],
            "vessel_quota": candidate["vessel_quota"], "tide_level_m": candidate["tide_level_m"],
        })
        changes = dict(data)
        try:
            window = self.repository.update_tide_window(window_id, int(expected_version), changes, actor.user_id)
        except Conflict as exc:
            draft_id = None
            if save_draft_on_conflict:
                draft_id = self.repository.save_conflict_draft(
                    "tide_window", window_id, "update", int(expected_version),
                    actor.user_id, changes, "潮位窗口调整时版本冲突，已保留为冲突草稿",
                )
            raise Conflict(str(exc), {"draft_id": draft_id, "current_version": current["version"]}) from exc
        self.recompute_window(actor, window_id, trigger="tide_window_changed")
        return window

    # ---- 批次重算 ----

    def _recompute_locked(self, window_id: int, trigger: str, actor_id: str,
                          connection: sqlite3.Connection) -> Dict[str, Any]:
        """调用方已持有写事务。预演失败：旧批次不动，落failed重算记录后回滚调用方事务。"""
        window = self.repository.get_tide_window(window_id, connection)
        plans = self.repository.plans_for_window(window_id, connection)
        self.rules.assert_no_berth_conflict(plans)
        for plan in plans:
            if plan["state"] in BATCH_PLAN_STATES:
                self.rules.assert_plan_fits_window(plan["payload"], window)
        pinned = self.repository.active_entries_for_recompute(window_id, connection)
        groups = self.rules.plan_batches(plans, window, pinned=pinned)
        generation = self.repository.next_batch_generation(window_id, connection)
        run_id = self.repository.insert_run(
            window_id, trigger, "success", actor_id,
            details={"generation": generation, "batch_count": len(groups),
                     "planned_vessels": sum(1 for g in groups for item in g if not item.get("pinned"))},
            generation=generation, connection=connection,
        )
        self.repository.replace_batches(window_id, groups, generation, run_id, actor_id, connection)
        return {"run_id": run_id, "generation": generation, "batch_count": len(groups)}

    def recompute_window(self, actor: Actor, window_id: int, trigger: str = "manual_retry") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'port_controller', 'duty_officer', 'admin'})
        window = self.repository.get_tide_window(window_id)
        connection = self.repository._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = self._recompute_locked(window_id, trigger, actor.user_id, connection)
            connection.commit()
        except RecomputeFailed as exc:
            connection.rollback()
            connection.close()
            self.repository.insert_run(window_id, trigger, "failed", actor.user_id,
                                       reason=str(exc), details=exc.extra)
            raise
        except Exception:
            connection.rollback()
            connection.close()
            raise
        else:
            connection.close()
        result["window"] = window
        return result

    def plan_batches_view(self, actor: Actor, window_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        view = self.repository.list_batches(window_id)
        view["pending_failure"] = self.repository.latest_pending_run(window_id)
        return view

    def list_recompute_runs(self, actor: Actor, window_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_tide_window(window_id)
        return self.repository.list_runs(window_id)

    # ---- 闸口放行 ----

    def gate_release(self, actor: Actor, record_id: int, now_hour: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'duty_officer', 'port_controller', 'admin'})
        record = self.repository.get(record_id)
        window_id = record.get("tide_window_id")
        if not window_id:
            raise Conflict("该靠泊计划未编排潮位窗口，不得放行")
        pending = self.repository.latest_pending_run(int(window_id))
        if pending is not None:
            raise Conflict("潮位窗口批次重算失败后尚未恢复，闸口暂停放行：%s" % pending["reason"],
                           {"recompute_run_id": pending["id"], "reason": pending["reason"]})
        entry = self.repository.get_active_entry_for_record(record_id)
        if entry is None:
            raise Conflict("该船没有有效的航道批次，闸口不得放行（可能已改期）")
        batch = {k: entry[k] for k in ("batch_status", "batch_seq", "batch_generation")}
        batch["status"] = batch.pop("batch_status")
        batch["seq"] = batch.pop("batch_seq")
        batch["generation"] = batch.pop("batch_generation")
        window = self.repository.get_tide_window(int(window_id))
        self.rules.check_gate_release(entry, batch, window, record, int(now_hour))
        return self.repository.release_entry(int(entry["id"]), actor.user_id, int(now_hour))

    # ---- 引航回传与对账 ----

    def submit_pilot_report(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, {'pilot', 'port_controller', 'admin'})
        prepared = self.rules.validate_pilot_report(payload or {})
        tolerance = prepared["tolerance_hours"]
        existing = self.repository.get_pilot_report_by_notice(prepared["notice_id"])

        connection = self.repository._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            if existing is None:
                report_id = self.repository.create_pilot_report(
                    prepared["notice_id"], prepared["pilot_id"], tolerance, False, actor.user_id,
                    connection=connection,
                )
            else:
                report_id = int(existing["id"])
                self.repository.mark_report_retransmit(report_id, connection=connection)
            landed = suspended = skipped = 0
            for item in prepared["items"]:
                outcome = self.upsert_report_item(actor, report_id, item, tolerance, connection)
                if outcome == "skipped":
                    skipped += 1
                elif outcome == "landed":
                    landed += 1
                else:
                    suspended += 1
            totals = connection.execute(
                "SELECT COALESCE(SUM(state='landed'),0) AS landed, COALESCE(SUM(state='suspended'),0) AS suspended FROM pilot_report_items WHERE report_id=?",
                (report_id,),
            ).fetchone()
            self.repository.mark_report_totals(report_id, int(totals["landed"]), int(totals["suspended"]),
                                               skipped, connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.repository.get_pilot_report(report_id)

    def upsert_report_item(self, actor: Actor, report_id: int, item: Dict[str, Any], tolerance: int,
                           connection: sqlite3.Connection) -> str:
        """逐船对账并入单。返回 skipped/landed/suspended。

        已落地的船重传时跳过，不重复占额度；挂起的船按最新批次状态重新判定补落地。
        """
        existing_item = self.repository.get_report_item(report_id, item["vessel"], connection)
        if existing_item is not None:
            if existing_item["state"] == "landed":
                return "skipped"
        plan = self.repository.latest_plan_for_vessel(item["vessel"])
        result = self.rules.reconcile_item(plan, item, tolerance)
        row = {"vessel": item["vessel"], "direction": item["direction"],
               "actual_hour": item["actual_hour"]}
        row.update(result)
        if plan is not None:
            row["record_id"] = int(plan["id"])
        final_state = "suspended"
        if result["state"] == "matched":
            entry = None
            if plan is not None and plan.get("tide_window_id") is not None:
                entry = self.repository.get_active_entry_for_record(int(plan["id"]))
            if entry is None:
                row["state"] = "suspended"
                row["reason"] = "no_active_batch"
            elif entry["status"] not in ("waiting", "released"):
                row["state"] = "suspended"
                row["reason"] = "gate_not_ready"
                row["details"] = {"entry_status": entry["status"]}
            else:
                final_state = "landed"
                row["state"] = "landed"
                row["batch_entry_id"] = int(entry["id"])
        if existing_item is None:
            self.repository.upsert_report_item(report_id, row, connection=connection)
        else:
            connection.execute(
                "UPDATE pilot_report_items SET state=?, reason=?, record_id=?, batch_entry_id=?, details=? WHERE id=?",
                (row["state"], row.get("reason", ""), row.get("record_id"), row.get("batch_entry_id"),
                 json.dumps(row.get("details", {}), ensure_ascii=False, sort_keys=True),
                 existing_item["id"]),
            )
        if final_state == "landed":
            self.repository.land_entry(
                int(entry["id"]), actor.user_id,
                {"record_id": int(plan["id"]), "report_id": report_id, "vessel": item["vessel"]},
                connection=connection,
            )
        return final_state

    def list_pilot_reports(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_pilot_reports()

    def get_pilot_report(self, actor: Actor, report_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_pilot_report(report_id)

    # ---- 冲突草稿 ----

    def list_drafts(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_drafts()

    def apply_draft(self, actor: Actor, draft_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        draft = self.repository.get_draft(draft_id)
        if draft["status"] != "open":
            raise Conflict("冲突草稿已处理，状态：%s" % draft["status"])
        result: Dict[str, Any] = {}
        if draft["target_type"] == "record":
            if draft["action"] != "reschedule":
                raise Conflict("不支持的草稿动作：%s" % draft["action"])
            record = self.repository.get(int(draft["target_id"]))
            result = self.reschedule(actor, int(draft["target_id"]), int(record["version"]),
                                     draft["payload"], save_draft_on_conflict=False)
        elif draft["target_type"] == "tide_window":
            if draft["action"] != "update":
                raise Conflict("不支持的草稿动作：%s" % draft["action"])
            window = self.repository.get_tide_window(int(draft["target_id"]))
            result = self.update_tide_window(actor, int(draft["target_id"]), int(window["version"]),
                                             draft["payload"], save_draft_on_conflict=False)
        else:
            raise Conflict("未知草稿类型：%s" % draft["target_type"])
        self.repository.mark_draft_status(draft_id, "applied")
        return {"draft_id": draft_id, "status": "applied", "target": result}

    def discard_draft(self, actor: Actor, draft_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_draft(draft_id)
        self.repository.mark_draft_status(draft_id, "discarded")
        return {"draft_id": draft_id, "status": "discarded"}

    # ---- 审计 ----

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def entity_timeline(self, actor: Actor, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.entity_timeline(entity_type, entity_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
