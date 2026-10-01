"""评分规则生效期、评委回避、评分依据与申诉复核版本测试。"""

import unittest
from datetime import timedelta

from drillcert import clock
from drillcert.errors import ConflictError, ForbiddenError

from support import (
    Clock, FULL_MANUAL, make_ready_hospital, new_service, passing_submission,
    publish_open_rubric, register_grader,
)


def _dispatch_and_submit(svc, hid, *, preservation="cold_dry", vessel="digit",
                         time_band="workday_daytime", decision=None,
                         declare_concern=None, channel_open=True):
    drill = svc.dispatch(hid, {"seed": "g1", "overrides": {
        "time": time_band, "preservation": preservation, "vessel": vessel}})
    if decision is None:
        decision = "accept"
    payload = passing_submission(svc, hid, drill, decision=decision,
                                 declare_concern=bool(declare_concern))
    return drill, svc.submit(drill["drill_id"], hid, payload)


class GradingTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)
        register_grader(self.svc, "G1")
        publish_open_rubric(self.svc)

    def tearDown(self):
        self.clk.stop()

    def test_passing_response_grades_pass_and_issues_certificate(self):
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        self.assertTrue(result["passed"])
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["kind"], "initial")
        self.assertTrue(result["certificate"]["issued"])
        # 评分依据可追溯到锚定时钟
        timely = next(i for i in result["items"] if i["key"] == "timely")
        self.assertTrue(timely["basis"]["on_time"])

    def test_unsound_preservation_accept_is_wrong_triage(self):
        drill, _ = _dispatch_and_submit(
            self.svc, self.hid, preservation="frozen", decision="accept")
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        triage = next(i for i in result["items"] if i["key"] == "triage")
        self.assertEqual(triage["score"], 0)
        self.assertFalse(result["passed"])
        self.assertFalse(result["certificate"]["issued"])

    def test_unsound_preservation_transfer_passes_when_concern_declared(self):
        drill, _ = _dispatch_and_submit(
            self.svc, self.hid, preservation="frozen",
            decision="transfer", declare_concern=True)
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        self.assertTrue(result["passed"])

    def test_supermicro_requires_super_surgeon(self):
        # 撤销超级显微资质后，accept 误判且资源锁定不完整
        self.svc.upsert_staff(self.hid, {
            "staff_id": "S2", "name": "S2", "role": "supermicrosurgeon",
            "valid_from": clock.now() - timedelta(days=365),
            "valid_until": clock.now() - timedelta(days=1),  # 已过期
        })
        drill, _ = _dispatch_and_submit(self.svc, self.hid, vessel="supermicro")
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        triage = next(i for i in result["items"] if i["key"] == "triage")
        self.assertEqual(triage["score"], 0)

    def test_evidence_must_match_dispatch_snapshot(self):
        # 派发后设备点检过期，证据仍提交该设备 → 与快照不符扣分
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        self.svc.upsert_equipment(self.hid, {
            "equipment_id": "E2", "kind": "microsurgical_set", "status": "failed",
            "checked_at": clock.now(), "next_check_due": clock.now() + timedelta(days=30),
        })
        result = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        res = next(i for i in result["items"] if i["key"] == "resource_lock")
        self.assertTrue(res["basis"]["evidence_matches_snapshot"])  # 评分只认派发快照
        self.assertEqual(res["score"], 30)

    def test_conflicted_grader_rejected(self):
        self.svc.register_grader({"grader_id": "G2", "name": "本院顾问", "affiliations": [self.hid]})
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        with self.assertRaises(ForbiddenError) as ctx:
            self.svc.grade(drill["drill_id"], {"grader_ids": ["G2"], "manual_scores": {
                "G2": {"triage_rationale": 8, "backup_quality": 7}}})
        self.assertEqual(ctx.exception.code, "conflict_of_interest")

    def test_rubric_periods_cannot_overlap(self):
        with self.assertRaises(ConflictError) as ctx:
            self.svc.publish_rubric({
                "version": "RB-2026-2", "valid_from": "2020-01-01T00:00:00Z",
                "valid_to": "2030-01-01T00:00:00Z", "published_by": "panel"})
        self.assertEqual(ctx.exception.code, "rubric_overlap")

    def test_no_effective_rubric_at_dispatch_time(self):
        # 规则只在未来生效 → 派发时刻无生效版本
        svc2, clk2, hid2 = _fresh_service(open_rubric=False)
        try:
            svc2.publish_rubric({
                "version": "RB-FUTURE", "valid_from": "2099-01-01T00:00:00Z",
                "valid_to": None, "published_by": "panel"})
            drill, _ = _dispatch_and_submit(svc2, hid2)
            with self.assertRaises(ConflictError) as ctx:
                svc2.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
            self.assertEqual(ctx.exception.code, "no_effective_rubric")
        finally:
            clk2.stop()

    def test_immutable_version_not_regradable(self):
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        with self.assertRaises(ConflictError):
            self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})


class AppealTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = new_service()
        self.hid = make_ready_hospital(self.svc)
        register_grader(self.svc, "G1")
        register_grader(self.svc, "G3")
        publish_open_rubric(self.svc)

    def tearDown(self):
        self.clk.stop()

    def test_appeal_creates_new_version_keeps_initial(self):
        # 初评失败（frozen + accept），医院申诉
        drill, _ = _dispatch_and_submit(self.svc, self.hid, preservation="frozen", decision="accept")
        initial = self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        self.assertFalse(initial["passed"])
        apl = self.svc.appeal(drill["drill_id"], self.hid, {"reason": "证据漏传，申请复核"})
        self.assertEqual(apl["status"], "open")

        reviewed = self.svc.review({
            "appeal_id": apl["appeal_id"], "grader_ids": ["G3"],
            "manual_scores": {"G3": {"triage_rationale": 8, "backup_quality": 7}},
            "conclusion": "维持原判断"})
        self.assertEqual(reviewed["kind"], "review")
        self.assertEqual(reviewed["grading_version"], 2)
        self.assertTrue(reviewed["immutable"])

        versions = self.svc.store.data["gradings"][drill["drill_id"]]
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["kind"], "initial")  # 初评原样保留
        self.assertEqual(versions[1]["kind"], "review")

    def test_conflicted_reviewer_rejected(self):
        self.svc.register_grader({"grader_id": "G9", "name": "x", "affiliations": [self.hid]})
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        apl = self.svc.appeal(drill["drill_id"], self.hid, {"reason": "r"})
        with self.assertRaises(ForbiddenError):
            self.svc.review({"appeal_id": apl["appeal_id"], "grader_ids": ["G9"],
                             "manual_scores": {"G9": {"triage_rationale": 1, "backup_quality": 1}}})

    def test_closed_appeal_cannot_review_twice(self):
        drill, _ = _dispatch_and_submit(self.svc, self.hid)
        self.svc.grade(drill["drill_id"], {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        apl = self.svc.appeal(drill["drill_id"], self.hid, {"reason": "r"})
        self.svc.review({"appeal_id": apl["appeal_id"], "grader_ids": ["G3"],
                         "manual_scores": {"G3": {"triage_rationale": 8, "backup_quality": 7}}})
        with self.assertRaises(ConflictError):
            self.svc.review({"appeal_id": apl["appeal_id"], "grader_ids": ["G3"],
                             "manual_scores": {"G3": {"triage_rationale": 8, "backup_quality": 7}}})


def _fresh_service(open_rubric=True):
    clk = Clock(clock.now())
    svc = new_service()
    hid = make_ready_hospital(svc, "HX", "华南")
    register_grader(svc, "G1")
    if open_rubric:
        publish_open_rubric(svc)
    return svc, clk, hid


if __name__ == "__main__":
    unittest.main()
