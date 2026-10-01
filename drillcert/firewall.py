"""临床隔离防火墙。

硬性约束（领域红线，任何入口都必须经过这里）：

1. 演练编号与真实告警严格隔离：真实告警样例形如 ``TRAUMA-006H-17``，
   演练编号必须使用独立的 ``DRILL-`` 号段，任何输入若携带真实告警号
   一律拒绝。
2. 任何模拟结果都不得写回临床病历：请求中出现病历/EHR/真实患者标识
   字段一律拒绝，服务内部对象也不设此类字段。
3. 所有出站响应都带 ``X-Simulation-Only: 1`` 与免责声明，防止下游误把
   演练数据当成真实调度指令。
"""

import re

from .errors import ValidationError

SERVICE_ID = "limb-salvage-network"
SIMULATION_HEADER = "X-Simulation-Only"
SIMULATION_BANNER = "本记录为能力模拟演练数据，禁止用于真实患者调度，禁止写回临床病历。"

#: 演练编号号段（独立于真实告警号段）
DRILL_ID_RE = re.compile(r"^DRILL-\d{4}-[A-Z0-9]{4}$")
#: 真实告警号段（来自 contracts/trauma_alert.json 的样例形态）
REAL_ALERT_RE = re.compile(r"TRAUMA-\d+[A-Z]?-\d+", re.IGNORECASE)

#: 禁止出现的字段名（命中即拒绝，防止写回病历/EHR）
_FORBIDDEN_KEY_RE = re.compile(
    r"(medical_?record|emr|ehr|patient_?id|clinical_?record|病历|病案|真实患者)",
    re.IGNORECASE,
)


def assert_drill_id(drill_id):
    """确保编号属于演练号段，且不得伪装成真实告警。"""
    if not isinstance(drill_id, str) or not DRILL_ID_RE.match(drill_id):
        raise ValidationError(
            f"演练编号必须形如 DRILL-9999-XXXX：{drill_id!r}",
            code="bad_drill_id",
        )
    if REAL_ALERT_RE.search(drill_id):
        raise ValidationError("演练编号不得引用真实告警号段", code="clinical_isolation_violation")
    return drill_id


def assert_no_clinical_fields(obj, path=""):
    """递归扫描载荷，发现病历/真实患者字段立即拒绝。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            keypath = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _FORBIDDEN_KEY_RE.search(key):
                raise ValidationError(
                    f"字段 {keypath} 涉及临床病历或真实患者标识，模拟数据禁止写回病历",
                    code="clinical_isolation_violation",
                )
            assert_no_clinical_fields(value, keypath)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            assert_no_clinical_fields(item, f"{path}[{index}]")
    elif isinstance(obj, str):
        # 字符串值里也不允许夹带真实告警号，防止演练挂到真实事件上。
        if REAL_ALERT_RE.search(obj):
            raise ValidationError(
                f"{path or '载荷'} 中出现真实告警编号，演练必须与真实告警严格隔离",
                code="clinical_isolation_violation",
            )


def simulation_headers():
    """出站统一的模拟标记头（HTTP 头只允许 latin-1，中文提示放响应体）。"""
    return {
        SIMULATION_HEADER: "1",
        "X-Service": SERVICE_ID,
        "X-Clinical-Writeback": "forbidden",
    }
