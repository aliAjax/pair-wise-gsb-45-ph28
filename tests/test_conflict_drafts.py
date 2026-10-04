import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CONTROLLER = Actor("ctrl-1", "port_controller")
DISPATCHER_A = Actor("dispatch-a", "port_controller")
DISPATCHER_B = Actor("dispatch-b", "port_controller")

WINDOW_IN = {"name": "早潮进口", "date": "2026-10-04", "direction": "inbound",
             "open_hour": 4, "close_hour": 10, "vessel_quota": 2, "tide_level_m": 12.0}

PLAN = {"vessel": "HaiYun", "berth": "B12", "vessel_length_m": 180, "berth_length_m": 220,
        "draft_m": 10.2, "berth_depth_m": 11.5, "eta_hour": 6, "etd_hour": 18,
        "risk_level": "medium", "dangerous_goods": False, "dangerous_class": "",
        "direction": "inbound"}


class ConflictDraftTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.window = self.service.create_tide_window(CONTROLLER, WINDOW_IN)
        self.rec = self.service.create(CONTROLLER, "V-001", PLAN, tide_window_id=self.window["id"])

    def tearDown(self):
        self.temp.cleanup()

    def test_first_writer_wins_second_keeps_draft(self):
        # 调度员A先改期，版本从1升到2
        updated = self.service.act(DISPATCHER_A, self.rec["id"], 1, "reschedule", {"eta_hour": 7})
        self.assertEqual(updated["version"], 2)
        # 调度员B仍持版本1改期 -> 409，草稿落库
        with self.assertRaises(Conflict) as ctx:
            self.service.act(DISPATCHER_B, self.rec["id"], 1, "reschedule", {"eta_hour": 8})
        self.assertEqual(ctx.exception.code, "conflict")
        draft_id = ctx.exception.extra["draft_id"]
        self.assertIsNotNone(draft_id)
        self.assertEqual(ctx.exception.extra["current_version"], 2)

        drafts = self.service.list_drafts(CONTROLLER)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["actor_id"], "dispatch-b")
        self.assertEqual(drafts[0]["payload"], {"eta_hour": 8})

        # B在最新版本上应用草稿（重放为eta=8）
        result = self.service.apply_draft(DISPATCHER_B, draft_id)
        self.assertEqual(result["status"], "applied")
        final = self.service.get_record(CONTROLLER, self.rec["id"])
        self.assertEqual(final["payload"]["eta_hour"], 8)
        self.assertEqual(final["version"], 3)
        # 已处理草稿不再出现在open列表
        self.assertEqual(self.service.list_drafts(CONTROLLER), [])

    def test_draft_can_be_discarded(self):
        self.service.act(DISPATCHER_A, self.rec["id"], 1, "reschedule", {"eta_hour": 7})
        with self.assertRaises(Conflict) as ctx:
            self.service.act(DISPATCHER_B, self.rec["id"], 1, "reschedule", {"eta_hour": 9})
        draft_id = ctx.exception.extra["draft_id"]
        result = self.service.discard_draft(DISPATCHER_B, draft_id)
        self.assertEqual(result["status"], "discarded")
        self.assertEqual(self.service.list_drafts(CONTROLLER), [])
        # 已废弃草稿不能再应用
        from src.domain import Conflict as DomainConflict
        with self.assertRaises(DomainConflict):
            self.service.apply_draft(DISPATCHER_B, draft_id)

    def test_window_concurrent_edit_keeps_draft(self):
        # 窗口并发：A先把配额改成1
        win = self.service.update_tide_window(DISPATCHER_A, self.window["id"], 1, {"vessel_quota": 1})
        self.assertEqual(win["version"], 2)
        # B持版本1改潮位 -> 冲突草稿
        with self.assertRaises(Conflict) as ctx:
            self.service.update_tide_window(DISPATCHER_B, self.window["id"], 1, {"tide_level_m": 11.0},
                                            save_draft_on_conflict=True)
        draft_id = ctx.exception.extra["draft_id"]
        self.assertIsNotNone(draft_id)
        # 应用草稿：在最新版本2上重放潮位调整
        result = self.service.apply_draft(DISPATCHER_B, draft_id)
        self.assertEqual(result["status"], "applied")
        win2 = self.service.repository.get_tide_window(self.window["id"])
        self.assertEqual(win2["vessel_quota"], 1)
        self.assertEqual(win2["tide_level_m"], 11.0)
        self.assertEqual(win2["version"], 3)

    def test_stale_reschedule_without_draft_flag(self):
        self.service.act(DISPATCHER_A, self.rec["id"], 1, "reschedule", {"eta_hour": 7})
        # 直接调service方法且不保存草稿 -> 409但无草稿
        with self.assertRaises(Conflict) as ctx:
            self.service.reschedule(DISPATCHER_B, self.rec["id"], 1, {"eta_hour": 8},
                                    save_draft_on_conflict=False)
        self.assertIsNone(ctx.exception.extra["draft_id"])
        self.assertEqual(self.service.list_drafts(CONTROLLER), [])
