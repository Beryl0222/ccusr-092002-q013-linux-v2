"""HTTP API 端到端测试（stdlib http.client + 随机端口）。"""

import json
import threading
import unittest
from datetime import timedelta
from http.client import HTTPConnection

from drillcert import clock
from drillcert.api import make_server
from drillcert.firewall import SIMULATION_HEADER
from drillcert.service import DrillCertService

from support import (
    Clock, FULL_MANUAL, make_ready_hospital, passing_submission,
    publish_open_rubric, register_grader,
)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.clk = Clock(clock.now())
        self.svc = DrillCertService(None)
        self.server = make_server(self.svc, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.clk.stop()

    def _request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        payload = json.loads(raw) if raw else {}
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, payload

    def test_health_and_simulation_headers(self):
        status, hdrs, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "limb-salvage-network")
        self.assertTrue(body["simulation_only"])
        self.assertEqual(hdrs[SIMULATION_HEADER.lower()], "1")

    def test_full_flow_over_http(self):
        # 医院与资源
        status, _, body = self._request("POST", "/hospitals",
                                        {"hospital_id": "H1", "name": "一院", "region": "华东"})
        self.assertEqual(status, 201)
        at = clock.now()
        staff = [
            ("S1", "microsurgeon"), ("S2", "supermicrosurgeon"),
            ("S3", "anesthesiologist"), ("S4", "scrub_nurse"),
        ]
        for sid, role in staff:
            status, _, _ = self._request("PUT", "/hospitals/H1/staff", {
                "staff_id": sid, "name": sid, "role": role,
                "valid_from": clock.iso(at - timedelta(days=1)),
                "valid_until": clock.iso(at + timedelta(days=365))})
            self.assertEqual(status, 200)
        for eid, kind in [("E1", "microscope"), ("E2", "microsurgical_set"),
                          ("E3", "supermicro_set"), ("E4", "vascular_shunt")]:
            self._request("PUT", "/hospitals/H1/equipment", {
                "equipment_id": eid, "kind": kind, "status": "ok",
                "checked_at": clock.iso(at - timedelta(days=1)),
                "next_check_due": clock.iso(at + timedelta(days=30))})
        self._request("PUT", "/hospitals/H1/green-channel", {"open": True})
        self._request("POST", "/hospitals/H1/availability", {
            "starts_at": clock.iso(at - timedelta(days=1)),
            "ends_at": clock.iso(at + timedelta(days=5))})

        # 评委与规则
        self._request("POST", "/graders", {"grader_id": "G1", "name": "专家甲"})
        status, _, _ = self._request("POST", "/rubrics", {
            "version": "RB-1", "valid_from": "2000-01-01T00:00:00Z",
            "valid_to": None, "published_by": "panel"})
        self.assertEqual(status, 201)

        # 派发
        status, hdrs, body = self._request("POST", "/hospitals/H1/drills", {
            "seed": "http-1",
            "overrides": {"time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
        self.assertEqual(status, 201)
        self.assertEqual(hdrs[SIMULATION_HEADER.lower()], "1")
        drill = body["data"]
        self.assertNotIn("rubric_key", drill)  # 评分要点对医院不可见

        # 提交
        payload = passing_submission(self.svc, "H1", drill)
        status, _, body = self._request(
            "POST", f"/drills/{drill['drill_id']}/response", payload,
            headers={"X-Hospital-Id": "H1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["on_time"])

        # 错误医院身份被拒
        status, _, body = self._request(
            "POST", f"/drills/{drill['drill_id']}/response",
            {**payload, "submission_id": "SUB-X"}, headers={"X-Hospital-Id": "H2"})
        self.assertEqual(status, 403)

        # 评分
        status, _, body = self._request("POST", f"/drills/{drill['drill_id']}/grade",
                                        {"grader_ids": ["G1"], "manual_scores": FULL_MANUAL})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["passed"])

        # 能力标识与覆盖
        status, _, body = self._request("GET", "/hospitals/H1/capability")
        self.assertTrue(body["data"]["display_capability"])
        status, _, body = self._request("GET", "/coverage")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 1)

        # 追溯
        status, _, body = self._request("GET", "/certificates/CERT-H1-001/trace")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["certificate"]["cert_no"], "CERT-H1-001")

    def test_clinical_payload_rejected_with_simulation_headers(self):
        make_ready_hospital(self.svc)
        register_grader(self.svc)
        publish_open_rubric(self.svc)
        status, _, body = self._request("POST", "/hospitals/H1/drills", {
            "seed": "x",
            "overrides": {"time": "workday_daytime", "preservation": "cold_dry", "vessel": "digit"}})
        did = body["data"]["drill_id"]
        status, _, body = self._request(
            "POST", f"/drills/{did}/response",
            {"submission_id": "S1", "patient_id": "P-9", "triage_decision": "accept",
             "locked_resources": {"evidence": [{"ref": "S1", "sha256": "a"}]},
             "backup_plan": {"x": 1}},
            headers={"X-Hospital-Id": "H1"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "clinical_isolation_violation")

    def test_contract_sample_dispatch(self):
        make_ready_hospital(self.svc)
        register_grader(self.svc)
        publish_open_rubric(self.svc)
        status, _, body = self._request("POST", "/hospitals/H1/drills",
                                        {"seed": "contract", "use_contract_sample": True})
        self.assertEqual(status, 201)
        self.assertEqual(body["data"]["vessel_band"], "supermicro")

    def test_bad_json_returns_400(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/hospitals", body=b"{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)

    def test_unknown_route_404(self):
        status, _, _ = self._request("GET", "/nothing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
