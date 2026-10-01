"""HTTP 路由层（stdlib，无第三方依赖）。

所有响应都带 X-Simulation-Only 头；所有写接口都先过临床隔离扫描。
路径参数只支持本服务固定形态，未匹配返回 404。
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DomainError, PayloadError
from .firewall import SIMULATION_BANNER, simulation_headers

SERVICE_ID = "limb-salvage-network"


def health_payload():
    from . import SERVICE_NAME

    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME,
            "simulation_only": True, "banner": SIMULATION_BANNER}


ROUTES = [
    ("POST", r"^/hospitals$", "register_hospital"),
    ("PUT", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/staff$", "upsert_staff"),
    ("GET", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/staff$", "list_staff"),
    ("PUT", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/equipment$", "upsert_equipment"),
    ("GET", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/equipment$", "list_equipment"),
    ("PUT", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/green-channel$", "set_green_channel"),
    ("POST", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/availability$", "add_window"),
    ("POST", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/drills$", "dispatch"),
    ("GET", r"^/hospitals/(?P<hid>[A-Za-z0-9]+)/capability$", "capability"),
    ("GET", r"^/drills/(?P<did>DRILL-[0-9A-Z-]+)$", "get_drill"),
    ("POST", r"^/drills/(?P<did>DRILL-[0-9A-Z-]+)/response$", "submit"),
    ("POST", r"^/drills/(?P<did>DRILL-[0-9A-Z-]+)/receipts$", "merge_receipts"),
    ("POST", r"^/drills/(?P<did>DRILL-[0-9A-Z-]+)/grade$", "grade"),
    ("POST", r"^/drills/(?P<did>DRILL-[0-9A-Z-]+)/appeal$", "appeal"),
    ("POST", r"^/graders$", "register_grader"),
    ("POST", r"^/rubrics$", "publish_rubric"),
    ("POST", r"^/reviews$", "review"),
    ("POST", r"^/admin/reconcile$", "reconcile"),
    ("GET", r"^/coverage$", "coverage"),
    ("GET", r"^/certificates/(?P<cert>CERT-[A-Za-z0-9-]+)/trace$", "trace"),
]


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in simulation_headers().items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PayloadError(f"请求体不是合法 JSON：{exc}")
            if not isinstance(payload, dict):
                raise PayloadError("请求体必须是 JSON 对象")
            return payload

        def _hospital_id(self):
            hid = self.headers.get("X-Hospital-Id")
            if not hid:
                raise DomainError("缺少 X-Hospital-Id 头", code="missing_hospital_header",
                                  http_status=401)
            return hid

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def _route(self, method):
            try:
                if method == "GET" and self.path == "/health":
                    self._send(200, health_payload())
                    return
                for verb, pattern, action in ROUTES:
                    if verb != method:
                        continue
                    match = re.match(pattern, self.path)
                    if match:
                        self._dispatch(action, match.groupdict())
                        return
                self._send(404, {"error": "not_found", "message": f"无此路由：{method} {self.path}"})
            except DomainError as exc:
                self._send(exc.http_status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001 - 兜底，避免泄漏堆栈
                self._send(500, {"error": "internal_error", "message": str(exc)})

        def _dispatch(self, action, params):
            svc = service
            payload = self._read_json() if self.command in ("POST", "PUT") else {}

            if action == "register_hospital":
                result = svc.register_hospital(payload), 201
            elif action == "upsert_staff":
                result = svc.upsert_staff(params["hid"], payload), 200
            elif action == "list_staff":
                result = svc.list_staff(params["hid"]), 200
            elif action == "upsert_equipment":
                result = svc.upsert_equipment(params["hid"], payload), 200
            elif action == "list_equipment":
                result = svc.list_equipment(params["hid"]), 200
            elif action == "set_green_channel":
                result = svc.set_green_channel(params["hid"], payload), 200
            elif action == "add_window":
                result = svc.add_window(params["hid"], payload), 201
            elif action == "dispatch":
                result = svc.dispatch(params["hid"], payload), 201
            elif action == "capability":
                result = svc.capability(params["hid"]), 200
            elif action == "get_drill":
                result = svc.get_drill(params["did"]), 200
            elif action == "submit":
                result = svc.submit(params["did"], self._hospital_id(), payload), 200
            elif action == "merge_receipts":
                result = svc.merge_receipts(params["did"], payload), 200
            elif action == "grade":
                result = svc.grade(params["did"], payload), 200
            elif action == "appeal":
                result = svc.appeal(params["did"], self._hospital_id(), payload), 201
            elif action == "register_grader":
                result = svc.register_grader(payload), 201
            elif action == "publish_rubric":
                result = svc.publish_rubric(payload), 201
            elif action == "review":
                result = svc.review(payload), 200
            elif action == "reconcile":
                result = svc.reconcile(), 200
            elif action == "coverage":
                result = svc.coverage(), 200
            elif action == "trace":
                result = svc.trace(params["cert"]), 200
            else:  # pragma: no cover - 路由表与分派保持一致
                self._send(404, {"error": "not_found", "message": action})
                return

            body, status = result
            self._send(status, {"data": body, "simulation_notice": SIMULATION_BANNER})

        def log_message(self, *_args):
            return

    return Handler


def make_server(service, port=8000):
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(service))
