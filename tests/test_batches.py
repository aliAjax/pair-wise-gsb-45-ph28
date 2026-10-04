import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, DraftConflict, RecomputeError


def plan_data(vessel='HaiYun', berth='B12', eta=6, etd=18, risk='medium'):
    return {'vessel': vessel, 'berth': berth, 'vessel_length_m': 180, 'berth_length_m': 220,
            'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': eta, 'etd_hour': etd,
            'risk_level': risk, 'dangerous_goods': False, 'dangerous_class': '', 'direction': 'in'}


CONTROLLER = Actor('dispatcher', 'port_controller')
PILOT = Actor('pilot-1', 'pilot')


class BatchRecomputeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.windows = self.service.list_windows(CONTROLLER)
        self.window_id = self.windows[0]['id']

    def tearDown(self):
        self.temp.cleanup()

    def test_batches_independent_and_reschedule_recomputes_same_window(self):
        first = self.service.create(CONTROLLER, 'VOY-1', plan_data('HaiYun', 'B12', eta=6))
        self.service.create(CONTROLLER, 'VOY-2', plan_data('HaiFeng', 'B13', eta=7))
        gate = self.service.gate_list(CONTROLLER, self.window_id)
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]['status'], 'active')
        self.assertEqual([m['vessel'] for m in gate[0]['members']], ['HaiYun', 'HaiFeng'])
        old_generation = gate[0]['generation']

        # HaiYun改期到8时：同潮位窗口批次整体重算，旧批次先失效。
        self.service.act(CONTROLLER, first['id'], first['version'], 'reschedule',
                         {'eta_hour': 8, 'etd_hour': 20})
        all_batches = self.service.list_batches(CONTROLLER, self.window_id, active_only=False)
        old = [b for b in all_batches if b['generation'] == old_generation]
        self.assertTrue(old)
        self.assertTrue(all(b['status'] == 'superseded' for b in old))
        gate = self.service.gate_list(CONTROLLER, self.window_id)
        # 闸口只看到新世代，且顺位按新eta重排（HaiFeng 7时先于HaiYun 8时）。
        self.assertEqual(len(gate), 1)
        self.assertEqual([m['eta_hour'] for m in gate[0]['members']], [7, 8])
        runs = self.service.latest_runs(CONTROLLER, self.window_id)
        self.assertEqual(runs[0]['status'], 'success')
        self.assertEqual(runs[0]['trigger'], 'reschedule')

    def test_failed_recompute_keeps_old_batches_with_reason(self):
        # 容量只有1艘的窗口：第二艘船排不下时，重算失败、旧批次保留。
        window = self.service.create_window(
            CONTROLLER, {'code': 'TIGHT', 'start_hour': 10, 'end_hour': 12,
                         'batch_quota': 1, 'max_batches': 1})
        wid = window['id']
        self.service.create(CONTROLLER, 'VOY-T1', plan_data('V1', 'B21', eta=11))
        self.service.create(CONTROLLER, 'VOY-T2', plan_data('V2', 'B22', eta=11))

        gate = self.service.gate_list(CONTROLLER, wid)
        self.assertEqual(len(gate), 1)
        self.assertEqual([m['vessel'] for m in gate[0]['members']], ['V1'])
        failed = [r for r in self.service.latest_runs(CONTROLLER, wid) if r['status'] == 'failed']
        self.assertTrue(failed and '容量不足' in failed[0]['reason'])

        # 值班员手动重试仍然失败，原批次不动。
        with self.assertRaises(RecomputeError):
            self.service.replan(CONTROLLER, wid)
        gate = self.service.gate_list(CONTROLLER, wid)
        self.assertEqual([m['vessel'] for m in gate[0]['members']], ['V1'])

    def test_concurrent_edits_first_wins_loser_keeps_draft(self):
        plan = self.service.create(CONTROLLER, 'VOY-C', plan_data('Concur', 'B31', eta=6))
        self.service.act(Actor('d1', 'port_controller'), plan['id'], plan['version'],
                         'reschedule', {'eta_hour': 8, 'etd_hour': 20})
        with self.assertRaises(DraftConflict) as ctx:
            self.service.act(Actor('d2', 'port_controller'), plan['id'], plan['version'],
                             'reschedule', {'eta_hour': 9, 'etd_hour': 21})
        draft = self.service.get_draft(CONTROLLER, ctx.exception.draft_id)
        self.assertEqual(draft['action'], 'reschedule')
        self.assertEqual(draft['payload'], {'eta_hour': 9, 'etd_hour': 21})
        self.assertEqual(draft['base_version'], plan['version'])

        # 后到者基于最新版本重新应用草稿，改期生效。
        updated = self.service.apply_draft(CONTROLLER, draft['id'])
        self.assertEqual(updated['payload']['eta_hour'], 9)
        gate = self.service.gate_list(CONTROLLER, self.window_id)
        self.assertEqual(gate[0]['members'][0]['eta_hour'], 9)


class PilotReconcileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / 'test.db'))
        self.window_id = self.service.list_windows(CONTROLLER)[0]['id']
        self.service.create(CONTROLLER, 'VOY-P1', plan_data('HaiYun', 'B12', eta=6, etd=18))
        self.service.create(CONTROLLER, 'VOY-P2', plan_data('HaiFeng', 'B13', eta=7, etd=19))

    def tearDown(self):
        self.temp.cleanup()

    def test_reconcile_suspends_mismatch_and_retransmit_only_fills_gap(self):
        report = self.service.submit_pilot_report(
            PILOT, 'TICKET-1',
            [{'vessel': 'HaiYun', 'movement': 'in', 'actual_hour': 6},
             {'vessel': 'HaiFeng', 'movement': 'in', 'actual_hour': 8},
             {'vessel': 'Ghost', 'movement': 'in', 'actual_hour': 9}])
        self.assertEqual(report['landed'], 1)
        self.assertEqual(report['suspended'], 2)
        self.assertEqual(report['quota_consumed'], 1)

        # 同一张回传单重传：已落地的HaiYun跳过，修正后的HaiFeng补落地，Ghost仍挂起。
        report = self.service.submit_pilot_report(
            PILOT, 'TICKET-1',
            [{'vessel': 'HaiYun', 'movement': 'in', 'actual_hour': 6},
             {'vessel': 'HaiFeng', 'movement': 'in', 'actual_hour': 7},
             {'vessel': 'Ghost', 'movement': 'in', 'actual_hour': 9}])
        self.assertEqual(report['landed'], 2)
        self.assertEqual(report['suspended'], 1)
        self.assertEqual(report['quota_consumed'], 2)

        # 原样再传一次：没有新增落地船，额度不重复占用。
        report = self.service.submit_pilot_report(
            PILOT, 'TICKET-1',
            [{'vessel': 'HaiYun', 'movement': 'in', 'actual_hour': 6},
             {'vessel': 'HaiFeng', 'movement': 'in', 'actual_hour': 7}])
        self.assertEqual(report['quota_consumed'], 2)
        with self.service.repository._connect() as connection:
            used = connection.execute('SELECT COUNT(*) AS n FROM quota_usage').fetchone()['n']
        self.assertEqual(used, 2)

        fetched = self.service.get_pilot_report(CONTROLLER, 'TICKET-1')
        self.assertEqual(fetched['ticket'], 'TICKET-1')


if __name__ == '__main__':
    unittest.main()
