"""服务门面：业务模块与存储的唯一编排入口（HTTP 层只调用这里）。"""

import json
from pathlib import Path

from . import certificates as certs
from . import drills as drill_mod
from . import grading as grading_mod
from . import resources
from .clock import iso, now
from .errors import NotFoundError
from .firewall import assert_no_clinical_fields
from .scenario import CONTRACT_PATH
from .store import Store


class DrillCertService:
    def __init__(self, store_path=None):
        self.store = Store(store_path)

    # -- 医院与资源 -------------------------------------------------------

    def register_hospital(self, payload):
        return resources.register_hospital(
            self.store, payload["hospital_id"], payload["name"], payload["region"]
        )

    def upsert_staff(self, hospital_id, payload):
        return resources.upsert_staff(self.store, hospital_id, payload)

    def list_staff(self, hospital_id):
        self.store.require_hospital(hospital_id)
        return self.store.list_for("staff", hospital_id)

    def upsert_equipment(self, hospital_id, payload):
        return resources.upsert_equipment(self.store, hospital_id, payload)

    def list_equipment(self, hospital_id):
        self.store.require_hospital(hospital_id)
        return self.store.list_for("equipment", hospital_id)

    def set_green_channel(self, hospital_id, payload):
        return resources.set_green_channel(
            self.store, hospital_id, payload.get("open", False), payload.get("note")
        )

    def add_window(self, hospital_id, payload):
        return resources.add_commitment_window(
            self.store, hospital_id,
            payload["starts_at"], payload["ends_at"], payload.get("covers_roles"),
        )

    # -- 演练 -------------------------------------------------------------

    def dispatch(self, hospital_id, payload):
        assert_no_clinical_fields(payload or {})
        sample = None
        if payload.get("use_contract_sample"):
            sample = json.loads(Path(CONTRACT_PATH).read_text(encoding="utf-8"))["sample"]
        return drill_mod.dispatch_drill(
            self.store, hospital_id,
            seed=payload.get("seed", iso(now())),
            contract_sample=sample,
            overrides=payload.get("overrides"),
        )

    def get_drill(self, drill_id):
        from .firewall import assert_drill_id

        assert_drill_id(drill_id)
        drill = self.store.data["drills"].get(drill_id)
        if drill is None:
            raise NotFoundError(f"演练不存在：{drill_id}")
        return drill_mod.hospital_view(drill)

    def submit(self, drill_id, hospital_id, payload):
        return drill_mod.submit_response(self.store, drill_id, hospital_id, payload)

    def merge_receipts(self, drill_id, payload):
        return drill_mod.merge_offline_receipts(self.store, drill_id, payload.get("receipts", []))

    # -- 评分 -------------------------------------------------------------

    def register_grader(self, payload):
        return grading_mod.register_grader(
            self.store, payload["grader_id"], payload["name"], payload.get("affiliations")
        )

    def publish_rubric(self, payload):
        return grading_mod.publish_rubric(
            self.store, payload["version"], payload["valid_from"], payload.get("valid_to"),
            published_by=payload["published_by"], criteria=payload.get("criteria"),
        )

    def grade(self, drill_id, payload):
        return grading_mod.grade_drill(
            self.store, drill_id, payload["grader_ids"],
            payload.get("manual_scores"), payload.get("note"),
        )

    def appeal(self, drill_id, hospital_id, payload):
        return grading_mod.appeal(self.store, drill_id, hospital_id, payload["reason"])

    def review(self, payload):
        return grading_mod.review_appeal(
            self.store, payload["appeal_id"], payload["grader_ids"],
            payload.get("manual_scores"), payload.get("conclusion"),
        )

    # -- 证书、覆盖与追溯 --------------------------------------------------

    def reconcile(self):
        return {"changed_hospitals": certs.reconcile(self.store), "at": iso(now())}

    def capability(self, hospital_id):
        return certs.capability(self.store, hospital_id)

    def coverage(self):
        return certs.coverage_matrix(self.store)

    def trace(self, cert_no):
        return certs.trace_certificate(self.store, cert_no)
