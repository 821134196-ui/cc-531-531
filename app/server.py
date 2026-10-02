"""零依赖 HTTP 服务：JSON API + 静态文件。"""
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db, service
from .errors import ConflictError, DomainError, NotFound, OverdueError, PermissionError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def make_handler(db_path: str, data_dir: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # ---- 基础工具 ----
        def _conn(self):
            return db.connect(db_path)

        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except json.JSONDecodeError:
                raise DomainError("请求体不是合法 JSON")

        def _error(self, e):
            status = 404 if isinstance(e, NotFound) else \
                     403 if isinstance(e, PermissionError) else \
                     409 if isinstance(e, ConflictError) else \
                     400 if isinstance(e, DomainError) else 500
            if status == 500:
                raise e
            self._send_json({"error": type(e).__name__, "message": str(e)}, status)

        # ---- 静态文件 ----
        def _serve_static(self, rel):
            rel = rel or "index.html"
            path = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
                path = os.path.join(STATIC_DIR, "index.html")
            ctype = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                     "js": "application/javascript; charset=utf-8",
                     "svg": "image/svg+xml", "png": "image/png",
                     "json": "application/json"}.get(path.rsplit(".", 1)[-1],
                                                    "application/octet-stream")
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_data(self, rel):
            path = os.path.normpath(os.path.join(data_dir, rel))
            if not path.startswith(os.path.abspath(data_dir)) or not os.path.isfile(path):
                self._send_json({"error": "NotFound", "message": "照片不存在"}, 404)
                return
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- 路由 ----
        def do_GET(self):
            parsed = urlparse(self.path)
            p = parsed.path.strip("/")
            q = {}
            if parsed.query:
                for kv in parsed.query.split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        q[k] = v
            try:
                if p == "api/meta":
                    return self._meta()
                if p == "api/batches":
                    return self._batches()
                if p.startswith("api/batches/"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.batch_detail(self._conn(), bid))
                if p == "api/incubators":
                    return self._incubators(q.get("id"))
                if p == "api/incidents":
                    return self._incidents()
                if p.startswith("data/"):
                    return self._serve_data(p[5:])
                if p.startswith("api/"):
                    return self._send_json({"error": "NotFound", "message": "未知接口"}, 404)
                self._serve_static(p)
            except (DomainError, sqlite3.Error) as e:
                self._error(e)
            except ValueError:
                self._send_json({"error": "BadRequest", "message": "ID 格式错误"}, 400)

        def do_POST(self):
            p = urlparse(self.path).path.strip("/")
            try:
                payload = self._read_json()
                conn = self._conn()
                if p == "api/batches":
                    return self._send_json(service.register_batch(
                        conn, int(payload["flock_id"]), payload["collected_at"],
                        float(payload["storage_temp_c"]), int(payload["egg_count"]),
                        int(payload["operator_id"]), payload.get("notes", "")), 201)
                if p.endswith("/set"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.set_batch(
                        conn, bid, int(payload["incubator_id"]),
                        payload["assignments"], int(payload["operator_id"])), 201)
                if p.endswith("/candle"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.add_candling(
                        conn, bid, int(payload["tray_id"]), payload["counts"],
                        int(payload["operator_id"]), payload.get("candled_at"),
                        payload.get("note", "")), 201)
                if p.endswith("/candle/finalize"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.finalize_candling(
                        conn, bid, int(payload["operator_id"])))
                if p.endswith("/hatch"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.hatch_batch(
                        conn, bid, int(payload["healthy"]), int(payload["observe"]),
                        int(payload.get("late", 0)), int(payload["operator_id"]),
                        payload.get("note", "")), 201)
                if p.endswith("/review"):
                    bid = int(p.split("/")[2])
                    return self._send_json(service.health_review(
                        conn, bid, int(payload["user_id"]),
                        payload["conclusion"], payload.get("note", "")), 201)
                if p == "api/move-tray":
                    return self._send_json(service.move_tray(
                        conn, int(payload["tray_id"]), int(payload["to_incubator_id"]),
                        int(payload["layer_no"]), int(payload["slot_no"]),
                        int(payload["operator_id"]), payload.get("reason", "transfer"),
                        payload.get("incident_id"), payload.get("expected_updated_at")), 201)
                if p == "api/report-shift":
                    return self._send_json(service.report_shift(
                        conn, int(payload["tray_id"]), int(payload["actual_incubator_id"]),
                        int(payload["layer_no"]), int(payload["slot_no"]),
                        int(payload["operator_id"]), payload["description"]), 201)
                if p == "api/incidents":
                    iid = service.open_incident_api(
                        conn, payload["kind"], payload.get("batch_id"),
                        payload.get("incubator_id"), payload["title"],
                        payload.get("description", ""), int(payload["reporter_id"]))
                    return self._send_json({"id": iid}, 201)
                if p.startswith("api/incidents/") and p.endswith("/handle"):
                    iid = int(p.split("/")[2])
                    service.handle_incident(conn, iid, int(payload["handler_id"]),
                                            payload["action"])
                    return self._send_json({"ok": True})
                if p == "api/incubators/stop":
                    return self._send_json(service.stop_incubator(
                        conn, int(payload["incubator_id"]), int(payload["operator_id"]),
                        payload.get("detail", "")), 201)
                if p == "api/incubators/resume":
                    return self._send_json(service.resume_incubator(
                        conn, int(payload["incubator_id"]), int(payload["operator_id"]),
                        payload.get("evacuation_action")), 201)
                self._send_json({"error": "NotFound", "message": "未知接口"}, 404)
            except DomainError as e:
                self._error(e)
            except (KeyError, ValueError) as e:
                self._send_json({"error": "BadRequest", "message": f"参数缺失或格式错误: {e}"}, 400)

        # ---- 聚合查询 ----
        def _meta(self):
            conn = self._conn()
            self._send_json({
                "users": [dict(r) for r in conn.execute("SELECT * FROM users ORDER BY id")],
                "flocks": [dict(r) for r in conn.execute("SELECT * FROM flocks ORDER BY id")],
                "incubators": [dict(r) for r in conn.execute(
                    "SELECT id,code,name,status,layer_count,slot_count FROM incubators ORDER BY id")],
            })

        def _batches(self):
            conn = self._conn()
            rows = conn.execute(
                """SELECT b.*, f.code AS flock_code, f.name AS flock_name, i.code AS incubator_code
                   FROM egg_batches b JOIN flocks f ON f.id=b.flock_id
                   LEFT JOIN incubators i ON i.id=b.incubator_id ORDER BY b.id""").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["balance"] = service.ledger_balance(conn, r["id"])
                d["hatch_rate_pct"] = (round(d["balance"]["healthy"] /
                                             d["balance"]["set_in"] * 100, 1)
                                       if d["balance"]["set_in"] else None)
                d["storage_age_days"] = service.storage_age_days(conn, r, service.now())
                out.append(d)
            self._send_json(out)

        def _incubators(self, inc_id):
            conn = self._conn()
            if inc_id:
                row = conn.execute("SELECT * FROM incubators WHERE id=?", (int(inc_id),)).fetchone()
                if not row:
                    raise NotFound("孵化箱不存在")
                slots = [dict(r) for r in conn.execute(
                    """SELECT s.*, p.tray_id, p.egg_count, p.status AS tray_status,
                              b.id AS batch_id, b.code AS batch_code, f.code AS flock_code,
                              t.code AS tray_code
                       FROM rack_slots s
                       LEFT JOIN tray_positions p ON p.slot_id=s.id AND p.status='active'
                       LEFT JOIN trays t ON t.id=p.tray_id
                       LEFT JOIN egg_batches b ON b.id=p.batch_id
                       LEFT JOIN flocks f ON f.id=b.flock_id
                       WHERE s.incubator_id=? ORDER BY s.layer_no,s.slot_no""",
                    (inc_id,)).fetchall()]
                temps = [dict(r) for r in conn.execute(
                    "SELECT temp_c,read_at FROM temp_readings WHERE incubator_id=? ORDER BY id",
                    (inc_id,)).fetchall()]
                stops = [dict(r) for r in conn.execute(
                    "SELECT * FROM stop_events WHERE incubator_id=? ORDER BY id",
                    (inc_id,)).fetchall()]
                signals = [dict(r) for r in conn.execute(
                    "SELECT kind,occurred_at,detail FROM signal_events WHERE incubator_id=? "
                    "ORDER BY id", (inc_id,)).fetchall()]
                return self._send_json({"incubator": dict(row), "slots": slots,
                                        "temps": temps, "stops": stops, "signals": signals})
            self._send_json([dict(r) for r in conn.execute(
                "SELECT * FROM incubators ORDER BY id").fetchall()])

        def _incidents(self):
            conn = self._conn()
            rows = conn.execute(
                """SELECT n.*, b.code AS batch_code, i.code AS incubator_code,
                          u1.name AS reporter_name, u2.name AS handler_name
                   FROM incidents n
                   LEFT JOIN egg_batches b ON b.id=n.batch_id
                   LEFT JOIN incubators i ON i.id=n.incubator_id
                   LEFT JOIN users u1 ON u1.id=n.reporter_id
                   LEFT JOIN users u2 ON u2.id=n.handler_id
                   ORDER BY n.id DESC""").fetchall()
            self._send_json([dict(r) for r in rows])

    return Handler


def serve(db_path: str, data_dir: str, host: str = "127.0.0.1", port: int = 8080):
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, data_dir))
    print(f"孵化管理已启动：http://{host}:{port}")
    httpd.serve_forever()
