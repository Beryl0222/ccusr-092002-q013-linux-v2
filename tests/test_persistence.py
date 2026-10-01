"""JSON 持久化：落盘后重开服务，数据与时钟锚定不变。"""

import tempfile
import unittest
from pathlib import Path

from drillcert import clock

from support import (
    Clock, FULL_MANUAL, make_ready_hospital, passing_submission,
    publish_open_rubric, register_grader,
)


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())

    def tearDown(self):
        self.clk.stop()

    def test_state_roundtrips_through_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "drill.json")
            svc = new_service_with(path)
            hid = make_ready_hospital(svc)
            register_grader(svc)
            publish_open_rubric(svc)
            drill = svc.dispatch(hid, {"seed": "p1", "overrides": {
                "time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
            did = drill["drill_id"]
            deadline_before = svc.get_drill(did)["deadline_at"]
            payload = passing_submission(svc, hid, drill)
            svc.submit(did, hid, payload)
            graded = svc.grade(did, {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
            cert_no = graded["certificate"]["certificate"]["cert_no"]

            # 重新打开：同一存储文件
            svc2 = new_service_with(path)
            self.assertEqual(svc2.get_drill(did)["deadline_at"], deadline_before)
            trace = svc2.trace(cert_no)
            self.assertEqual(trace["drill"]["drill_id"], did)
            self.assertTrue(svc2.capability(hid)["display_capability"])

            # 推进时间到证书到期后，重开的服务同样判定暂停
            self.clk.advance(days=800)
            self.assertFalse(svc2.capability(hid)["display_capability"])


def new_service_with(path):
    from drillcert.service import DrillCertService

    return DrillCertService(path)


if __name__ == "__main__":
    unittest.main()
