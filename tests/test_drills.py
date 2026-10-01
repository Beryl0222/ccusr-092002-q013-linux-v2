"""演练派发、响应时钟、幂等提交与离线回执因果合并测试。"""

import unittest
from datetime import timedelta

from drillcert import clock
from drillcert.errors import ValidationError

from support import Clock, make_ready_hospital, new_service


class DispatchAndDeadlineTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)

    def tearDown(self):
        self.clk.stop()

    def _dispatch(self, **overrides):
        return self.svc.dispatch(self.hid, {"seed": "s1", "overrides": overrides})

    def test_deadline_anchored_at_dispatch_and_hidden_rubric(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        stored = self.svc.store.data["drills"][drill["drill_id"]]
        deadline = stored["deadline_at"]
        self.assertEqual(deadline, clock.iso(clock.now() + timedelta(minutes=30)))
        # 医院视图不含评分要点/资源快照
        self.assertNotIn("rubric_key", drill)
        self.assertNotIn("requires", drill)
        self.assertNotIn("snapshot", drill)
        self.assertNotIn("preservation_sound", drill)

    def test_night_window_has_longer_deadline(self):
        drill = self._dispatch(time="night_deep", preservation="cold_dry", vessel="digit")
        stored = self.svc.store.data["drills"][drill["drill_id"]]
        self.assertEqual(stored["deadline_at"],
                         clock.iso(clock.now() + timedelta(minutes=45)))

    def test_submission_on_time_then_late(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        payload = self._payload(drill["drill_id"])
        ack = self.svc.submit(drill["drill_id"], self.hid, payload)
        self.assertTrue(ack["on_time"])
        self.assertFalse(ack["duplicate"])

        self.clk.advance(minutes=31)
        payload2 = {**payload, "submission_id": "SUB-2"}
        ack2 = self.svc.submit(drill["drill_id"], self.hid, payload2)
        # 第二份超时，但不改变首份锚定的响应时钟（ack2 以首份时间为准）
        self.assertTrue(ack2["on_time"])
        self.assertEqual(ack2["notice"], "重复提交不会刷新响应时钟")

    def test_first_submission_late_marks_late(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        self.clk.advance(minutes=40)
        ack = self.svc.submit(drill["drill_id"], self.hid, self._payload(drill["drill_id"]))
        self.assertFalse(ack["on_time"])
        stored = self.svc.store.data["drills"][drill["drill_id"]]
        self.assertEqual(stored["status"], "late")

    def test_duplicate_submission_is_idempotent(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        payload = self._payload(drill["drill_id"])
        first = self.svc.submit(drill["drill_id"], self.hid, payload)
        self.clk.advance(minutes=20)
        again = self.svc.submit(drill["drill_id"], self.hid, payload)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["effective_response_at"], first["effective_response_at"])
        stored = self.svc.store.data["drills"][drill["drill_id"]]
        self.assertEqual(len(stored["submissions"]), 1)

    def test_missing_evidence_rejected(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        bad = self._payload(drill["drill_id"])
        bad["locked_resources"]["evidence"] = []
        with self.assertRaises(ValidationError):
            self.svc.submit(drill["drill_id"], self.hid, bad)

    def test_missing_backup_plan_rejected(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        bad = self._payload(drill["drill_id"])
        bad["backup_plan"] = None
        with self.assertRaises(ValidationError):
            self.svc.submit(drill["drill_id"], self.hid, bad)

    def test_clinical_fields_rejected_on_submission(self):
        drill = self._dispatch(time="workday_daytime", preservation="cold_dry", vessel="digit")
        bad = self._payload(drill["drill_id"])
        bad["locked_resources"]["evidence"].append(
            {"type": "staff", "ref": "S1", "sha256": "x", "ehr_id": "EHR-9"})
        with self.assertRaises(ValidationError) as ctx:
            self.svc.submit(drill["drill_id"], self.hid, bad)
        self.assertEqual(ctx.exception.code, "clinical_isolation_violation")

    def _payload(self, drill_id):
        from support import passing_submission

        drill = self.svc.get_drill(drill_id)
        return passing_submission(self.svc, self.hid, drill)


class OfflineReceiptMergeTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)
        self.drill = self.svc.dispatch(self.hid, {
            "seed": "off",
            "overrides": {"time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"},
        })
        self.did = self.drill["drill_id"]

    def tearDown(self):
        self.clk.stop()

    def test_gap_held_until_predecessor_arrives(self):
        # e3 依赖 e2，e2 尚未到达 → e3 挂起
        receipts = [
            {"event_id": "evt-lock-1", "at": clock.iso(clock.now() + timedelta(minutes=1)),
             "type": "resource_locked", "after": ["evt-dispatched"]},
            {"event_id": "evt-esc-3", "at": clock.iso(clock.now() + timedelta(minutes=3)),
             "type": "escalation_sent", "after": ["evt-lock-2"]},
        ]
        result = self.svc.merge_receipts(self.did, {"receipts": receipts})
        self.assertIn("evt-esc-3", result["pending"])
        chain_ids = [e["event_id"] for e in self._chain()]
        self.assertNotIn("evt-esc-3", chain_ids)

        # 前驱到达 → 级联释放，且因果顺序正确（lock-2 在 esc-3 之前）
        fill = [
            {"event_id": "evt-lock-2", "at": clock.iso(clock.now() + timedelta(minutes=2)),
             "type": "channel_cleared", "after": ["evt-lock-1"]},
        ]
        self.svc.merge_receipts(self.did, {"receipts": fill})
        chain_ids = [e["event_id"] for e in self._chain()]
        self.assertIn("evt-esc-3", chain_ids)
        self.assertLess(chain_ids.index("evt-lock-2"), chain_ids.index("evt-esc-3"))

    def test_duplicate_receipts_deduped(self):
        receipts = [
            {"event_id": "evt-a", "at": clock.iso(clock.now()), "type": "note",
             "after": ["evt-dispatched"]},
            {"event_id": "evt-a", "at": clock.iso(clock.now()), "type": "note",
             "after": ["evt-dispatched"]},
        ]
        self.svc.merge_receipts(self.did, {"receipts": receipts})
        chain_ids = [e["event_id"] for e in self._chain() if e["event_id"] == "evt-a"]
        self.assertEqual(len(chain_ids), 1)

    def test_receipts_attached_to_submission_also_merge(self):
        from support import passing_submission

        payload = passing_submission(self.svc, self.hid, self.drill)
        payload["offline_receipts"] = [
            {"event_id": "evt-offline-1", "at": clock.iso(clock.now() - timedelta(minutes=2)),
             "type": "offline_note", "after": ["evt-dispatched"]},
        ]
        self.svc.submit(self.did, self.hid, payload)
        chain_ids = [e["event_id"] for e in self._chain()]
        self.assertIn("evt-offline-1", chain_ids)
        self.assertLess(chain_ids.index("evt-offline-1"),
                        chain_ids.index("evt-submit-SUB-1"))

    def _chain(self):
        return self.svc.store.data["drills"][self.did]["chain"]


if __name__ == "__main__":
    unittest.main()
