"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_WINDOW = {"code": "DEFAULT", "start_hour": 0, "end_hour": 24, "batch_quota": 10, "max_batches": 100}


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tide_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    batch_quota INTEGER NOT NULL,
                    max_batches INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transit_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id INTEGER NOT NULL REFERENCES tide_windows(id),
                    generation INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES transit_batches(id) ON DELETE CASCADE,
                    plan_id INTEGER NOT NULL REFERENCES records(id),
                    snapshot TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recompute_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    window_id INTEGER NOT NULL REFERENCES tide_windows(id),
                    generation INTEGER,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflict_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES records(id),
                    action TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    server_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pilot_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL UNIQUE,
                    pilot_id TEXT NOT NULL,
                    raw_items TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pilot_report_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL REFERENCES pilot_reports(id) ON DELETE CASCADE,
                    plan_id INTEGER,
                    vessel TEXT NOT NULL,
                    movement TEXT NOT NULL,
                    actual_hour INTEGER NOT NULL,
                    landed INTEGER NOT NULL,
                    quota_consumed INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL REFERENCES records(id),
                    movement TEXT NOT NULL,
                    report_id INTEGER NOT NULL REFERENCES pilot_reports(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(plan_id, movement)
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batches_window ON transit_batches(window_id, status, generation);
                CREATE INDEX IF NOT EXISTS idx_members_batch ON batch_members(batch_id);
                CREATE INDEX IF NOT EXISTS idx_members_plan ON batch_members(plan_id);
                CREATE INDEX IF NOT EXISTS idx_drafts_plan ON conflict_drafts(plan_id, status);
                CREATE INDEX IF NOT EXISTS idx_report_items ON pilot_report_items(report_id);
                """
            )
            existing = connection.execute("SELECT COUNT(*) AS n FROM tide_windows").fetchone()
            if int(existing["n"]) == 0:
                connection.execute(
                    "INSERT INTO tide_windows(code,start_hour,end_hour,batch_quota,max_batches,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (DEFAULT_WINDOW["code"], DEFAULT_WINDOW["start_hour"], DEFAULT_WINDOW["end_hour"],
                     DEFAULT_WINDOW["batch_quota"], DEFAULT_WINDOW["max_batches"], "system", _now()),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 潮位窗口 ----

    def create_window(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tide_windows(code,start_hour,end_hour,batch_quota,max_batches,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (data["code"], data["start_hour"], data["end_hour"], data["batch_quota"],
                     data["max_batches"], actor_id, now),
                )
                row = connection.execute("SELECT * FROM tide_windows WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("潮位窗口编码已存在") from exc
        return dict(row)

    def list_windows(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tide_windows ORDER BY start_hour, id").fetchall()
        return [dict(row) for row in rows]

    def get_window(self, window_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFound("潮位窗口不存在")
        return dict(row)

    def window_for_hour(self, hour: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tide_windows WHERE start_hour<=? AND ?<end_hour ORDER BY (end_hour-start_hour), id LIMIT 1",
                (hour, hour),
            ).fetchone()
        return dict(row) if row else None

    # ---- 通行批次 ----

    @staticmethod
    def _batch_payload(connection: sqlite3.Connection, batch_id: int) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT snapshot FROM batch_members WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return [json.loads(row["snapshot"]) for row in rows]

    def list_batches(self, window_id: Optional[int] = None, active_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM transit_batches WHERE 1=1"
        params: List[Any] = []
        if active_only:
            sql += " AND status='active'"
        if window_id is not None:
            sql += " AND window_id=?"
            params.append(window_id)
        sql += " ORDER BY window_id, generation DESC, seq"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["members"] = self._batch_payload(connection, item["id"])
                result.append(item)
        return result

    def active_plan_ids(self, connection: sqlite3.Connection, window_id: Optional[int] = None) -> set:
        sql = (
            "SELECT DISTINCT m.plan_id AS plan_id FROM batch_members m "
            "JOIN transit_batches b ON b.id=m.batch_id WHERE b.status='active'"
        )
        params: List[Any] = []
        if window_id is not None:
            sql += " AND b.window_id=?"
            params.append(window_id)
        return {int(row["plan_id"]) for row in connection.execute(sql, params).fetchall()}

    def gate_list(self, window_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """闸口放行视图：只有active批次里的船才会出现，旧批次整代失效。"""
        return self.list_batches(window_id=window_id, active_only=True)

    def latest_runs(self, window_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM recompute_runs WHERE 1=1"
        params: List[Any] = []
        if window_id is not None:
            sql += " AND window_id=?"
            params.append(window_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---- 改期 + 批次重算（单事务，失败回滚并保留旧批次） ----

    def reschedule_and_rebatch(
        self,
        *,
        plan_id: int,
        expected_version: int,
        new_payload: Dict[str, Any],
        actor_id: str,
        window_ids: List[int],
        planner: Callable[[sqlite3.Connection, Dict[str, Any]], List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (plan_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            # 先改计划，随后在同一事务内排批次；任一步失败整体回滚，旧批次原样保留。
            connection.execute(
                "UPDATE records SET version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (int(expected_version) + 1, json.dumps(new_payload, ensure_ascii=False, sort_keys=True), actor_id, now, plan_id),
            )
            planned: Dict[int, List[Dict[str, Any]]] = {}
            for window_id in window_ids:
                window = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
                if window is None:
                    connection.rollback()
                    raise NotFound("潮位窗口不存在")
                planned[int(window_id)] = planner(connection, dict(window))
            new_batches = self._commit_rebatch(connection, window_ids, planned, actor_id, trigger="reschedule", now=now)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (plan_id, "reschedule", actor_id, int(expected_version) + 1,
                 json.dumps({"summary": "计划改期，通行批次已重算", "window_ids": window_ids,
                             "batch_generations": {wid: b[0]["generation"] for wid, b in new_batches.items() if b}},
                            ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (plan_id,)).fetchone()
            connection.commit()
            return {"record": self._row(result), "batches": {str(wid): batches for wid, batches in new_batches.items()}}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rebatch_windows(
        self,
        *,
        window_ids: List[int],
        actor_id: str,
        trigger: str,
        planner: Callable[[sqlite3.Connection, Dict[str, Any]], List[Dict[str, Any]]],
    ) -> Dict[int, List[Dict[str, Any]]]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            planned: Dict[int, List[Dict[str, Any]]] = {}
            for window_id in window_ids:
                window = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
                if window is None:
                    connection.rollback()
                    raise NotFound("潮位窗口不存在")
                planned[int(window_id)] = planner(connection, dict(window))
            new_batches = self._commit_rebatch(connection, window_ids, planned, actor_id, trigger=trigger, now=now)
            connection.commit()
            return new_batches
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _commit_rebatch(self, connection: sqlite3.Connection, window_ids: List[int],
                        planned: Dict[int, List[Dict[str, Any]]], actor_id: str, *, trigger: str, now: str) -> Dict[int, List[Dict[str, Any]]]:
        """旧批次先置superseded，再写入新一代active批次。调用方已持有写事务。"""
        output: Dict[int, List[Dict[str, Any]]] = {}
        for window_id in window_ids:
            gen_row = connection.execute(
                "SELECT COALESCE(MAX(generation),0) AS g FROM transit_batches WHERE window_id=?", (window_id,)
            ).fetchone()
            generation = int(gen_row["g"]) + 1
            connection.execute(
                "UPDATE transit_batches SET status='superseded', superseded_at=? WHERE window_id=? AND status='active'",
                (now, window_id),
            )
            batches_out: List[Dict[str, Any]] = []
            for spec in planned[window_id]:
                cursor = connection.execute(
                    "INSERT INTO transit_batches(window_id,generation,seq,status,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (window_id, generation, spec["seq"], "active", actor_id, now),
                )
                batch_id = int(cursor.lastrowid)
                for member in spec["members"]:
                    connection.execute(
                        "INSERT INTO batch_members(batch_id,plan_id,snapshot) VALUES(?,?,?)",
                        (batch_id, member["plan_id"], json.dumps(member, ensure_ascii=False, sort_keys=True)),
                    )
                item = dict(connection.execute("SELECT * FROM transit_batches WHERE id=?", (batch_id,)).fetchone())
                item["members"] = self._batch_payload(connection, batch_id)
                batches_out.append(item)
            connection.execute(
                "INSERT INTO recompute_runs(window_id,generation,status,trigger,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (window_id, generation, "success", trigger, "", actor_id, now),
            )
            output[window_id] = batches_out
        return output

    def record_failed_recompute(self, window_ids: List[int], reason: str, actor_id: str, trigger: str) -> None:
        """重算失败后留痕：旧批次仍然active，值班员凭原因修正后重试。"""
        now = _now()
        with self._connect() as connection:
            for window_id in window_ids:
                connection.execute(
                    "INSERT INTO recompute_runs(window_id,generation,status,trigger,reason,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (window_id, None, "failed", trigger, reason, actor_id, now),
                )

    # ---- 冲突草稿 ----

    def save_conflict_draft(self, plan_id: int, action: str, base_version: int, server_version: int,
                            payload: Dict[str, Any], actor_id: str, note: str) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO conflict_drafts(plan_id,action,base_version,server_version,payload,actor_id,note,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, action, base_version, server_version,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, note, "pending", now),
            )
            return int(cursor.lastrowid)

    def get_conflict_draft(self, draft_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM conflict_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound("冲突草稿不存在")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_conflict_drafts(self, plan_id: Optional[int] = None, status: str = "pending") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM conflict_drafts WHERE status=?"
        params: List[Any] = [status]
        if plan_id is not None:
            sql += " AND plan_id=?"
            params.append(plan_id)
        sql += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def dismiss_conflict_draft(self, draft_id: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE conflict_drafts SET status='discarded' WHERE id=? AND status='pending'", (draft_id,))
            if cursor.rowcount == 0:
                raise NotFound("待处理冲突草稿不存在")

    # ---- 引航回传对账与额度 ----

    def submit_pilot_report(self, ticket: str, pilot_id: str, items: List[Dict[str, Any]],
                            reconcile: Callable[[sqlite3.Connection, List[Dict[str, Any]], List[Dict[str, Any]]], List[Dict[str, Any]]]) -> Dict[str, Any]:
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            header = connection.execute("SELECT * FROM pilot_reports WHERE ticket=?", (ticket,)).fetchone()
            if header is None:
                cursor = connection.execute(
                    "INSERT INTO pilot_reports(ticket,pilot_id,raw_items,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (ticket, pilot_id, json.dumps(items, ensure_ascii=False), now, now),
                )
                report_id = int(cursor.lastrowid)
            else:
                report_id = int(header["id"])
            landed_keys = {
                (str(r["vessel"]), str(r["movement"]))
                for r in connection.execute(
                    "SELECT vessel,movement FROM pilot_report_items WHERE report_id=? AND landed=1", (report_id,)
                ).fetchall()
            }
            # 重传同一回传单：已经落地的船直接跳过，只补还没落地的。
            pending = [it for it in items if (str(it.get("vessel", "")).strip(), str(it.get("movement", ""))) not in landed_keys]
            if pending:
                # 全部计划用于船名对账；"是否在有效批次中"由active批次成员集合单独判断。
                rows = connection.execute("SELECT * FROM records").fetchall()
                plans = [self._row(row) for row in rows]
                results = reconcile(connection, plans, pending)
                for result in results:
                    quota_consumed = 0
                    if result["landed"]:
                        cursor = connection.execute(
                            "INSERT OR IGNORE INTO quota_usage(plan_id,movement,report_id,created_at) VALUES(?,?,?,?)",
                            (result["plan_id"], result["movement"], report_id, now),
                        )
                        quota_consumed = cursor.rowcount
                    connection.execute(
                        "INSERT INTO pilot_report_items(report_id,plan_id,vessel,movement,actual_hour,landed,quota_consumed,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (report_id, result.get("plan_id"), result["vessel"], result["movement"],
                         result["actual_hour"], 1 if result["landed"] else 0, quota_consumed, result.get("reason", ""), now),
                    )
                connection.execute("UPDATE pilot_reports SET raw_items=?, updated_at=? WHERE id=?",
                                   (json.dumps(items, ensure_ascii=False), now, report_id))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_pilot_report_by_id(report_id)

    def get_pilot_report_by_id(self, report_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            header = connection.execute("SELECT * FROM pilot_reports WHERE id=?", (report_id,)).fetchone()
            if header is None:
                raise NotFound("回传单不存在")
            items = connection.execute("SELECT * FROM pilot_report_items WHERE report_id=? ORDER BY id", (report_id,)).fetchall()
        item_list = []
        for row in items:
            d = dict(row)
            d["landed"] = bool(d["landed"])
            d["quota_consumed"] = bool(d["quota_consumed"])
            item_list.append(d)
        result = dict(header)
        result["raw_items"] = json.loads(result["raw_items"])
        result["items"] = item_list
        # 同一船重传可能留下多条对账记录，按(船,动作)取最新一条作为当前结论。
        latest: Dict[tuple, Dict[str, Any]] = {}
        for it in item_list:
            latest[(it["vessel"], it["movement"])] = it
        conclusions = list(latest.values())
        result["landed"] = sum(1 for it in conclusions if it["landed"])
        result["suspended"] = sum(1 for it in conclusions if not it["landed"])
        result["quota_consumed"] = sum(1 for it in conclusions if it["quota_consumed"])
        return result

    def get_pilot_report(self, ticket: str) -> Dict[str, Any]:
        with self._connect() as connection:
            header = connection.execute("SELECT id FROM pilot_reports WHERE ticket=?", (ticket,)).fetchone()
        if header is None:
            raise NotFound("回传单不存在")
        return self.get_pilot_report_by_id(int(header["id"]))

    def list_pilot_reports(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT id FROM pilot_reports ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self.get_pilot_report_by_id(int(row["id"])) for row in rows]
