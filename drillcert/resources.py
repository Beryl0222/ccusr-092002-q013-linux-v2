"""医院与资源维护。

医院预先维护四类资源，演练派发时整体冻结为快照（评分与追溯都只认
快照，事后改数据不影响已派发的演练）：

* 人员资质 staff —— 角色、证书有效期；
* 显微设备点检 equipment —— 点检有效期，过期/失败即不可用；
* 绿色通道 channels —— 是否开放、确认时间；
* 可承诺时段 availability —— 承诺可接诊的时间区间。
"""

from .clock import iso, now, parse
from .errors import ConflictError, ValidationError
from .firewall import assert_no_clinical_fields

ROLES = ("microsurgeon", "supermicrosurgeon", "anesthesiologist", "scrub_nurse")
EQUIPMENT_KINDS = ("microscope", "microsurgical_set", "supermicro_set", "vascular_shunt")


# --------------------------------------------------------------------------
# 医院
# --------------------------------------------------------------------------

def register_hospital(store, hospital_id, name, region):
    assert_no_clinical_fields({"hospital_id": hospital_id, "name": name})
    if not hospital_id or not str(hospital_id).isalnum():
        raise ValidationError("医院ID须为字母数字", code="bad_hospital_id")
    with store.lock:
        if store.get_hospital(hospital_id):
            raise ConflictError(f"医院已存在：{hospital_id}", code="hospital_exists")
        hospital = {
            "hospital_id": hospital_id,
            "name": name,
            "region": region,
            "created_at": iso(now()),
        }
        store.data["hospitals"][hospital_id] = hospital
        store.data["certificates"][hospital_id] = {"current": None, "ledger": []}
        store.save()
        return hospital


# --------------------------------------------------------------------------
# 人员资质
# --------------------------------------------------------------------------

def upsert_staff(store, hospital_id, staff):
    """新增/更新一名人员资质。staff 需含 staff_id/name/role/valid_until。"""
    store.require_hospital(hospital_id)
    role = staff.get("role")
    if role not in ROLES:
        raise ValidationError(f"未知角色：{role}（允许：{ROLES}）")
    valid_until = parse(staff["valid_until"])
    valid_from = parse(staff.get("valid_from", now()))
    if valid_from >= valid_until:
        raise ValidationError("资质生效时间必须早于到期时间")
    assert_no_clinical_fields(staff)
    record = {
        "staff_id": staff["staff_id"],
        "name": staff.get("name", staff["staff_id"]),
        "role": role,
        "credential_no": staff.get("credential_no"),
        "valid_from": iso(valid_from),
        "valid_until": iso(valid_until),
        "status": staff.get("status", "active"),
        "updated_at": iso(now()),
    }
    with store.lock:
        items = store.list_for("staff", hospital_id)
        for index, existing in enumerate(items):
            if existing["staff_id"] == record["staff_id"]:
                items[index] = {**existing, **record}
                store.save()
                return items[index]
        items.append(record)
        store.save()
        return record


def staff_valid_at(record, at):
    return (
        record.get("status") == "active"
        and parse(record["valid_from"]) <= at <= parse(record["valid_until"])
    )


# --------------------------------------------------------------------------
# 设备点检
# --------------------------------------------------------------------------

def upsert_equipment(store, hospital_id, equipment):
    """新增/更新设备点检记录，需含 equipment_id/kind/checked_at/next_check_due/status。"""
    store.require_hospital(hospital_id)
    kind = equipment.get("kind")
    if kind not in EQUIPMENT_KINDS:
        raise ValidationError(f"未知设备类型：{kind}（允许：{EQUIPMENT_KINDS}")
    status = equipment.get("status", "ok")
    if status not in ("ok", "failed"):
        raise ValidationError("设备状态只允许 ok/failed")
    checked_at = parse(equipment.get("checked_at", now()))
    next_due = parse(equipment["next_check_due"])
    if next_due <= checked_at:
        raise ValidationError("下次点检时间必须晚于本次点检时间")
    assert_no_clinical_fields(equipment)
    record = {
        "equipment_id": equipment["equipment_id"],
        "kind": kind,
        "checked_at": iso(checked_at),
        "next_check_due": iso(next_due),
        "status": status,
        "note": equipment.get("note"),
        "updated_at": iso(now()),
    }
    with store.lock:
        items = store.list_for("equipment", hospital_id)
        for index, existing in enumerate(items):
            if existing["equipment_id"] == record["equipment_id"]:
                items[index] = {**existing, **record}
                store.save()
                return items[index]
        items.append(record)
        store.save()
        return record


def equipment_usable_at(record, at):
    return record.get("status") == "ok" and at < parse(record["next_check_due"])


# --------------------------------------------------------------------------
# 绿色通道
# --------------------------------------------------------------------------

def set_green_channel(store, hospital_id, open_, note=None):
    store.require_hospital(hospital_id)
    with store.lock:
        store.data["channels"][hospital_id] = {
            "open": bool(open_),
            "confirmed_at": iso(now()),
            "note": note,
        }
        store.save()
        return store.data["channels"][hospital_id]


def channel_open_at(store, hospital_id, at):
    channel = store.data["channels"].get(hospital_id)
    return bool(channel and channel["open"] and parse(channel["confirmed_at"]) <= at)


# --------------------------------------------------------------------------
# 可承诺时段
# --------------------------------------------------------------------------

def add_commitment_window(store, hospital_id, starts_at, ends_at, covers_roles=None):
    store.require_hospital(hospital_id)
    start, end = parse(starts_at), parse(ends_at)
    if start >= end:
        raise ValidationError("承诺时段开始必须早于结束")
    with store.lock:
        windows = store.list_for("availability", hospital_id)
        wid = f"W{len(windows) + 1:03d}"
        for existing in windows:
            if start < parse(existing["ends_at"]) and parse(existing["starts_at"]) < end:
                raise ConflictError("可承诺时段不得重叠", code="window_overlap")
        record = {
            "window_id": wid,
            "starts_at": iso(start),
            "ends_at": iso(end),
            "covers_roles": covers_roles or list(ROLES),
        }
        windows.append(record)
        store.save()
        return record


def committed_at(store, hospital_id, at):
    """at 时刻是否落在医院承诺时段内。"""
    for window in store.list_for("availability", hospital_id):
        if parse(window["starts_at"]) <= at < parse(window["ends_at"]):
            return window
    return None


# --------------------------------------------------------------------------
# 派发时资源快照
# --------------------------------------------------------------------------

def snapshot(store, hospital_id, at):
    """冻结派发时刻的资源状态，供评分与长期追溯使用。"""
    store.require_hospital(hospital_id)
    with store.lock:
        staff = [dict(s) for s in store.list_for("staff", hospital_id)]
        equipment = [dict(e) for e in store.list_for("equipment", hospital_id)]
        channel = dict(store.data["channels"].get(hospital_id) or {})
        windows = [dict(w) for w in store.list_for("availability", hospital_id)]
    return {
        "taken_at": iso(at),
        "hospital_id": hospital_id,
        "staff": staff,
        "staff_valid": [s["staff_id"] for s in staff if staff_valid_at(s, at)],
        "roles_available": sorted({s["role"] for s in staff if staff_valid_at(s, at)}),
        "equipment": equipment,
        "equipment_usable": [e["equipment_id"] for e in equipment if equipment_usable_at(e, at)],
        "green_channel_open": channel_open_at(store, hospital_id, at),
        "commitment_windows": windows,
        "committed": committed_at(store, hospital_id, at) is not None,
    }
