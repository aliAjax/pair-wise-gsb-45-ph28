"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, DraftConflict, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
WINDOW_RECOMPUTE_RE = re.compile(r"^/api/windows/(\d+)/recompute$")
DRAFT_RE = re.compile(r"^/api/drafts/(\d+)$")
DRAFT_ACTION_RE = re.compile(r"^/api/drafts/(\d+)/(apply|discard)$")
PILOT_REPORT_RE = re.compile(r"^/api/pilot-reports/([^/]+)$")


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
                body: Dict[str, Any] = {"error": exc.code, "message": str(exc)}
                if isinstance(exc, DraftConflict):
                    body["draft_id"] = exc.draft_id
                self._send(exc.status, body)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def _query_window_id(self, query: Dict[str, list]):
            raw = query.get("window_id", [None])[0]
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except ValueError as exc:
                raise ValidationError("window_id必须是整数") from exc

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
                if parsed.path == "/api/windows":
                    self._send(200, {"items": service.list_windows(self._actor())})
                    return
                if parsed.path == "/api/batches":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.list_batches(self._actor(), window_id=self._query_window_id(query))})
                    return
                if parsed.path == "/api/gate":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.gate_list(self._actor(), window_id=self._query_window_id(query))})
                    return
                if parsed.path == "/api/recompute-runs":
                    query = parse_qs(parsed.query)
                    self._send(200, {"items": service.latest_runs(self._actor(), window_id=self._query_window_id(query))})
                    return
                if parsed.path == "/api/drafts":
                    query = parse_qs(parsed.query)
                    raw_plan = query.get("plan_id", [None])[0]
                    plan_id = int(raw_plan) if raw_plan else None
                    self._send(200, {"items": service.list_drafts(self._actor(), plan_id=plan_id)})
                    return
                match = DRAFT_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_draft(self._actor(), int(match.group(1))))
                    return
                if parsed.path == "/api/pilot-reports":
                    self._send(200, {"items": service.list_pilot_reports(self._actor())})
                    return
                match = PILOT_REPORT_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_pilot_report(self._actor(), match.group(1)))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                if parsed.path == "/api/windows":
                    self._send(201, service.create_window(self._actor(), body.get("data", {})))
                    return
                match = WINDOW_RECOMPUTE_RE.match(parsed.path)
                if match:
                    service.replan(self._actor(), int(match.group(1)))
                    self._send(200, {"items": service.list_batches(self._actor(), window_id=int(match.group(1)))})
                    return
                match = DRAFT_ACTION_RE.match(parsed.path)
                if match:
                    if match.group(2) == "apply":
                        self._send(200, service.apply_draft(self._actor(), int(match.group(1))))
                    else:
                        self._send(200, service.discard_draft(self._actor(), int(match.group(1))))
                    return
                if parsed.path == "/api/pilot-reports":
                    report = service.submit_pilot_report(self._actor(), body.get("ticket", ""), body.get("items", []))
                    self._send(201, report)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
