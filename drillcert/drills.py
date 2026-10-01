"""盲演练派发、提交与离线回执的因果合并。

关键规则：

* 响应截止时间在**派发时**写死（``deadline_at``），任何重复提交都不会
  刷新它；首份提交的到达时间决定 on_time/late。
* 提交按 ``submission_id`` 幂等去重；网络重传同一提交返回首次回执。
* 断网期间产生的回执带 Lamport 风格的因果前驱（``after``）；重连后按
  因果边合并、按 ``event_id`` 去重，前驱未到的事件先挂起，前驱一到立即
  级联合并。
"""

from .clock import iso, now, parse
from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .firewall import assert_no_clinical_fields
from .resources import snapshot
from .scenario import generate_drill
from . import certificates as certs

DISPATCH_ROOT = "evt-dispatched"
TRIAGE_DECISIONS = ("accept", "transfer", "decline")
HIDDEN_FROM_HOSPITAL = ("rubric_key", "preservation_sound", "requires")


def dispatch_drill(store, hospital_id, *, seed, contract_sample=None, overrides=None):
    """生成并派发一场盲演练，冻结资源快照。"""
    hospital = store.require_hospital(hospital_id)
    with store.lock:
        seq = store.next_seq("drill_dispatch")
        for _attempt in range(20):
            scenario = generate_drill(
                region=hospital["region"],
                seed=f"{seed}#{seq}",
                contract_sample=contract_sample,
                overrides=overrides,
            )
            if scenario["drill_id"] not in store.data["drills"]:
                break
        else:
            raise ConflictError("演练编号冲突，请更换种子重试", code="drill_id_collision")

        dispatched_at = parse(scenario["dispatched_at"])
        # 响应时钟在派发这一刻锚定，之后任何提交都不刷新
        from datetime import timedelta

        deadline = dispatched_at + timedelta(minutes=scenario["response_deadline_minutes"])

        drill = {
            **scenario,
            "hospital_id": hospital_id,
            "dispatch_seq": seq,
            "deadline_at": iso(deadline),
            "status": "awaiting_response",
            "snapshot": snapshot(store, hospital_id, dispatched_at),
            "chain": [
                {
                    "event_id": DISPATCH_ROOT,
                    "at": scenario["dispatched_at"],
                    "type": "dispatched",
                    "after": [],
                }
            ],
            "pending": {},
            "submissions": [],
            "grading_refs": [],
        }
        store.data["drills"][scenario["drill_id"]] = drill
        store.data["events"].setdefault(scenario["drill_id"], [])
        store.data["pending_events"].setdefault(scenario["drill_id"], {})
        certs.mark_scheduled(store, hospital_id, scenario["drill_id"], dispatched_at)
        store.save()
        return hospital_view(drill)


def submit_response(store, drill_id, hospital_id, payload):
    """接收一份接诊响应；幂等、不刷新截止时钟。"""
    assert_no_clinical_fields(payload)
    drill = _require_drill(store, drill_id)
    if drill["hospital_id"] != hospital_id:
        raise ForbiddenError(f"演练不属于该医院：{drill_id}")

    submission_id = payload.get("submission_id")
    if not submission_id:
        raise ValidationError("缺少 submission_id（幂等键）")

    with store.lock:
        # 幂等：重传同一提交，原样返回首次回执，不产生新时钟效果
        for prior in drill["submissions"]:
            if prior["submission_id"] == submission_id:
                _merge_offline_batch(drill, payload.get("offline_receipts", []))
                store.save()
                return _receipt(prior, duplicate=True, drill=drill)

        _validate_submission(payload)
        arrived_at = now()
        first = not drill["submissions"]
        # 响应时钟只认首份提交；后续提交仅留痕
        response_at = arrived_at if first else parse(drill["submissions"][0]["arrived_at"])
        on_time = response_at <= parse(drill["deadline_at"])

        submission = {
            "submission_id": submission_id,
            "arrived_at": iso(arrived_at),
            "effective_response_at": iso(response_at),
            "first_submission": first,
            "on_time": on_time,
            "triage_decision": payload["triage_decision"],
            "locked_resources": payload["locked_resources"],
            "backup_plan": payload["backup_plan"],
            "note": payload.get("note"),
        }
        drill["submissions"].append(submission)
        _merge_offline_batch(drill, payload.get("offline_receipts", []))
        drill["chain"].append(
            {
                "event_id": f"evt-submit-{submission_id}",
                "at": iso(arrived_at),
                "type": "submitted",
                "after": [e["event_id"] for e in drill["chain"]],
                "submission_id": submission_id,
            }
        )
        if first:
            drill["status"] = "submitted" if on_time else "late"
            certs.mark_attended(store, hospital_id, drill_id, on_time, arrived_at)
        store.save()
        return _receipt(submission, duplicate=False, drill=drill)


def _validate_submission(payload):
    decision = payload.get("triage_decision")
    if decision not in TRIAGE_DECISIONS:
        raise ValidationError(f"triage_decision 必须是 {TRIAGE_DECISIONS} 之一")
    locked = payload.get("locked_resources") or {}
    evidence = locked.get("evidence") or []
    if not evidence:
        raise ValidationError("必须提交资源锁定证据（locked_resources.evidence）")
    for item in evidence:
        if not item.get("ref") or not item.get("sha256"):
            raise ValidationError("每条锁定证据需含 ref 与 sha256")
    if not payload.get("backup_plan"):
        raise ValidationError("必须提交备选方案 backup_plan")


def _merge_offline_batch(drill, batch):
    """把一批断网回执按因果合并进演练事件链。

    - event_id 去重；
    - after 中列出的前驱全部已知才可入链，否则挂起等待；
    - 入链后级联释放被它阻塞的挂起事件；
    - 最终链按因果做稳定拓扑排序（同序按 at、event_id）。
    """
    known = {e["event_id"]: e for e in drill["chain"]}
    pending = drill["pending"]

    for raw in batch or []:
        event = _normalize_receipt(raw)
        if event["event_id"] in known or event["event_id"] in pending:
            continue  # 幂等去重，重复回执丢弃
        pending[event["event_id"]] = event

    progressed = True
    while progressed:
        progressed = False
        for eid in sorted(pending, key=lambda k: (pending[k]["at"], k)):
            event = pending[eid]
            if all(p in known for p in event["after"]):
                known[eid] = event
                del pending[eid]
                progressed = True

    drill["chain"] = _topo_sort(list(known.values()))


def _normalize_receipt(raw):
    for field in ("event_id", "at", "type"):
        if field not in raw:
            raise ValidationError(f"离线回执缺少字段：{field}")
    try:
        at = iso(parse(raw["at"]))
    except (ValueError, TypeError):
        raise ValidationError(f"离线回执时间非法：{raw.get('at')!r}")
    after = raw.get("after") or []
    if not isinstance(after, list):
        raise ValidationError("after 必须是前驱 event_id 列表")
    event = {k: v for k, v in raw.items() if k != "at"}
    event["at"] = at
    event["after"] = after
    event["offline"] = True
    return event


def _topo_sort(events):
    by_id = {e["event_id"]: e for e in events}
    visited, ordered = set(), []

    def visit(eid):
        if eid in visited:
            return
        visited.add(eid)
        event = by_id[eid]
        preds = [p for p in event["after"] if p in by_id]
        for pred in sorted(preds, key=lambda p: (by_id[p]["at"], p)):
            visit(pred)
        ordered.append(event)

    for event in sorted(events, key=lambda e: (e["at"], e["event_id"])):
        visit(event["event_id"])
    return ordered


def _receipt(submission, duplicate, drill=None):
    return {
        "accepted": True,
        "duplicate": duplicate,
        "submission_id": submission["submission_id"],
        "on_time": submission["on_time"],
        "effective_response_at": submission["effective_response_at"],
        "deadline_at": drill["deadline_at"] if drill else None,
        "notice": "重复提交不会刷新响应时钟" if not duplicate and not submission["first_submission"] else None,
    }


def _require_drill(store, drill_id):
    from .firewall import assert_drill_id

    assert_drill_id(drill_id)
    drill = store.data["drills"].get(drill_id)
    if drill is None:
        raise NotFoundError(f"演练不存在：{drill_id}")
    return drill


def merge_offline_receipts(store, drill_id, batch):
    """网络恢复后，单独补送一批断网回执（同样按因果合并、幂等）。"""
    drill = _require_drill(store, drill_id)
    with store.lock:
        _merge_offline_batch(drill, batch)
        if batch:
            drill["chain"].append({
                "event_id": f"evt-reconnect-{len(drill['chain'])}",
                "at": iso(now()),
                "type": "reconnected",
                "after": [e["event_id"] for e in drill["chain"]],
            })
            drill["chain"] = _topo_sort(drill["chain"])
        store.save()
        return {"merged": True, "chain_len": len(drill["chain"]),
                "pending": sorted(drill["pending"].keys())}


def hospital_view(drill):
    """参演医院视图：隐藏评分要点与答案标记。"""
    return {k: v for k, v in drill.items() if k not in HIDDEN_FROM_HOSPITAL and k != "snapshot"}
