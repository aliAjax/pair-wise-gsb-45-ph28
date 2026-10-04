"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


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
                    tide_window_id INTEGER,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tide_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    date TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    open_hour INTEGER NOT NULL,
                    close_hour INTEGER NOT NULL,
                    vessel_quota INTEGER NOT NULL,
                    tide_level_m REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(date, direction)
                );
                CREATE TABLE IF NOT EXISTS channel_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tide_window_id INTEGER NOT NULL REFERENCES tide_windows(id),
                    generation INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    invalidated_at TEXT,
                    superseded_run_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(tide_window_id, generation, seq)
                );
                CREATE TABLE IF NOT EXISTS batch_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES channel_batches(id),
                    tide_window_id INTEGER NOT NULL REFERENCES tide_windows(id),
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    vessel TEXT NOT NULL,
                    dangerous_goods INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    generation INTEGER NOT NULL,
                    released_at TEXT,
                    landed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recompute_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tide_window_id INTEGER NOT NULL REFERENCES tide_windows(id),
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '{}',
                    generation INTEGER,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conflict_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_type TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    applied_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pilot_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    notice_id TEXT NOT NULL UNIQUE,
                    pilot_id TEXT NOT NULL,
                    tolerance_hours INTEGER NOT NULL,
                    matched INTEGER NOT NULL DEFAULT 0,
                    suspended INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    retransmit INTEGER NOT NULL DEFAULT 0,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pilot_report_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL REFERENCES pilot_reports(id),
                    vessel TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    actual_hour INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    record_id INTEGER,
                    batch_entry_id INTEGER,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(report_id, vessel)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER,
                    entity_type TEXT NOT NULL DEFAULT 'record',
                    entity_id INTEGER,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_records_window ON records(tide_window_id);
                CREATE INDEX IF NOT EXISTS idx_batches_window ON channel_batches(tide_window_id, generation);
                CREATE INDEX IF NOT EXISTS idx_entries_record ON batch_entries(record_id);
                CREATE INDEX IF NOT EXISTS idx_entries_window ON batch_entries(tide_window_id, status);
                CREATE INDEX IF NOT EXISTS idx_runs_window ON recompute_runs(tide_window_id, id);
                CREATE INDEX IF NOT EXISTS idx_drafts_target ON conflict_drafts(target_type, target_id, status);
                CREATE INDEX IF NOT EXISTS idx_report_items_record ON pilot_report_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)")}
            if "tide_window_id" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN tide_window_id INTEGER")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ---- 靠泊计划 ----

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str,
               tide_window_id: Optional[int] = None, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO records(reference,state,version,payload,tide_window_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (reference, state, 1, _dumps(payload), tide_window_id, actor_id, actor_id, now, now),
            )
            record_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, "record", record_id, "created", actor_id, 1, _dumps({"state": state, "tide_window_id": tide_window_id}), now),
            )
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if own:
                connection.commit()
        except sqlite3.IntegrityError as exc:
            if own:
                connection.rollback()
            raise Conflict("reference已存在") from exc
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return self._row(row)

    def get(self, record_id: int, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        own = connection is None
        if own:
            connection = self._connect()
        row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if own:
            connection.close()
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str,
               action: str, details: Dict[str, Any], tide_window_id: Optional[int] = None,
               connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        try:
            if row is None:
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            if tide_window_id is None:
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                    (state, version, _dumps(payload), actor_id, now, record_id),
                )
            else:
                connection.execute(
                    "UPDATE records SET state=?,version=?,payload=?,tide_window_id=?,updated_by=?,updated_at=? WHERE id=?",
                    (state, version, _dumps(payload), tide_window_id, actor_id, now, record_id),
                )
            connection.execute(
                "INSERT INTO audit_events(record_id,entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, "record", record_id, action, actor_id, version, _dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (record_id, "record", record_id, action, actor_id, int(row["version"]), _dumps(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [self._audit_row(row) for row in rows]

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

    def create_tide_window(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO tide_windows(name,date,direction,open_hour,close_hour,vessel_quota,tide_level_m,status,version,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (payload["name"], payload["date"], payload["direction"], payload["open_hour"],
                     payload["close_hour"], payload["vessel_quota"], payload["tide_level_m"],
                     "open", 1, actor_id, actor_id, now, now),
                )
                window_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    ("tide_window", window_id, "created", actor_id, 1, _dumps(payload), now),
                )
                row = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("该日期与流向的潮位窗口已存在") from exc
        return dict(row)

    def get_tide_window(self, window_id: int, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        own = connection is None
        if own:
            connection = self._connect()
        row = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
        if own:
            connection.close()
        if row is None:
            raise NotFound("潮位窗口不存在")
        return dict(row)

    def list_tide_windows(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tide_windows ORDER BY date DESC, id DESC").fetchall()
        return [dict(row) for row in rows]

    def update_tide_window(self, window_id: int, expected_version: int, changes: Dict[str, Any],
                           actor_id: str, connection: sqlite3.Connection = None) -> Dict[str, Any]:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
        try:
            if row is None:
                raise NotFound("潮位窗口不存在")
            if int(row["version"]) != int(expected_version):
                raise Conflict("潮位窗口版本冲突，请刷新后重试")
            current = dict(row)
            current.update(changes)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE tide_windows SET name=?,open_hour=?,close_hour=?,vessel_quota=?,tide_level_m=?,version=?,updated_by=?,updated_at=? WHERE id=?",
                (current["name"], current["open_hour"], current["close_hour"], current["vessel_quota"],
                 current["tide_level_m"], version, actor_id, now, window_id),
            )
            connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                ("tide_window", window_id, "updated", actor_id, version,
                 _dumps({"changes": changes, "from_version": int(expected_version)}), now),
            )
            result = connection.execute("SELECT * FROM tide_windows WHERE id=?", (window_id,)).fetchone()
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return dict(result)

    def plans_for_window(self, window_id: int, connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        rows = connection.execute(
            "SELECT * FROM records WHERE tide_window_id=? ORDER BY id", (window_id,)
        ).fetchall()
        if own:
            connection.close()
        return [self._row(row) for row in rows]

    # ---- 批次与重算 ----

    def next_batch_generation(self, window_id: int, connection: sqlite3.Connection = None) -> int:
        own = connection is None
        if own:
            connection = self._connect()
        row = connection.execute(
            "SELECT COALESCE(MAX(generation), 0) AS g FROM channel_batches WHERE tide_window_id=?",
            (window_id,),
        ).fetchone()
        if own:
            connection.close()
        return int(row["g"]) + 1

    def active_entries_for_recompute(self, window_id: int, connection: sqlite3.Connection = None) -> List[Dict[str, Any]]:
        """已放行/落地的条目继续占用新批次额度。"""
        own = connection is None
        if own:
            connection = self._connect()
        rows = connection.execute(
            """
            SELECT e.* FROM batch_entries e
            JOIN channel_batches b ON b.id = e.batch_id
            WHERE e.tide_window_id=? AND b.status='active' AND e.status IN ('released','landed')
            ORDER BY e.id
            """,
            (window_id,),
        ).fetchall()
        if own:
            connection.close()
        return [dict(row) for row in rows]

    def insert_run(self, window_id: int, trigger: str, status: str, actor_id: str,
                   reason: str = "", details: Dict[str, Any] = None, generation: Optional[int] = None,
                   connection: sqlite3.Connection = None) -> int:
        own = connection is None
        if own:
            connection = self._connect()
        cursor = connection.execute(
            "INSERT INTO recompute_runs(tide_window_id,trigger,status,reason,details,generation,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (window_id, trigger, status, reason, _dumps(details or {}), generation, actor_id, _now()),
        )
        run_id = int(cursor.lastrowid)
        if own:
            connection.commit()
        return run_id

    def replace_batches(self, window_id: int, groups: List[List[Dict[str, Any]]], generation: int,
                        run_id: int, actor_id: str, connection: sqlite3.Connection) -> None:
        now = _now()
        connection.execute(
            "UPDATE channel_batches SET status='invalid', invalidated_at=?, superseded_run_id=? WHERE tide_window_id=? AND status='active'",
            (now, run_id, window_id),
        )
        connection.execute(
            """
            UPDATE batch_entries AS e
            SET status='invalid', updated_at=?
            FROM channel_batches AS b
            WHERE e.batch_id=b.id AND b.tide_window_id=? AND b.status='invalid'
              AND b.superseded_run_id=? AND e.status IN ('waiting','released','landed')
            """,
            (now, window_id, run_id),
        )
        for seq, group in enumerate(groups, start=1):
            cursor = connection.execute(
                "INSERT INTO channel_batches(tide_window_id,generation,seq,status,created_at) VALUES(?,?,?, 'active', ?)",
                (window_id, generation, seq, now),
            )
            batch_id = int(cursor.lastrowid)
            for item in group:
                status = item.get("status", "waiting") if item.get("pinned") else "waiting"
                connection.execute(
                    """
                    INSERT INTO batch_entries(batch_id,tide_window_id,record_id,vessel,dangerous_goods,pinned,status,generation,released_at,landed_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (batch_id, window_id, item["record_id"], item["vessel"],
                     1 if item.get("dangerous_goods") else 0, 1 if item.get("pinned") else 0,
                     status, generation, item.get("released_at"), item.get("landed_at"), now, now),
                )
        connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
            ("tide_window", window_id, "batches_recomputed", actor_id, generation,
             _dumps({"run_id": run_id, "generation": generation, "batch_count": len(groups)}), now),
        )

    def list_batches(self, window_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            window = self.get_tide_window(window_id, connection)
            rows = connection.execute(
                "SELECT * FROM channel_batches WHERE tide_window_id=? ORDER BY generation DESC, seq", (window_id,)
            ).fetchall()
            batches = []
            for row in rows:
                batch = dict(row)
                entries = connection.execute(
                    "SELECT * FROM batch_entries WHERE batch_id=? ORDER BY id", (batch["id"],)
                ).fetchall()
                batch["entries"] = [dict(entry) for entry in entries]
                batches.append(batch)
            run_row = connection.execute(
                "SELECT * FROM recompute_runs WHERE tide_window_id=? ORDER BY id DESC LIMIT 1", (window_id,)
            ).fetchone()
        return {"window": window, "batches": batches,
                "latest_run": dict(run_row) if run_row else None}

    def latest_pending_run(self, window_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recompute_runs WHERE tide_window_id=? AND status='failed' ORDER BY id DESC LIMIT 1",
                (window_id,),
            ).fetchone()
            if row is None:
                return None
            failed_id = int(row["id"])
            later = connection.execute(
                "SELECT 1 FROM recompute_runs WHERE tide_window_id=? AND id>? AND status='success' LIMIT 1",
                (window_id, failed_id),
            ).fetchone()
        return None if later else dict(row)

    def list_runs(self, window_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recompute_runs WHERE tide_window_id=? ORDER BY id DESC", (window_id,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def invalidate_entries_for_record(self, record_id: int, actor_id: str, reason: str,
                                      connection: sqlite3.Connection = None) -> Optional[int]:
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        window_id = None
        try:
            row = connection.execute(
                """
                SELECT e.tide_window_id AS wid FROM batch_entries e
                JOIN channel_batches b ON b.id = e.batch_id
                WHERE e.record_id=? AND b.status='active' AND e.status!='invalid' LIMIT 1
                """,
                (record_id,),
            ).fetchone()
            if row is not None:
                window_id = int(row["wid"])
                now = _now()
                connection.execute(
                    """
                    UPDATE batch_entries SET status='invalid', updated_at=?
                    WHERE record_id=? AND status!='invalid' AND batch_id IN (
                        SELECT id FROM channel_batches WHERE tide_window_id=? AND status='active'
                    )
                    """,
                    (now, record_id, window_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, "record", record_id, "batch_entry_invalidated", actor_id, 0,
                     _dumps({"reason": reason, "tide_window_id": window_id}), now),
                )
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return window_id

    # ---- 闸口放行 ----

    def get_active_entry_for_record(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT e.*, b.status AS batch_status, b.seq AS batch_seq, b.generation AS batch_generation
                FROM batch_entries e
                JOIN channel_batches b ON b.id = e.batch_id
                WHERE e.record_id=? AND b.status='active' AND e.status!='invalid'
                ORDER BY b.generation DESC, b.seq LIMIT 1
                """,
                (record_id,),
            ).fetchone()
        return dict(row) if row else None

    def release_entry(self, entry_id: int, actor_id: str, now_hour: int) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batch_entries WHERE id=?", (entry_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次条目不存在")
            if dict(row)["status"] != "waiting":
                connection.rollback()
                raise Conflict("批次条目状态为%s，不可放行" % dict(row)["status"])
            connection.execute(
                "UPDATE batch_entries SET status='released', released_at=?, updated_at=? WHERE id=?",
                (now, now, entry_id),
            )
            connection.execute(
                "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                ("batch_entry", entry_id, "gate_released", actor_id, int(row["generation"]),
                 _dumps({"record_id": row["record_id"], "now_hour": now_hour}), now),
            )
            result = connection.execute("SELECT * FROM batch_entries WHERE id=?", (entry_id,)).fetchone()
            connection.commit()
        return dict(result)

    # ---- 引航对账 ----

    def latest_plan_for_vessel(self, vessel: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM records WHERE json_extract(payload, '$.vessel')=? AND state!='cancelled' ORDER BY id DESC LIMIT 1",
                (vessel,),
            ).fetchone()
        return self._row(row) if row else None

    def create_pilot_report(self, notice_id: str, pilot_id: str, tolerance: int, retransmit: bool,
                            actor_id: str, connection: sqlite3.Connection = None) -> int:
        now = _now()
        own = connection is None
        if own:
            connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO pilot_reports(notice_id,pilot_id,tolerance_hours,retransmit,actor_id,created_at) VALUES(?,?,?,?,?,?)",
                (notice_id, pilot_id, tolerance, 1 if retransmit else 0, actor_id, now),
            )
            report_id = int(cursor.lastrowid)
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return report_id

    def get_pilot_report_by_notice(self, notice_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM pilot_reports WHERE notice_id=?", (notice_id,)).fetchone()
        return dict(row) if row else None

    def list_pilot_reports(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM pilot_reports ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def landed_vessels_for_notice(self, report_id: int) -> set:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT vessel FROM pilot_report_items WHERE report_id=? AND state='landed'", (report_id,)
            ).fetchall()
        return {row["vessel"] for row in rows}

    def get_report_item(self, report_id: int, vessel: str,
                        connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        row = connection.execute(
            "SELECT * FROM pilot_report_items WHERE report_id=? AND vessel=?", (report_id, vessel)
        ).fetchone()
        if own:
            connection.close()
        return dict(row) if row else None

    def upsert_report_item(self, report_id: int, item: Dict[str, Any], connection: sqlite3.Connection = None) -> str:
        """同一回传单同一船舶幂等：已存在则跳过，返回 inserted/skipped。"""
        own = connection is None
        if own:
            connection = self._connect()
        existing = self.get_report_item(report_id, item["vessel"], connection)
        if existing is not None:
            if own:
                connection.close()
            return "skipped"
        connection.execute(
            """
            INSERT INTO pilot_report_items(report_id,vessel,direction,actual_hour,state,reason,record_id,batch_entry_id,details,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (report_id, item["vessel"], item["direction"], item["actual_hour"],
             item["state"], item.get("reason", ""), item.get("record_id"), item.get("batch_entry_id"),
             _dumps(item.get("details", {})), _now()),
        )
        if own:
            connection.commit()
        return "inserted"

    def mark_report_totals(self, report_id: int, matched: int, suspended: int, skipped: int,
                           connection: sqlite3.Connection = None) -> None:
        own = connection is None
        if own:
            connection = self._connect()
        connection.execute(
            "UPDATE pilot_reports SET matched=?, suspended=?, skipped=? WHERE id=?",
            (matched, suspended, skipped, report_id),
        )
        if own:
            connection.commit()

    def mark_report_retransmit(self, report_id: int, connection: sqlite3.Connection = None) -> None:
        own = connection is None
        if own:
            connection = self._connect()
        connection.execute("UPDATE pilot_reports SET retransmit=1 WHERE id=?", (report_id,))
        if own:
            connection.commit()

    def land_entry(self, entry_id: int, actor_id: str, details: Dict[str, Any],
                   connection: sqlite3.Connection = None) -> bool:
        """waiting/released -> landed，幂等。已是landed返回False。"""
        own = connection is None
        if own:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM batch_entries WHERE id=?", (entry_id,)).fetchone()
        landed = False
        try:
            if row is not None and dict(row)["status"] in ("waiting", "released"):
                now = _now()
                connection.execute(
                    "UPDATE batch_entries SET status='landed', landed_at=COALESCE(landed_at,?), updated_at=? WHERE id=?",
                    (now, now, entry_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?,?)",
                    ("batch_entry", entry_id, "pilot_landed", actor_id, int(row["generation"]),
                     _dumps(details), now),
                )
                landed = True
            if own:
                connection.commit()
        except Exception:
            if own:
                connection.rollback()
            raise
        finally:
            if own:
                connection.close()
        return landed

    def get_entry(self, entry_id: int, connection: sqlite3.Connection = None) -> Optional[Dict[str, Any]]:
        own = connection is None
        if own:
            connection = self._connect()
        row = connection.execute("SELECT * FROM batch_entries WHERE id=?", (entry_id,)).fetchone()
        if own:
            connection.close()
        return dict(row) if row else None

    def get_pilot_report(self, report_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM pilot_reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise NotFound("回传单不存在")
            items = connection.execute(
                "SELECT * FROM pilot_report_items WHERE report_id=? ORDER BY id", (report_id,)
            ).fetchall()
        result = dict(row)
        result["items"] = [self._item_row(item) for item in items]
        return result

    # ---- 冲突草稿 ----

    def save_conflict_draft(self, target_type: str, target_id: int, action: str, base_version: int,
                            actor_id: str, payload: Dict[str, Any], note: str) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO conflict_drafts(target_type,target_id,action,base_version,actor_id,payload,note,status,created_at) VALUES(?,?,?,?,?,?,?, 'open', ?)",
                (target_type, target_id, action, base_version, actor_id, _dumps(payload), note, now),
            )
            draft_id = int(cursor.lastrowid)
        return draft_id

    def get_draft(self, draft_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM conflict_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            raise NotFound("冲突草稿不存在")
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_drafts(self, target_type: str = None, target_id: int = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM conflict_drafts WHERE status='open'"
        args: List[Any] = []
        if target_type:
            sql += " AND target_type=?"
            args.append(target_type)
        if target_id is not None:
            sql += " AND target_id=?"
            args.append(target_id)
        sql += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def mark_draft_status(self, draft_id: int, status: str) -> None:
        with self._connect() as connection:
            if status == "open":
                connection.execute(
                    "UPDATE conflict_drafts SET status='open', applied_at=NULL WHERE id=?", (draft_id,)
                )
            else:
                connection.execute(
                    "UPDATE conflict_drafts SET status=?, applied_at=? WHERE id=?",
                    (status, _now(), draft_id),
                )

    # ---- 审计 ----

    def entity_timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        return [self._audit_row(row) for row in rows]

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    @staticmethod
    def _run_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    @staticmethod
    def _item_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item
