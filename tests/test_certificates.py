"""证书签发/暂停/复测、覆盖空白与追溯测试。"""

import unittest

from drillcert import clock
from drillcert.errors import NotFoundError

from support import (
    Clock, FULL_MANUAL, make_ready_hospital, new_service, passing_submission,
    publish_open_rubric, register_grader,
)


class CertCertificateTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)
        register_grader(self.svc)
        publish_open_rubric(self.svc)

    def tearDown(self):
        self.clk.stop()

    def _pass_drill(self, hid="H1", seed="d"):
        drill = self.svc.dispatch(hid, {"seed": seed, "overrides": {
            "time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
        payload = passing_submission(self.svc, hid, drill, submission_id=f"SUB-{seed}")
        self.svc.submit(drill["drill_id"], hid, payload)
        return self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})

    def test_certificate_issued_then_displayed(self):
        result = self._pass_drill(seed="d1")
        cert = result["certificate"]["certificate"]
        self.assertEqual(cert["cert_no"], "CERT-H1-001")
        cap = self.svc.capability(self.hid)
        self.assertTrue(cap["display_capability"])
        self.assertEqual(cap["status"], "active")

    def test_expiry_suspends_marker(self):
        self._pass_drill(seed="d1")
        self.clk.advance(days=731)
        cap = self.svc.capability(self.hid)
        self.assertFalse(cap["display_capability"])
        self.assertEqual(cap["status"], "suspended")
        self.assertIn("certificate_expired", cap["reasons"])

    def test_consecutive_absences_suspend(self):
        self._pass_drill(seed="d1")
        # 连续两场派发均不响应
        for seed in ("miss-1", "miss-2"):
            self.svc.dispatch(self.hid, {"seed": seed, "overrides": {
                "time": "weekend_daytime", "preservation": "cold_dry", "vessel": "digit"}})
            self.clk.advance(hours=2)
            self.svc.reconcile()
        cap = self.svc.capability(self.hid)
        self.assertFalse(cap["display_capability"])
        self.assertTrue(any("consecutive_absences" in r for r in cap["reasons"]))

    def test_critical_resource_failure_suspends(self):
        self._pass_drill(seed="d1")
        # 绿色通道关闭 → 关键资源失效
        self.svc.set_green_channel(self.hid, {"open": False})
        self.svc.reconcile()
        cap = self.svc.capability(self.hid)
        self.assertFalse(cap["display_capability"])
        self.assertTrue(any("绿色通道关闭" in r for r in cap["reasons"]))

        # 恢复通道后标识恢复（无需复测）
        self.svc.set_green_channel(self.hid, {"open": True})
        self.svc.reconcile()
        self.assertTrue(self.svc.capability(self.hid)["display_capability"])

    def test_retest_after_suspension_issues_new_certificate(self):
        self._pass_drill(seed="d1")
        self.svc.set_green_channel(self.hid, {"open": False})
        self.svc.reconcile()
        self.assertFalse(self.svc.capability(self.hid)["display_capability"])

        # 整改：恢复资源并完成复测
        self.svc.set_green_channel(self.hid, {"open": True})
        retest = self._pass_drill(seed="retest-1")
        self.assertTrue(retest["certificate"]["issued"])
        new_cert = retest["certificate"]["certificate"]
        self.assertEqual(new_cert["cert_no"], "CERT-H1-002")
        self.assertEqual(new_cert["renewed_from"], "CERT-H1-001")
        self.assertTrue(self.svc.capability(self.hid)["display_capability"])

    def test_failed_grading_does_not_issue(self):
        drill = self.svc.dispatch(self.hid, {"seed": "fail", "overrides": {
            "time": "workday_daytime", "preservation": "frozen", "vessel": "digit"}})
        payload = passing_submission(self.svc, self.hid, drill,
                                     submission_id="SUB-fail", decision="accept")
        self.svc.submit(drill["drill_id"], self.hid, payload)
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        self.assertFalse(result["passed"])
        self.assertFalse(result["certificate"]["issued"])
        self.assertEqual(self.svc.capability(self.hid)["status"], "never_certified")


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        register_grader(self.svc)
        publish_open_rubric(self.svc)

    def tearDown(self):
        self.clk.stop()

    def test_coverage_matrix_shows_gaps(self):
        make_ready_hospital(self.svc, "HA", "华东")
        drill = self.svc.dispatch("HA", {"seed": "c1", "overrides": {
            "time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
        payload = passing_submission(self.svc, "HA", drill)
        self.svc.submit(drill["drill_id"], "HA", payload)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})

        matrix = self.svc.coverage()
        east = next(r for r in matrix if r["region"] == "华东")
        self.assertEqual(east["total_cells"], 12)  # 4 时段 × 3 血管档
        self.assertEqual(east["gap_cells"], 11)
        covered = [c for c in east["cells"] if c["covered"]]
        self.assertEqual(len(covered), 1)
        self.assertEqual(covered[0]["hospitals"], ["HA"])

    def test_unsound_preservation_marked(self):
        make_ready_hospital(self.svc, "HB", "华北")
        drill = self.svc.dispatch("HB", {"seed": "c2", "overrides": {
            "time": "night_deep", "preservation": "frozen", "vessel": "major_limb"}})
        payload = passing_submission(self.svc, "HB", drill,
                                     decision="transfer", declare_concern=True)
        self.svc.submit(drill["drill_id"], "HB", payload)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        matrix = self.svc.coverage()
        north = next(r for r in matrix if r["region"] == "华北")
        cell = next(c for c in north["cells"]
                    if c["time_band"] == "night_deep" and c["vessel_band"] == "major_limb")
        self.assertTrue(cell["covered"])
        self.assertTrue(cell["unsound_preservation_seen"])


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)
        register_grader(self.svc, "G1")
        register_grader(self.svc, "G3")
        publish_open_rubric(self.svc)

    def tearDown(self):
        self.clk.stop()

    def test_trace_from_certificate_to_evidence(self):
        drill = self.svc.dispatch(self.hid, {"seed": "tr", "overrides": {
            "time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
        payload = passing_submission(self.svc, self.hid, drill)
        self.svc.submit(drill["drill_id"], self.hid, payload)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})

        trace = self.svc.trace("CERT-H1-001")
        self.assertEqual(trace["drill"]["drill_id"], drill["drill_id"])
        # 当时资源：快照冻结了派发时刻的人员设备
        self.assertIn("S1", trace["resource_snapshot_at_dispatch"]["staff_valid"])
        self.assertIn("E2", trace["resource_snapshot_at_dispatch"]["equipment_usable"])
        # 评分依据
        self.assertEqual(len(trace["gradings"]), 1)
        self.assertEqual(trace["gradings"][0]["rubric_version"], "RB-2026-1")
        # 事件链
        ids = [e["event_id"] for e in trace["event_chain"]]
        self.assertEqual(ids[0], "evt-dispatched")
        self.assertIn("evt-submit-SUB-1", ids)

    def test_trace_includes_appeal_and_retests(self):
        # frozen + transfer 但未声明保存风险：自动项 75 分。
        # 初评人工分 0 → 不通过、无证书；申诉后复核给满人工分 → 翻案开证。
        drill = self.svc.dispatch(self.hid, {"seed": "tr2", "overrides": {
            "time": "workday_daytime", "preservation": "frozen", "vessel": "digit"}})
        payload = passing_submission(self.svc, self.hid, drill, decision="transfer")
        self.svc.submit(drill["drill_id"], self.hid, payload)
        initial = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": {
            "G1": {"triage_rationale": 0, "backup_quality": 0}}})
        self.assertFalse(initial["passed"])
        apl = self.svc.appeal(drill["drill_id"], self.hid, {"reason": "申请复核"})
        reviewed = self.svc.review({"appeal_id": apl["appeal_id"], "grader_ids": ["G3"],
                                   "manual_scores": {"G3": {"triage_rationale": 8, "backup_quality": 7}}})
        self.assertTrue(reviewed["passed"])
        self.assertTrue(reviewed["certificate"]["issued"])

        trace = self.svc.trace("CERT-H1-001")
        self.assertEqual(len(trace["gradings"]), 2)
        self.assertEqual(trace["gradings"][0]["kind"], "initial")
        self.assertEqual(trace["gradings"][1]["kind"], "review")
        self.assertEqual(len(trace["appeals"]), 1)
        self.assertEqual(trace["retest_certificates"][0]["cert_no"], "CERT-H1-001")

    def test_trace_unknown_cert_404(self):
        with self.assertRaises(NotFoundError):
            self.svc.trace("CERT-NOPE-999")


if __name__ == "__main__":
    unittest.main()
