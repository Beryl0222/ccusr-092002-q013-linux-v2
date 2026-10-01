"""测试辅助：固定时钟与「资源齐备」医院构造。"""

from datetime import timedelta

from drillcert import clock
from drillcert.service import DrillCertService


class Clock:
    def __init__(self, t):
        self.t = t
        clock.set_clock(lambda: self.t)

    def advance(self, **kwargs):
        self.t += timedelta(**kwargs)
        return self.t

    def stop(self):
        clock.reset_clock()


def new_service():
    return DrillCertService(None)


def make_ready_hospital(svc, hid="H1", region="华东", at=None, channel_open=True):
    """登记一家人员/设备/通道/时段齐备的医院。"""
    at = at or clock.now()
    svc.register_hospital({"hospital_id": hid, "name": f"{hid}医院", "region": region})
    staff_specs = [
        ("S1", "microsurgeon"),
        ("S2", "supermicrosurgeon"),
        ("S3", "anesthesiologist"),
        ("S4", "scrub_nurse"),
    ]
    for sid, role in staff_specs:
        svc.upsert_staff(hid, {
            "staff_id": sid, "name": sid, "role": role,
            "valid_from": at - timedelta(days=30),
            "valid_until": at + timedelta(days=365),
        })
    equip_specs = [
        ("E1", "microscope"),
        ("E2", "microsurgical_set"),
        ("E3", "supermicro_set"),
        ("E4", "vascular_shunt"),
    ]
    for eid, kind in equip_specs:
        svc.upsert_equipment(hid, {
            "equipment_id": eid, "kind": kind, "status": "ok",
            "checked_at": at - timedelta(days=1),
            "next_check_due": at + timedelta(days=90),
        })
    svc.set_green_channel(hid, {"open": channel_open})
    svc.add_window(hid, {
        "starts_at": at - timedelta(days=1),
        "ends_at": at + timedelta(days=10),
    })
    return hid


def passing_submission(svc, hid, drill, *, submission_id="SUB-1",
                       decision="accept", confirm_channel=True,
                       declare_concern=False, include_roles=None,
                       include_equipment=None, drop_evidence=False):
    """构造一份能通过评分的提交。"""
    staff = {s["role"]: s["staff_id"] for s in svc.list_staff(hid)}
    equip = {e["kind"]: e["equipment_id"] for e in svc.list_equipment(hid)}
    roles = include_roles
    if roles is None:
        roles = ["microsurgeon", "anesthesiologist", "scrub_nurse"]
        if drill["requires_supermicro"]:
            roles.append("supermicrosurgeon")
    kinds = include_equipment
    if kinds is None:
        kinds = ["microscope", "microsurgical_set"]
        if drill["requires_supermicro"]:
            kinds.append("supermicro_set")
    evidence = []
    if not drop_evidence:
        for role in roles:
            evidence.append({"type": "staff", "role": role, "ref": staff[role],
                             "sha256": f"hash-{staff[role]}"})
        for kind in kinds:
            evidence.append({"type": "equipment", "kind": kind, "ref": equip[kind],
                             "sha256": f"hash-{equip[kind]}"})
    return {
        "submission_id": submission_id,
        "triage_decision": decision,
        "locked_resources": {
            "evidence": evidence,
            "green_channel_confirmed": confirm_channel,
            "preservation_concern": declare_concern,
        },
        "backup_plan": {"transfer_to": "H2", "transport": "救护车保温转运"},
    }


def publish_open_rubric(svc, version="RB-2026-1"):
    return svc.publish_rubric({
        "version": version,
        "valid_from": "2000-01-01T00:00:00Z",
        "valid_to": None,
        "published_by": "panel",
    })


def register_grader(svc, gid="G1", affiliations=None):
    return svc.register_grader({"grader_id": gid, "name": gid, "affiliations": affiliations})


FULL_MANUAL = {"G1": {"triage_rationale": 8, "backup_quality": 7}}
