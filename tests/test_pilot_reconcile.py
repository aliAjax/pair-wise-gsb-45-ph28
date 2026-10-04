import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


CONTROLLER = Actor("ctrl-1", "port_controller")
PILOT = Actor("pilot-1", "pilot")

WINDOW_IN = {"name": "早潮进口", "date": "2026-10-04", "direction": "inbound",
             "open_hour": 4, "close_hour": 10, "vessel_quota": 3, "tide_level_m": 12.0}


def plan_data(vessel, berth, eta=6, etd=18):
    return {"vessel": vessel, "berth": berth, "vessel_length_m": 180, "berth_length_m": 220,
            "draft_m": 10.2, "berth_depth_m": 11.5, "eta_hour": eta, "etd_hour": etd,
            "risk_level": "medium", "dangerous_goods": False, "dangerous_class": "",
            "direction": "inbound"}


class PilotReconcileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.window = self.service.create_tide_window(CONTROLLER, WINDOW_IN)
        self.ship_a = self.service.create(CONTROLLER, "V-A", plan_data("船A", "B1"),
                                          tide_window_id=self.window["id"])
        self.ship_b = self.service.create(CONTROLLER, "V-B", plan_data("船B", "B2"),
                                          tide_window_id=self.window["id"])

    def tearDown(self):
        self.temp.cleanup()

    def _entry(self, record_id):
        return self.service.repository.get_active_entry_for_record(record_id)

    def test_report_matches_lands_and_suspends_mismatches(self):
        report = self.service.submit_pilot_report(PILOT, {
            "notice_id": "N-001", "pilot_id": "pilot-1",
            "items": [
                {"vessel": "船A", "direction": "in", "actual_hour": 6},       # 一致
                {"vessel": "船B", "direction": "in", "actual_hour": 10},      # 超容差，挂起
                {"vessel": "幽灵船", "direction": "in", "actual_hour": 6},    # 无计划，挂起
            ],
        })
        states = {item["vessel"]: item for item in report["items"]}
        self.assertEqual(states["船A"]["state"], "landed")
        self.assertEqual(states["船B"]["state"], "suspended")
        self.assertEqual(states["船B"]["reason"], "time_mismatch")
        self.assertEqual(states["幽灵船"]["state"], "suspended")
        self.assertEqual(states["幽灵船"]["reason"], "plan_not_found")
        # 一致船的批次条目落地，不一致船仍是waiting
        self.assertEqual(self._entry(self.ship_a["id"])["status"], "landed")
        self.assertEqual(self._entry(self.ship_b["id"])["status"], "waiting")
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["suspended"], 2)

    def test_retransmit_same_notice_is_idempotent_for_landed(self):
        payload = {"notice_id": "N-002", "pilot_id": "pilot-1",
                   "items": [{"vessel": "船A", "direction": "in", "actual_hour": 6}]}
        first = self.service.submit_pilot_report(PILOT, payload)
        self.assertEqual(first["matched"], 1)
        landed_entry = self._entry(self.ship_a["id"])
        landed_at = landed_entry["landed_at"]
        # 完全相同的回传单重传
        second = self.service.submit_pilot_report(PILOT, payload)
        self.assertEqual(second["retransmit"], 1)
        self.assertEqual(second["skipped"], 1)
        # 条目仍是landed，落地时刻未变，没有重复占额度
        again = self._entry(self.ship_a["id"])
        self.assertEqual(again["status"], "landed")
        self.assertEqual(again["landed_at"], landed_at)
        self.assertEqual(len(second["items"]), 1)
        events = self.service.repository.entity_timeline("batch_entry", landed_entry["id"])
        self.assertEqual(len([e for e in events if e["action"] == "pilot_landed"]), 1)

    def test_retransmit_only_completes_previously_suspended_ship(self):
        # 首单：船A落地，船B因时刻不一致挂起
        self.service.submit_pilot_report(PILOT, {
            "notice_id": "N-003", "pilot_id": "pilot-1",
            "items": [
                {"vessel": "船A", "direction": "in", "actual_hour": 6},
                {"vessel": "船B", "direction": "in", "actual_hour": 9},
            ],
        })
        self.assertEqual(self._entry(self.ship_b["id"])["status"], "waiting")
        # 重传同一通知单：船A原样（跳过），船B修正时刻 -> 补落地
        result = self.service.submit_pilot_report(PILOT, {
            "notice_id": "N-003", "pilot_id": "pilot-1",
            "items": [
                {"vessel": "船A", "direction": "in", "actual_hour": 6},
                {"vessel": "船B", "direction": "in", "actual_hour": 6},
            ],
        })
        self.assertEqual(result["retransmit"], 1)
        self.assertEqual(result["skipped"], 1)
        states = {item["vessel"]: item["state"] for item in result["items"]}
        self.assertEqual(states["船A"], "landed")
        self.assertEqual(states["船B"], "landed")
        self.assertEqual(self._entry(self.ship_b["id"])["status"], "landed")

    def test_direction_mismatch_is_suspended(self):
        report = self.service.submit_pilot_report(PILOT, {
            "notice_id": "N-004", "pilot_id": "pilot-1",
            "items": [{"vessel": "船A", "direction": "out", "actual_hour": 6}],
        })
        self.assertEqual(report["items"][0]["state"], "suspended")
        self.assertEqual(report["items"][0]["reason"], "direction_mismatch")
        # 不一致不落地，不占额度
        self.assertEqual(self._entry(self.ship_a["id"])["status"], "waiting")

    def test_tolerance_configuration(self):
        # 容差3小时，实际9点与计划6点差3，在容差内 -> 落地
        report = self.service.submit_pilot_report(PILOT, {
            "notice_id": "N-005", "pilot_id": "pilot-1", "tolerance_hours": 3,
            "items": [{"vessel": "船A", "direction": "in", "actual_hour": 9}],
        })
        self.assertEqual(report["items"][0]["state"], "landed")
