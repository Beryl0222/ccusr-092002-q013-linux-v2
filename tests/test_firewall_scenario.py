"""临床隔离防火墙与盲演练场景生成测试。"""

import json
import unittest
from pathlib import Path

from drillcert import clock
from drillcert.errors import ValidationError
from drillcert.firewall import (
    DRILL_ID_RE, REAL_ALERT_RE, assert_drill_id, assert_no_clinical_fields,
)
from drillcert.scenario import (
    generate_drill, preservation_by_label, vessel_band_for,
)


class FirewallTest(unittest.TestCase):
    def test_drill_and_real_alert_segments_are_distinct(self):
        self.assertTrue(DRILL_ID_RE.match("DRILL-0061-AB12"))
        self.assertIsNotNone(REAL_ALERT_RE.search("TRAUMA-006H-17"))

    def test_real_alert_id_rejected_as_drill(self):
        with self.assertRaises(ValidationError) as ctx:
            assert_drill_id("TRAUMA-006H-17")
        self.assertEqual(ctx.exception.code, "bad_drill_id")

    def test_malformed_drill_id_rejected(self):
        for bad in ("DRILL-1-AB", "drill-0001-AB12", "EVAC-0001-AB12"):
            with self.assertRaises(ValidationError):
                assert_drill_id(bad)

    def test_payload_with_medical_record_key_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            assert_no_clinical_fields({"note": {"medical_record_no": "MR-1"}})
        self.assertEqual(ctx.exception.code, "clinical_isolation_violation")

    def test_payload_with_chinese_record_key_rejected(self):
        with self.assertRaises(ValidationError):
            assert_no_clinical_fields({"data": {"病历号": "x"}})

    def test_payload_quoting_real_alert_rejected(self):
        with self.assertRaises(ValidationError):
            assert_no_clinical_fields({"related": "请参考 TRAUMA-006H-17"})

    def test_clean_payload_passes(self):
        assert_no_clinical_fields({"submission_id": "SUB-1", "items": [{"ref": "E1"}]})


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.t = clock.now()
        clock.set_clock(lambda: self.t)

    def tearDown(self):
        clock.reset_clock()

    def test_deterministic_with_seed(self):
        a = generate_drill(region="华东", seed="sched-1")
        b = generate_drill(region="华东", seed="sched-1")
        self.assertEqual(a, b)

    def test_scenario_hides_answer_markers_is_pure(self):
        s = generate_drill(region="华东", seed="x")
        self.assertTrue(DRILL_ID_RE.match(s["drill_id"]))
        self.assertNotIn("score_points", s)
        # rubric_key 是不可反推的引用，不含答案词
        self.assertTrue(s["rubric_key"].startswith("RUB-"))

    def test_vessel_band_thresholds(self):
        self.assertEqual(vessel_band_for(0.5)["key"], "supermicro")
        self.assertEqual(vessel_band_for(1.2)["key"], "digit")
        self.assertEqual(vessel_band_for(2.5)["key"], "major_limb")

    def test_contract_sample_aligns_dimensions(self):
        sample = json.loads(
            Path("contracts/trauma_alert.json").read_text(encoding="utf-8")
        )["sample"]
        s = generate_drill(region="华东", seed="contract", contract_sample=sample)
        self.assertEqual(s["injuries"][0]["vessel_mm"], 0.5)
        self.assertEqual(s["vessel_band"], "supermicro")
        self.assertTrue(s["requires_supermicro"])
        self.assertEqual(s["preservation"], "cold_dry")
        self.assertEqual(s["time_band"], "workday_daytime")  # 13:05 当地钟点

    def test_unknown_preservation_treated_as_unsound(self):
        self.assertFalse(preservation_by_label("未知液体浸泡")["sound"])

    def test_overrides_pick_dimensions(self):
        s = generate_drill(
            region="华北", seed="o",
            overrides={"time": "night_deep", "preservation": "frozen", "vessel": "major_limb"},
        )
        self.assertEqual(s["time_band"], "night_deep")
        self.assertEqual(s["preservation"], "frozen")
        self.assertEqual(s["vessel_band"], "major_limb")
        self.assertTrue(s["requires"]["night_oncall"])
        self.assertIn("vascular_shunt", s["requires"]["equipment"])


if __name__ == "__main__":
    unittest.main()
