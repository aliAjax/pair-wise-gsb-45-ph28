import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, RecomputeFailed, ValidationError


CONTROLLER = Actor("ctrl-1", "port_controller")
DUTY = Actor("duty-1", "duty_officer")
PILOT = Actor("pilot-1", "pilot")

WINDOW_IN = {"name": "早潮进口", "date": "2026-10-04", "direction": "inbound",
             "open_hour": 4, "close_hour": 10, "vessel_quota": 2, "tide_level_m": 12.0}


def plan_data(vessel="HaiYun", berth="B12", eta=6, etd=18, draft=10.2, dangerous=False):
    return {"vessel": vessel, "berth": berth, "vessel_length_m": 180, "berth_length_m": 220,
            "draft_m": draft, "berth_depth_m": 11.5, "eta_hour": eta, "etd_hour": etd,
            "risk_level": "medium", "dangerous_goods": dangerous, "dangerous_class": "3" if dangerous else "",
            "direction": "inbound"}


class TideWindowBatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.window = self.service.create_tide_window(CONTROLLER, WINDOW_IN)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, **overrides):
        data = plan_data(**overrides) if overrides is not None else plan_data()
        return self.service.create(CONTROLLER, ref, data, tide_window_id=self.window["id"])

    def test_create_plan_builds_active_batches_by_quota(self):
        self._create("V-001", vessel="船A", berth="B1")
        self._create("V-002", vessel="船B", berth="B2")
        self._create("V-003", vessel="船C", berth="B3")
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        active = [b for b in view["batches"] if b["status"] == "active"]
        self.assertEqual(len(active), 2)
        self.assertEqual([len(b["entries"]) for b in active], [2, 1])
        self.assertIsNone(view["pending_failure"])

    def test_dangerous_goods_goes_solo_batch(self):
        self._create("V-101", vessel="危A", berth="B1", dangerous=True)
        self._create("V-102", vessel="普A", berth="B2", dangerous=False)
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        active = [b for b in view["batches"] if b["status"] == "active"]
        self.assertEqual(len(active), 2)
        self.assertEqual([len(b["entries"]) for b in active], [1, 1])

    def test_plan_outside_window_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.create(CONTROLLER, "V-201", plan_data(vessel="晚船", eta=20, etd=23),
                                tide_window_id=self.window["id"])

    def test_window_change_recomputes_and_invalidates_old_batches(self):
        rec = self._create("V-301", vessel="船A", berth="B1")
        before = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        self.assertEqual(len(before["batches"]), 1)
        # 潮位提前关闭，船的eta=6落在新窗口外，重算失败
        with self.assertRaises(RecomputeFailed):
            self.service.update_tide_window(CONTROLLER, self.window["id"], 1, {"close_hour": 5})
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        self.assertIsNotNone(view["pending_failure"])
        # 旧批次仍在（保留），但闸口被失败重算拦住
        with self.assertRaises(Conflict):
            self.service.gate_release(DUTY, rec["id"], 6)

    def test_failed_recompute_then_fix_and_retry(self):
        self._create("V-401", vessel="船A", berth="B1")
        self._create("V-402", vessel="船B", berth="B2")
        # 潮位降到无法满足吃水 -> 重算失败，旧批次保留
        with self.assertRaises(RecomputeFailed):
            self.service.update_tide_window(CONTROLLER, self.window["id"], 1, {"tide_level_m": 9.0})
        failed_view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        self.assertIsNotNone(failed_view["pending_failure"])
        # 值班员在未恢复前重试，仍然失败并继续留痕
        with self.assertRaises(RecomputeFailed):
            self.service.recompute_window(DUTY, self.window["id"], trigger="manual_retry")
        # 恢复潮位（窗口更新自带重算）-> 成功，失败标记清除
        win = self.service.repository.get_tide_window(self.window["id"])
        self.service.update_tide_window(CONTROLLER, win["id"], win["version"], {"tide_level_m": 12.0})
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        self.assertIsNone(view["pending_failure"])
        active = [b for b in view["batches"] if b["status"] == "active"]
        self.assertEqual(len(active), 1)
        # 手动重试在健康窗口上产生新一代批次
        result = self.service.recompute_window(DUTY, self.window["id"], trigger="manual_retry")
        self.assertEqual(result["batch_count"], 1)
        self.assertGreaterEqual(result["generation"], 2)

    def test_reschedule_keeps_plan_and_batches_when_recompute_fails(self):
        rec = self._create("V-501", vessel="船A", berth="B1")
        # 改期到窗口外：失败，计划与旧批次都保留，失败原因留痕
        before_version = rec["version"]
        with self.assertRaises(RecomputeFailed):
            self.service.act(DUTY, rec["id"], before_version, "reschedule",
                             {"eta_hour": 20, "etd_hour": 22})
        again = self.service.get_record(CONTROLLER, rec["id"])
        self.assertEqual(again["payload"]["eta_hour"], 6)
        self.assertEqual(again["version"], before_version)
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        self.assertIsNotNone(view["pending_failure"])
        runs = self.service.list_recompute_runs(DUTY, self.window["id"])
        failed = [r for r in runs if r["status"] == "failed"]
        self.assertTrue(failed and failed[0]["reason"])
        # 改期到窗口内成功，批次换代
        fixed = self.service.act(DUTY, again["id"], again["version"], "reschedule", {"eta_hour": 7})
        self.assertEqual(fixed["version"], before_version + 1)
        self.assertIsNone(self.service.plan_batches_view(CONTROLLER, self.window["id"])["pending_failure"])


class GateReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.window = self.service.create_tide_window(CONTROLLER, WINDOW_IN)
        self.rec = self.service.create(CONTROLLER, "V-601", plan_data(vessel="船A", berth="B1"),
                                       tide_window_id=self.window["id"])

    def tearDown(self):
        self.temp.cleanup()

    def test_gate_release_requires_active_batch_and_open_hours(self):
        with self.assertRaises(Conflict):
            self.service.gate_release(DUTY, self.rec["id"], 3)  # 窗口未开
        entry = self.service.gate_release(DUTY, self.rec["id"], 6)
        self.assertEqual(entry["status"], "released")
        # 重复放行被拒绝
        with self.assertRaises(Conflict):
            self.service.gate_release(DUTY, self.rec["id"], 6)

    def test_rescheduled_ship_invalidated_batch_cannot_release_old(self):
        self.service.gate_release(DUTY, self.rec["id"], 6)
        rec = self.service.get_record(CONTROLLER, self.rec["id"])
        # 改期到窗口内7点：已放行条目锚定，批次重算
        self.service.act(DUTY, rec["id"], rec["version"], "reschedule", {"eta_hour": 7})
        view = self.service.plan_batches_view(CONTROLLER, self.window["id"])
        active = [b for b in view["batches"] if b["status"] == "active"]
        pinned = [e for b in active for e in b["entries"] if e["pinned"]]
        self.assertEqual(len(pinned), 1)
        self.assertEqual(pinned[0]["status"], "released")

    def test_plan_without_window_cannot_be_released(self):
        plain = self.service.create(CONTROLLER, "V-602", plan_data(vessel="船B", berth="B2"))
        with self.assertRaises(Conflict):
            self.service.gate_release(DUTY, plain["id"], 6)
