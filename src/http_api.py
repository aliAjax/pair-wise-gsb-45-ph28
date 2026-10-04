"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
WINDOW_RE = re.compile(r"^/api/tide-windows/(\d+)$")
WINDOW_BATCHES_RE = re.compile(r"^/api/tide-windows/(\d+)/batches$")
WINDOW_RUNS_RE = re.compile(r"^/api/tide-windows/(\d+)/recompute-runs$")
WINDOW_RECOMPUTE_RE = re.compile(r"^/api/tide-windows/(\d+)/recompute$")
DRAFT_RE = re.compile(r"^/api/conflict-drafts/(\d+)/(apply|discard)$")
REPORT_RE = re.compile(r"^/api/pilot-reports/(\d+)$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "port-berth/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                payload = {"error": exc.code, "message": str(exc)}
                if getattr(exc, "extra", None):
                    payload["extra"] = exc.extra
                self._send(exc.status, payload)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "port-berth", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    query = parse_qs(parsed.query)
                    records = service.list_records(self._actor(), state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                if parsed.path == "/api/tide-windows":
                    self._send(200, {"items": service.list_tide_windows(self._actor())})
                    return
                match = WINDOW_RE.match(parsed.path)
                if match:
                    view = service.plan_batches_view(self._actor(), int(match.group(1)))
                    self._send(200, view)
                    return
                match = WINDOW_BATCHES_RE.match(parsed.path)
                if match:
                    self._send(200, service.plan_batches_view(self._actor(), int(match.group(1))))
                    return
                match = WINDOW_RUNS_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.list_recompute_runs(self._actor(), int(match.group(1)))})
                    return
                if parsed.path == "/api/conflict-drafts":
                    self._send(200, {"items": service.list_drafts(self._actor())})
                    return
                if parsed.path == "/api/pilot-reports":
                    self._send(200, {"items": service.list_pilot_reports(self._actor())})
                    return
                match = REPORT_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_pilot_report(self._actor(), int(match.group(1))))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    window_id = body.get("tide_window_id")
                    if window_id is not None and not isinstance(window_id, int):
                        raise ValidationError("tide_window_id必须是整数")
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}),
                                            tide_window_id=window_id)
                    self._send(201, record)
                    return
                if parsed.path == "/api/tide-windows":
                    record = service.create_tide_window(self._actor(), body.get("data", {}))
                    self._send(201, record)
                    return
                match = WINDOW_RECOMPUTE_RE.match(parsed.path)
                if match:
                    result = service.recompute_window(self._actor(), int(match.group(1)),
                                                      trigger=body.get("trigger", "manual_retry"))
                    self._send(200, result)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    record_id = int(match.group(1))
                    action = match.group(2)
                    if action == "gate_release":
                        now_hour = body.get("data", {}).get("now_hour")
                        if not isinstance(now_hour, int):
                            raise ValidationError("gate_release需要data.now_hour整数")
                        result = service.gate_release(self._actor(), record_id, now_hour)
                        self._send(200, result)
                        return
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), record_id, version, action, body.get("data", {}))
                    self._send(200, record)
                    return
                if parsed.path == "/api/pilot-reports":
                    result = service.submit_pilot_report(self._actor(), body.get("data", {}))
                    self._send(200, result)
                    return
                match = DRAFT_RE.match(parsed.path)
                if match:
                    draft_id = int(match.group(1))
                    if match.group(2) == "apply":
                        result = service.apply_draft(self._actor(), draft_id)
                    else:
                        result = service.discard_draft(self._actor(), draft_id)
                    self._send(200, result)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_PUT(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                match = WINDOW_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    result = service.update_tide_window(self._actor(), int(match.group(1)), version,
                                                        body.get("data", {}), save_draft_on_conflict=True)
                    self._send(200, result)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
