"""盲演练场景生成。

依据 ``contracts/trauma_alert.json`` 约定的三个维度生成演练：

* 受伤时间 ``injury_at`` —— 映射到值班时段与缺血时限；
* 保存条件 ``preservation`` —— 正确/不当保存影响再植条件；
* 血管口径 ``injuries[].vessel_mm`` —— 决定是否需要超级显微外科能力。

生成结果只含演练情境，**不含评分要点**；评分要点由专家组的规则版本
保存（rubric_key 只是一个不可反推的引用），参演人员在提交截止前无法
看到。生成过程为纯函数、可由种子复现，绝不读取或生成真实患者数据。
"""

import hashlib
import random
from datetime import timedelta

from .clock import iso, now
from .firewall import assert_drill_id

#: 合同样例所在路径（仅取字段名与层级，不取任何真实业务数据）
CONTRACT_PATH = "contracts/trauma_alert.json"

# ---- 维度取值（模拟取值，与真实患者无关） ----

#: 受伤发生时段：考察值班团队在不同钟点的可用性
TIME_BANDS = [
    {"key": "workday_daytime", "label": "工作日白天", "hour": 10, "deadline_minutes": 30},
    {"key": "workday_evening", "label": "工作日晚间", "hour": 20, "deadline_minutes": 30},
    {"key": "night_deep", "label": "深夜", "hour": 3, "deadline_minutes": 45},
    {"key": "weekend_daytime", "label": "周末白天", "hour": 15, "deadline_minutes": 30},
]

#: 保存条件（含典型的不当保存，用于考察接诊判断）
PRESERVATIONS = [
    {"key": "cold_dry", "label": "冷藏干燥", "sound": True},
    {"key": "cold_wet", "label": "冷藏湿润包裹", "sound": True},
    {"key": "room_temp_dry", "label": "常温干燥", "sound": False},
    {"key": "frozen", "label": "直接冷冻", "sound": False},
    {"key": "soaked_formalin", "label": "消毒液浸泡", "sound": False},
]

#: 血管口径档（mm），最细档要求超级显微外科（0.3mm 级器械/资质）
VESSEL_BANDS = [
    {"key": "supermicro", "label": "指尖再植", "vessel_mm": 0.5, "min_mm": 0.0, "max_mm": 0.8},
    {"key": "digit", "label": "手指再植", "vessel_mm": 1.2, "min_mm": 0.8, "max_mm": 2.0},
    {"key": "major_limb", "label": "肢体再植", "vessel_mm": 2.5, "min_mm": 2.0, "max_mm": 6.0},
]

BODY_PARTS = {
    "supermicro": ["右手指尖", "左手指尖", "足趾尖"],
    "digit": ["右手中指", "左手拇指", "右手环指"],
    "major_limb": ["右前臂", "左小腿", "右上臂"],
}

#: 每个维度档位对应的评分要点键（隐藏；仅评分引擎按规则版本解析）
def rubric_key_for(time_key, preservation_key, vessel_key):
    digest = hashlib.sha256(
        f"rubric|{time_key}|{preservation_key}|{vessel_key}".encode("utf-8")
    ).hexdigest()[:10]
    return f"RUB-{digest}"


def vessel_band_for(vessel_mm):
    """把合同里的连续血管口径归入离散能力档。"""
    mm = float(vessel_mm)
    for band in VESSEL_BANDS:
        if band["min_mm"] <= mm < band["max_mm"]:
            return band
    raise ValueError(f"血管口径超出可演练范围：{vessel_mm}")


def preservation_by_key(key):
    for item in PRESERVATIONS:
        if item["key"] == key:
            return item
    raise KeyError(key)


def _seq(seed_text):
    return int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest(), 16)


def generate_drill(*, region, seed, contract_sample=None, overrides=None):
    """生成一场盲演练。

    :param region: 地区编码（用于覆盖空白统计）
    :param seed: 种子（建议用排期号），同种子结果可复现
    :param contract_sample: 可选，来自 trauma_alert.json 的 sample，
        提供时会按其 injury_at/preservation/vessel_mm 对齐维度
    :param overrides: 显式指定维度 ``time/preservation/vessel``
    :returns: 演练情境 dict（不含评分要点）
    """
    rng = random.Random(_seq(seed))
    overrides = overrides or {}

    if contract_sample:
        time_band = _band_from_injury_at(contract_sample.get("injury_at"))
        pres = preservation_by_label(contract_sample.get("preservation", ""))
        injuries = contract_sample.get("injuries") or []
        vessel = vessel_band_for(injuries[0]["vessel_mm"]) if injuries else rng.choice(VESSEL_BANDS)
        part = injuries[0]["part"] if injuries else rng.choice(BODY_PARTS[vessel["key"]])
    else:
        time_band = next((t for t in TIME_BANDS if t["key"] == overrides.get("time")), None) \
            or rng.choice(TIME_BANDS)
        pres = next((p for p in PRESERVATIONS if p["key"] == overrides.get("preservation")), None) \
            or rng.choice(PRESERVATIONS)
        vessel = next((v for v in VESSEL_BANDS if v["key"] == overrides.get("vessel")), None) \
            or rng.choice(VESSEL_BANDS)
        part = rng.choice(BODY_PARTS[vessel["key"]])

    serial = _seq(f"{seed}|{region}|{time_band['key']}|{pres['key']}|{vessel['key']}") % 10**4
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    suffix = "".join(rng.choice(alphabet) for _ in range(4))
    drill_id = f"DRILL-{serial:04d}-{suffix}"
    assert_drill_id(drill_id)

    dispatched_at = now()
    # 模拟受伤时间锚在派发日的对应钟点，缺血窗口按档位给死
    injury_at = dispatched_at.replace(hour=time_band["hour"], minute=5, second=0, microsecond=0)
    if injury_at > dispatched_at:
        injury_at -= timedelta(days=1)

    scenario = {
        "drill_id": drill_id,
        "region": region,
        "seed": seed,
        "dispatched_at": iso(dispatched_at),
        "injury_at": iso(injury_at),
        "time_band": time_band["key"],
        "time_band_label": time_band["label"],
        "preservation": pres["key"],
        "preservation_label": pres["label"],
        "preservation_sound": pres["sound"],
        "injuries": [{"part": part, "vessel_mm": vessel["vessel_mm"]}],
        "vessel_band": vessel["key"],
        "vessel_band_label": vessel["label"],
        "requires_supermicro": vessel["key"] == "supermicro",
        "response_deadline_minutes": time_band["deadline_minutes"],
        "rubric_key": rubric_key_for(time_band["key"], pres["key"], vessel["key"]),
        "requires": _resource_requirements(time_band["key"], pres["key"], vessel["key"]),
    }
    return scenario


def _band_from_injury_at(injury_at):
    """按合同样例的受伤时间归入时段档（按受伤地墙钟钟点；周末在合同中不可知，按钟点归并）。"""
    if injury_at is None:
        return TIME_BANDS[0]
    from datetime import datetime

    dt = datetime.fromisoformat(str(injury_at))  # 保留原时区偏移，取当地钟点
    hour = dt.hour
    if 0 <= hour < 6:
        return next(t for t in TIME_BANDS if t["key"] == "night_deep")
    if 18 <= hour <= 23:
        return next(t for t in TIME_BANDS if t["key"] == "workday_evening")
    return next(t for t in TIME_BANDS if t["key"] == "workday_daytime")


def preservation_by_label(label):
    for item in PRESERVATIONS:
        if item["label"] == label:
            return item
    # 合同出现未收录的保存条件时，按未知的不当保存处理（仍可演练）
    return {"key": "unknown", "label": label or "未知", "sound": False}


def _resource_requirements(time_key, preservation_key, vessel_key):
    """该情境要求医院锁定的关键资源（评分时对照 dispatch 快照核验）。"""
    req = {
        "staff_roles": ["microsurgeon", "anesthesiologist", "scrub_nurse"],
        "equipment": ["microscope", "microsurgical_set"],
        "commitment_needed": True,
        "green_channel": True,
        "supermicro_set": vessel_key == "supermicro",
        "night_oncall": time_key in ("night_deep", "workday_evening"),
        "preservation_advice": not preservation_by_key_sound(preservation_key),
    }
    if vessel_key == "major_limb":
        req["equipment"].append("vascular_shunt")
    return req


def preservation_by_key_sound(key):
    try:
        return preservation_by_key(key)["sound"]
    except KeyError:
        return False
