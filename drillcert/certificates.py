"""能力证书：签发、暂停、复测与追溯。

对外能力标识（capability marker）的状态机：

* 通过一场已评分演练 → 签发/换发带有效期的证书；
* 出现以下任一情况，``reconcile`` 会暂停标识：
  - 证书到期；
  - 连续缺席达到阈值（派发后时限内无任何响应）；
  - 关键资源当前失效（无有效显微外科医师、显微镜/显微器械不可用、
    绿色通道关闭）；
* 暂停后须完成**整改复测**（一场新的通过演练）才能换发新证；
* 每次换发保留 ledger，证书可一路追溯到原始演练、当时资源快照、
  评分依据版本与历次复测。
"""

from datetime import timedelta

from .clock import iso, now, parse
from .errors import ConflictError, NotFoundError
from .resources import staff_valid_at, equipment_usable_at
from .scenario import TIME_BANDS, VESSEL_BANDS

CERT_VALIDITY_DAYS = 365 * 2
ABSENCE_LIMIT = 2
CRITICAL_ROLES = ("microsurgeon",)
CRITICAL_EQUIPMENT = ("microscope", "microsurgical_set")


# --------------------------------------------------------------------------
# 出席流水（由演练模块回调）
# --------------------------------------------------------------------------

def mark_scheduled(store, hospital_id, drill_id, at):
    store.list_for("attendance", hospital_id).append(
        {"drill_id": drill_id, "scheduled_at": iso(at), "state": "scheduled", "on_time": None}
    )


def mark_attended(store, hospital_id, drill_id, on_time, at):
    for row in reversed(store.list_for("attendance", hospital_id)):
        if row["drill_id"] == drill_id and row["state"] == "scheduled":
            row["state"] = "responded"
            row["on_time"] = bool(on_time)
            row["responded_at"] = iso(at)
            return


def reconcile(store, at=None):
    """结算逾期缺席并刷新全部证书状态。返回发生变化的医院列表。"""
    at = at or now()
    changed = []
    with store.lock:
        # 1) 超时未响应 → 缺席
        for drill_id, drill in store.data["drills"].items():
            if drill["status"] == "awaiting_response" and at > parse(drill["deadline_at"]):
                drill["status"] = "no_response"
                for row in reversed(store.list_for("attendance", drill["hospital_id"])):
                    if row["drill_id"] == drill_id and row["state"] == "scheduled":
                        row["state"] = "absent"
                        row["on_time"] = False
                        row["closed_at"] = iso(at)
                changed.append(drill["hospital_id"])

        # 2) 刷新证书状态
        for hospital_id, cert_state in store.data["certificates"].items():
            if _refresh_certificate(store, hospital_id, cert_state, at):
                changed.append(hospital_id)
        store.save()
    return sorted(set(changed))


def consecutive_absences(store, hospital_id):
    streak = 0
    for row in reversed(store.list_for("attendance", hospital_id)):
        if row["state"] == "scheduled":
            continue  # 尚未到结算时刻，不计
        if row["state"] == "absent":
            streak += 1
        else:
            break
    return streak


def critical_resource_failures(store, hospital_id, at):
    """检查关键资源此刻是否有效，返回失效原因列表（空=全部有效）。"""
    failures = []
    staff = store.list_for("staff", hospital_id)
    roles = {s["role"] for s in staff if staff_valid_at(s, at)}
    for role in CRITICAL_ROLES:
        if role not in roles:
            failures.append(f"缺少有效资质人员：{role}")
    equipment = store.list_for("equipment", hospital_id)
    usable_kinds = {e["kind"] for e in equipment if equipment_usable_at(e, at)}
    for kind in CRITICAL_EQUIPMENT:
        if kind not in usable_kinds:
            failures.append(f"关键设备不可用：{kind}")
    channel = store.data["channels"].get(hospital_id)
    if not (channel and channel["open"]):
        failures.append("绿色通道关闭")
    return failures


def _refresh_certificate(store, hospital_id, cert_state, at):
    current = cert_state.get("current")
    if current is None:
        return False
    reasons = []
    if at > parse(current["valid_until"]):
        reasons.append("certificate_expired")
    absences = consecutive_absences(store, hospital_id)
    if absences >= ABSENCE_LIMIT:
        reasons.append(f"consecutive_absences:{absences}")
    reasons.extend(f"resource:{f}" for f in critical_resource_failures(store, hospital_id, at))

    new_status = "suspended" if reasons else "active"
    if new_status != current["status"] or reasons != current.get("suspend_reasons", []):
        current["status"] = new_status
        current["suspend_reasons"] = reasons
        current["status_checked_at"] = iso(at)
        return True
    return False


# --------------------------------------------------------------------------
# 评分结果 → 证书
# --------------------------------------------------------------------------

def record_grading_outcome(store, drill_id):
    """评分/复核落定后调用：通过则换发新证（整改复测），失败不开证。"""
    from .grading import latest_grading

    drill = store.data["drills"][drill_id]
    at = parse(latest_grading(store, drill_id)["graded_at"])
    hospital_id = drill["hospital_id"]
    grading = latest_grading(store, drill_id)
    cert_state = store.data["certificates"].setdefault(hospital_id, {"current": None, "ledger": []})

    if not grading["passed"]:
        return {"issued": False, "reason": "grading_failed", "certificate": cert_state.get("current")}

    # 同一演练只在首次通过时开证；申诉翻案（初评失败→复核通过）时此前无
    # 该演练的证书，因此会正常换发。
    if any(c["basis_drill_id"] == drill_id for c in cert_state["ledger"]):
        return {"issued": False, "reason": "already_issued_for_drill",
                "certificate": cert_state.get("current")}

    prior = cert_state.get("current")
    serial = len(cert_state["ledger"]) + 1
    cert = {
        "cert_no": f"CERT-{hospital_id}-{serial:03d}",
        "hospital_id": hospital_id,
        "issued_at": iso(at),
        "valid_until": iso(at + timedelta(days=CERT_VALIDITY_DAYS)),
        "basis_drill_id": drill_id,
        "status": "active",
        "suspend_reasons": [],
        "renewed_from": prior["cert_no"] if prior else None,
        "rubric_version": grading["rubric_version"],
        "grading_id": grading["grading_id"],
    }
    cert_state["current"] = cert
    cert_state["ledger"].append(cert)
    return {"issued": True, "certificate": cert}


def capability(store, hospital_id, at=None):
    """对外能力标识：只有持有未暂停证书才显示。"""
    store.require_hospital(hospital_id)
    at = at or now()
    reconcile(store, at)
    cert_state = store.data["certificates"][hospital_id]
    current = cert_state.get("current")
    if current is None:
        return {"hospital_id": hospital_id, "display_capability": False,
                "status": "never_certified", "reasons": ["no_certificate"]}
    return {
        "hospital_id": hospital_id,
        "display_capability": current["status"] == "active",
        "status": current["status"],
        "cert_no": current["cert_no"],
        "valid_until": current["valid_until"],
        "reasons": current.get("suspend_reasons", []),
    }


# --------------------------------------------------------------------------
# 地区 × 伤情 覆盖空白
# --------------------------------------------------------------------------

def coverage_matrix(store, at=None):
    """统计各地区在不同伤情组合下的覆盖情况。

    覆盖判定：该地区至少有一家医院持有**有效（未暂停、未到期）**证书，
    且其取证演练覆盖了该（时段 × 血管口径）组合。保存条件作为第三维在
    明细中给出（是否演练过不当保存）。
    """
    at = at or now()
    reconcile(store, at)
    combos = [(t["key"], v["key"]) for t in TIME_BANDS for v in VESSEL_BANDS]
    regions = {}

    for hospital_id, cert_state in store.data["certificates"].items():
        hospital = store.get_hospital(hospital_id)
        if hospital is None:
            continue
        region = regions.setdefault(hospital["region"], {})
        current = cert_state.get("current")
        if not current or current["status"] != "active":
            continue
        for ledger_cert in cert_state["ledger"]:
            drill = store.data["drills"].get(ledger_cert["basis_drill_id"])
            if not drill:
                continue
            grading = _latest(store, drill["drill_id"])
            if not grading or not grading["passed"]:
                continue
            cell = region.setdefault((drill["time_band"], drill["vessel_band"]),
                                     {"hospitals": [], "unsound_preservation_seen": False})
            cell["hospitals"].append(hospital_id)
            if not drill["preservation_sound"]:
                cell["unsound_preservation_seen"] = True

    result = []
    all_regions = sorted({h["region"] for h in store.data["hospitals"].values()})
    for region in all_regions:
        cells = []
        gaps = 0
        for time_key, vessel_key in combos:
            cell = regions.get(region, {}).get((time_key, vessel_key))
            covered = bool(cell)
            if not covered:
                gaps += 1
            cells.append({
                "time_band": time_key,
                "vessel_band": vessel_key,
                "covered": covered,
                "hospitals": cell["hospitals"] if cell else [],
                "unsound_preservation_seen": cell["unsound_preservation_seen"] if cell else False,
            })
        result.append({"region": region, "total_cells": len(combos),
                       "gap_cells": gaps, "cells": cells})
    return result


def _latest(store, drill_id):
    versions = store.data["gradings"].get(drill_id) or []
    return versions[-1] if versions else None


# --------------------------------------------------------------------------
# 证书追溯
# --------------------------------------------------------------------------

def trace_certificate(store, cert_no):
    """从一张证书追到：原始演练、当时资源、事件链、评分依据、整改复测。"""
    with store.lock:
        target = None
        owner = None
        for hospital_id, cert_state in store.data["certificates"].items():
            for cert in cert_state["ledger"]:
                if cert["cert_no"] == cert_no:
                    target, owner = cert, hospital_id
        if target is None:
            raise NotFoundError(f"证书不存在：{cert_no}")

        drill_id = target["basis_drill_id"]
        drill = store.data["drills"].get(drill_id)
        if drill is None:
            raise ConflictError("证书关联的演练记录缺失", code="trace_broken")
        gradings = store.data["gradings"].get(drill_id, [])
        appeals = store.data["appeals"].get(drill_id, [])
        cert_state = store.data["certificates"][owner]
        retests = [
            {"cert_no": c["cert_no"], "basis_drill_id": c["basis_drill_id"],
             "issued_at": c["issued_at"], "status": c["status"]}
            for c in cert_state["ledger"]
            if c["issued_at"] >= target["issued_at"]
        ]
        return {
            "certificate": target,
            "hospital_id": owner,
            "drill": {
                "drill_id": drill["drill_id"],
                "region": drill["region"],
                "dispatched_at": drill["dispatched_at"],
                "deadline_at": drill["deadline_at"],
                "dimensions": {
                    "time_band": drill["time_band"],
                    "preservation": drill["preservation"],
                    "vessel_band": drill["vessel_band"],
                    "requires_supermicro": drill["requires_supermicro"],
                },
                "status": drill["status"],
            },
            "resource_snapshot_at_dispatch": drill["snapshot"],
            "submissions": drill["submissions"],
            "event_chain": drill["chain"],
            "pending_offline_events": list(drill.get("pending", {}).values()),
            "rubric_version": target.get("rubric_version"),
            "gradings": gradings,
            "appeals": appeals,
            "retest_certificates": retests,
        }
