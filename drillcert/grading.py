"""评分规则版本、评委回避、申诉复核。

* 评分规则由专家组以**不可变版本**发布，带生效期；一场演练用哪一版，
  按其派发时刻落在哪个生效期决定，事后发布的新规不溯及既往。
* 评分要点（criteria）对参演医院保密；评分完成后随结果只开放已评项的
  得分依据，用于证书追溯。
* 评委与被评医院存在利益关系（任职/顾问/亲属等登记的 affiliations）时
  必须回避，系统直接拒绝其参与该医院的评分或复核。
* 申诉不改动原评分：每次申诉复核只**追加一个不可变版本**（v2、v3…），
  初评原样保留。
"""

from .clock import iso, now, parse
from .errors import ConflictError, NotFoundError, ValidationError, ForbiddenError
from .scenario import rubric_key_for
from . import certificates as certs

PASS_THRESHOLD = 0.8
MANUAL_ITEMS = ("triage_rationale", "backup_quality")


# --------------------------------------------------------------------------
# 评委与利益关系
# --------------------------------------------------------------------------

def register_grader(store, grader_id, name, affiliations=None):
    """登记评委；affiliations 为存在利益关系的 hospital_id 列表。"""
    if not grader_id:
        raise ValidationError("缺少 grader_id")
    with store.lock:
        if grader_id in store.data["graders"]:
            raise ConflictError("评委已登记", code="grader_exists")
        record = {
            "grader_id": grader_id,
            "name": name,
            "affiliations": sorted(set(affiliations or [])),
            "registered_at": iso(now()),
        }
        store.data["graders"][grader_id] = record
        store.save()
        return record


def _assert_no_conflict(store, grader_ids, hospital_id):
    grader_ids = sorted(set(grader_ids))
    if not grader_ids:
        raise ValidationError("至少指定一名评委")
    rejected = []
    for gid in grader_ids:
        grader = store.data["graders"].get(gid)
        if grader is None:
            raise NotFoundError(f"评委未登记：{gid}")
        if hospital_id in grader["affiliations"]:
            rejected.append(gid)
    if rejected:
        raise ForbiddenError(
            f"评委与被评医院存在利益关系，须回避：{rejected}",
            code="conflict_of_interest",
        )
    return grader_ids


# --------------------------------------------------------------------------
# 评分规则版本（不可变、带生效期）
# --------------------------------------------------------------------------

def publish_rubric(store, version, valid_from, valid_to, *, published_by, criteria=None):
    """发布一个评分规则版本。

    生效区间半开：[valid_from, valid_to)；valid_to 为 None 表示长期有效，
    但同一时间只允许一个版本开放（新区间不得与既有区间重叠）。
    """
    vf = parse(valid_from)
    vt = parse(valid_to) if valid_to else None
    if vt and vf >= vt:
        raise ValidationError("规则生效时间必须早于失效时间")
    with store.lock:
        for existing in store.data["rubrics"]:
            if existing["version"] == version:
                raise ConflictError("规则版本号已存在", code="rubric_version_exists")
            evf = parse(existing["valid_from"])
            evt = parse(existing["valid_to"]) if existing.get("valid_to") else None
            if vt is None or evt is None:
                raise ConflictError(
                    "存在开放生效期的规则版本，新区间必须先与其错开",
                    code="rubric_overlap",
                )
            if vf < evt and evf < vt:
                raise ConflictError("规则生效期不得与既有版本重叠", code="rubric_overlap")
        record = {
            "version": version,
            "valid_from": iso(vf),
            "valid_to": iso(vt) if vt else None,
            "published_by": published_by,
            "published_at": iso(now()),
            "immutable": True,
            "criteria": criteria or default_criteria(),
        }
        store.data["rubrics"].append(record)
        store.save()
        return record


def effective_rubric(store, at):
    """取 at 时刻生效的规则版本；无生效版本则报错。"""
    candidates = []
    for rubric in store.data["rubrics"]:
        vf = parse(rubric["valid_from"])
        vt = parse(rubric["valid_to"]) if rubric.get("valid_to") else None
        if vf <= at and (vt is None or at < vt):
            candidates.append(rubric)
    if not candidates:
        raise ConflictError("该演练派发时刻没有生效中的评分规则", code="no_effective_rubric")
    if len(candidates) > 1:
        raise ConflictError("生效规则版本不唯一", code="rubric_overlap")
    return candidates[0]


def default_criteria():
    """构造标准评分要点（专家组可在发布时替换）。对参演医院保密。"""
    template = []
    for time_key in ("workday_daytime", "workday_evening", "night_deep", "weekend_daytime"):
        for pres_key in ("cold_dry", "cold_wet", "room_temp_dry", "frozen", "soaked_formalin"):
            for vessel_key in ("supermicro", "digit", "major_limb"):
                key = rubric_key_for(time_key, pres_key, vessel_key)
                items = [
                    {"key": "timely", "label": "在响应时限内提交", "max": 20, "kind": "auto"},
                    {"key": "triage", "label": "接诊判断正确", "max": 25, "kind": "auto"},
                    {"key": "resource_lock", "label": "关键资源锁定证据完整", "max": 30, "kind": "auto"},
                    {"key": "preservation_ack", "label": "正确识别保存条件风险", "max": 10, "kind": "auto"},
                    {"key": "triage_rationale", "label": "判断依据阐述", "max": 8, "kind": "manual"},
                    {"key": "backup_quality", "label": "备选方案可行性", "max": 7, "kind": "manual"},
                ]
                template.append({"rubric_key": key, "dimensions": {
                    "time_band": time_key, "preservation": pres_key, "vessel_band": vessel_key,
                }, "items": items})
    return template


def _criteria_for(rubric, rubric_key):
    for entry in rubric["criteria"]:
        if entry["rubric_key"] == rubric_key:
            return entry
    raise NotFoundError("当前规则版本未覆盖该伤情组合", code="rubric_gap")


# --------------------------------------------------------------------------
# 评分
# --------------------------------------------------------------------------

def grade_drill(store, drill_id, grader_ids, manual_scores=None, note=None):
    """专家评分（初评）。manual_scores: {grader_id: {item_key: score}}。"""
    drill = _require_submitted_drill(store, drill_id)
    hospital_id = drill["hospital_id"]
    graders = _assert_no_conflict(store, grader_ids, hospital_id)
    with store.lock:
        versions = store.data["gradings"].setdefault(drill_id, [])
        if versions:
            raise ConflictError("已有评分版本，复核请走申诉流程", code="grading_exists")
        rubric = effective_rubric(store, parse(drill["dispatched_at"]))
        entry = _criteria_for(rubric, drill["rubric_key"])
        auto = _auto_scores(drill, entry)
        manual = _collect_manual(entry, graders, manual_scores or {})
        record = _build_version(
            seq=1, kind="initial", rubric=rubric, entry=entry,
            graders=graders, auto=auto, manual=manual, note=note,
        )
        versions.append(record)
        drill["grading_refs"].append(record["grading_id"])
        cert_outcome = certs.record_grading_outcome(store, drill_id)
        store.save()
        result = public_grading(record)
        result["certificate"] = cert_outcome
        return result


def appeal(store, drill_id, hospital_id, reason):
    """医院提起申诉；只形成复核任务，绝不改动初评。"""
    drill = _require_submitted_drill(store, drill_id)
    if drill["hospital_id"] != hospital_id:
        raise ForbiddenError("只有参演医院可以申诉")
    if not store.data["gradings"].get(drill_id):
        raise ConflictError("尚未评分，无法申诉", code="no_grading")
    with store.lock:
        appeals = store.data["appeals"].setdefault(drill_id, [])
        appeal_id = f"APL-{len(appeals) + 1:03d}-{drill_id[-4:]}"
        record = {
            "appeal_id": appeal_id,
            "drill_id": drill_id,
            "hospital_id": hospital_id,
            "reason": reason,
            "raised_at": iso(now()),
            "status": "open",
            "resulting_version": None,
        }
        appeals.append(record)
        store.save()
        return dict(record)


def review_appeal(store, appeal_id, reviewer_ids, manual_scores=None, conclusion=None):
    """对申诉出具复核：追加不可变版本，初评保留。"""
    with store.lock:
        found = None
        for drill_id, appeals in store.data["appeals"].items():
            for apl in appeals:
                if apl["appeal_id"] == appeal_id:
                    found = (drill_id, apl)
        if not found:
            raise NotFoundError(f"申诉不存在：{appeal_id}")
        drill_id, apl = found
        if apl["status"] != "open":
            raise ConflictError("该申诉已复核", code="appeal_closed")
        drill = _require_submitted_drill(store, drill_id)
        reviewers = _assert_no_conflict(store, reviewer_ids, drill["hospital_id"])
        prior = store.data["gradings"][drill_id]
        rubric = effective_rubric(store, parse(drill["dispatched_at"]))
        entry = _criteria_for(rubric, drill["rubric_key"])
        auto = _auto_scores(drill, entry)
        manual = _collect_manual(entry, reviewers, manual_scores or {})
        record = _build_version(
            seq=len(prior) + 1, kind="review", rubric=rubric, entry=entry,
            graders=reviewers, auto=auto, manual=manual, note=conclusion,
            appeal_id=appeal_id,
        )
        prior.append(record)
        apl["status"] = "reviewed"
        apl["resulting_version"] = record["grading_id"]
        drill["grading_refs"].append(record["grading_id"])
        cert_outcome = certs.record_grading_outcome(store, drill_id)
        store.save()
        result = public_grading(record)
        result["certificate"] = cert_outcome
        return result


def _build_version(*, seq, kind, rubric, entry, graders, auto, manual, note, appeal_id=None):
    items = []
    total = 0
    max_total = 0
    by_key = {item["key"]: item for item in entry["items"]}
    for key in ("timely", "triage", "resource_lock", "preservation_ack",
                "triage_rationale", "backup_quality"):
        item = by_key[key]
        if item["kind"] == "auto":
            score = auto[key]["score"]
            basis = auto[key]["basis"]
        else:
            per = manual[key]
            score = per["score"]
            basis = {"by_grader": per["by_grader"]}
        items.append({"key": key, "label": item["label"], "kind": item["kind"],
                      "score": round(score, 2), "max": item["max"], "basis": basis})
        total += score
        max_total += item["max"]
    return {
        "grading_id": f"GRD-{seq}-{rubric['version']}",
        "grading_version": seq,
        "kind": kind,
        "appeal_id": appeal_id,
        "drill_dimensions": entry["dimensions"],
        "rubric_version": rubric["version"],
        "graders": graders,
        "items": items,
        "total": round(total, 2),
        "max_total": max_total,
        "passed": total >= PASS_THRESHOLD * max_total,
        "note": note,
        "graded_at": iso(now()),
        "immutable": True,
    }


def _collect_manual(entry, graders, manual_scores):
    by_key = {i["key"]: i for i in entry["items"] if i["kind"] == "manual"}
    result = {}
    for key, item in by_key.items():
        per = {}
        for gid in graders:
            scores = manual_scores.get(gid) or manual_scores.get("*") or {}
            score = scores.get(key)
            if score is None:
                raise ValidationError(f"评委 {gid} 缺少人工评分项：{key}")
            if not 0 <= float(score) <= item["max"]:
                raise ValidationError(f"{key} 得分超出 0~{item['max']}")
            per[gid] = float(score)
        result[key] = {"score": sum(per.values()) / len(per), "by_grader": per}
    return result


def _auto_scores(drill, entry):
    submission = drill["submissions"][0]
    snapshot = drill["snapshot"]
    requires = drill["requires"]

    # 1) 时限
    timely = {"score": 20.0 if submission["on_time"] else 0.0,
              "basis": {"on_time": submission["on_time"],
                        "deadline_at": drill["deadline_at"],
                        "effective_response_at": submission["effective_response_at"]}}

    # 2) 接诊判断：保存条件不可挽回或本院不具备对应能力时，accept 为误判
    capable = _capable(drill, snapshot, requires)
    sound = drill["preservation_sound"]
    decision = submission["triage_decision"]
    acceptable = {"accept"} if (capable and sound) else {"transfer", "decline"}
    triage_ok = decision in acceptable
    triage = {"score": 25.0 if triage_ok else 0.0,
              "basis": {"decision": decision, "acceptable": sorted(acceptable),
                        "capable": capable, "preservation_sound": sound}}

    # 3) 资源锁定证据：逐项对照派发快照
    locked = submission["locked_resources"]
    evidence = locked.get("evidence") or []
    need_roles = set(requires["staff_roles"])
    if drill["requires_supermicro"]:
        need_roles.add("supermicrosurgeon")
    have_roles = {e.get("role") for e in evidence if e.get("type") == "staff"}
    role_ok = need_roles <= have_roles
    need_equip = set(requires["equipment"])
    if requires.get("supermicro_set"):
        need_equip.add("supermicro_set")
    have_equip = {e.get("kind") for e in evidence if e.get("type") == "equipment"}
    equip_ok = need_equip <= have_equip
    # 证据还必须对应快照中真实有效/可⽤的资源
    valid_staff = set(snapshot["staff_valid"])
    usable_equip = set(snapshot["equipment_usable"])
    ev_refs = {e.get("ref") for e in evidence}
    evidence_real = ev_refs <= (valid_staff | usable_equip)
    channel_ok = (not requires["green_channel"]) or locked.get("green_channel_confirmed") is True
    checks = {"roles_complete": role_ok, "equipment_complete": equip_ok,
              "evidence_matches_snapshot": evidence_real, "green_channel": channel_ok}
    resource_score = 30.0 * sum(checks.values()) / len(checks)
    resource = {"score": resource_score, "basis": {**checks,
                 "needed_roles": sorted(need_roles), "locked_roles": sorted(r for r in have_roles if r),
                 "needed_equipment": sorted(need_equip), "locked_equipment": sorted(e for e in have_equip if e)}}

    # 4) 保存条件风险识别
    concern = locked.get("preservation_concern") is True
    ack_ok = (not requires.get("preservation_advice")) or concern
    preservation = {"score": 10.0 if ack_ok else 0.0,
                    "basis": {"needed_ack": requires.get("preservation_advice", False),
                              "declared_concern": concern}}

    return {"timely": timely, "triage": triage, "resource_lock": resource,
            "preservation_ack": preservation}


def _capable(drill, snapshot, requires):
    roles = set(snapshot["roles_available"])
    if requires.get("supermicro_set") and "supermicrosurgeon" not in roles:
        return False
    if not set(requires["staff_roles"]) <= roles:
        return False
    kinds = {e["kind"] for e in snapshot["equipment"]
             if e["equipment_id"] in snapshot["equipment_usable"]}
    if not set(requires["equipment"]) <= kinds:
        return False
    if requires.get("supermicro_set") and "supermicro_set" not in kinds:
        return False
    if requires.get("night_oncall") and not snapshot["committed"]:
        return False
    if requires["green_channel"] and not snapshot["green_channel_open"]:
        return False
    return True


def public_grading(record):
    """对医院公开的评分版本：给出得分依据，但不暴露规则要点全集。"""
    view = {k: v for k, v in record.items() if k != "drill_dimensions"}
    return view


def latest_grading(store, drill_id):
    versions = store.data["gradings"].get(drill_id) or []
    if not versions:
        return None
    return versions[-1]


def _require_submitted_drill(store, drill_id):
    from .firewall import assert_drill_id

    assert_drill_id(drill_id)
    drill = store.data["drills"].get(drill_id)
    if drill is None:
        raise NotFoundError(f"演练不存在：{drill_id}")
    if not drill["submissions"]:
        raise ConflictError("演练尚未收到响应，无法评分", code="no_submission")
    return drill
