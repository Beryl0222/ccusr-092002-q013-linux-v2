"""显微外科能力演练与认证服务入口。

用法：
  python3 service.py --check             # 服务身份与临床隔离自检
  python3 service.py                     # 启动 HTTP 服务（默认 :8000）
  python3 service.py --data data/drill.json
"""

import argparse

from drillcert.api import health_payload, make_server
from drillcert.errors import ValidationError
from drillcert.firewall import DRILL_ID_RE, REAL_ALERT_RE, assert_drill_id
from drillcert.service import DrillCertService

SERVICE_ID = "limb-salvage-network"
SERVICE_NAME = "断肢救治协同计时"


def self_check():
    """服务身份、模拟标记与编号隔离的静态自检。"""
    payload = health_payload()
    assert payload["service"] == SERVICE_ID
    assert payload["simulation_only"] is True
    assert DRILL_ID_RE.match("DRILL-0001-AB23")
    assert REAL_ALERT_RE.search("TRAUMA-006H-17")
    try:
        assert_drill_id("TRAUMA-006H-17")
    except ValidationError:
        pass
    else:  # pragma: no cover - 防御性断言
        raise AssertionError("真实告警号不得通过演练编号校验")
    print("基础检查通过（含临床隔离与模拟标记）")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None, help="JSON 数据文件路径（默认纯内存）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        self_check()
        return
    service = DrillCertService(args.data)
    make_server(service, args.port).serve_forever()


if __name__ == "__main__":
    main()
