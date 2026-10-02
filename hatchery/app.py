"""HTTP 服务（标准库）：JSON API + 本地照片存储 + 静态前端。

启动：python -m hatchery.app --init-seed
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import threading
import uuid
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import services
from .adapters import MockTemperatureAdapter
from .db import connect, init_db

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).with_name("static")
UPLOAD_DIR = BASE_DIR / "uploads"

temp_adapter = MockTemperatureAdapter(seed=20261002)


def _safe_name(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    return f"{uuid.uuid4().hex}{ext}"


class Handler(BaseHTTPRequestHandler):
    server_version = "HatcheryLedger/1.0"
    db_path: str = ""          # 由 main 注入
    _tls = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = connect(self.db_path)
            self._tls.conn = c
        return c

    # ---- 基础收发 ----
    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, status=400):
        self._json({"error": msg}, status)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            raise services.DomainError("请求不是合法 JSON")

    def _uid(self, data):
        uid = self.headers.get("X-User-Id") or data.pop("_user_id", None)
        if not uid:
            raise services.DomainError("缺少操作员身份（X-User-Id）")
        return int(uid)

    def log_message(self, fmt, *args):
        pass  # 静默；需要时可打开

    # ---- 路由 ----
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/users":
                rows = self.conn.execute(
                    "SELECT id,username,display_name,role FROM users ORDER BY id"
                ).fetchall()
                return self._json([dict(r) for r in rows])
            if p == "/api/batches":
                rows = self.conn.execute(
                    "SELECT b.*, COUNT(DISTINCT t.id) trays FROM batches b "
                    "LEFT JOIN trays t ON t.batch_id=b.id GROUP BY b.id ORDER BY b.id"
                ).fetchall()
                out = []
                for r in rows:
                    s = services.batch_summary(self.conn, r["id"])
                    out.append({**dict(r), **{
                        "set_total": s["set_total"],
                        "stopped": s["candle_losses"]["stopped"],
                        "cracked": s["candle_losses"]["cracked"],
                        "weak": s["hatch_losses"]["weak"],
                        "dead": s["hatch_losses"]["dead"],
                        "healthy": s["healthy"],
                        "watch": s["watch"],
                        "balance": s["balance"],
                        "hatch_rate": s["hatch_rate"],
                        "hatched": s["hatched"],
                    }})
                return self._json(out)
            if p.startswith("/api/batches/"):
                bid = int(p.rsplit("/", 1)[-1])
                s = services.batch_summary(self.conn, bid)
                s["trays"] = [dict(r) for r in self.conn.execute(
                    "SELECT t.*, i.code incubator_code, sl.shelf_no, sl.pos_no "
                    "FROM trays t LEFT JOIN slots sl ON sl.id=t.current_slot_id "
                    "LEFT JOIN incubators i ON i.id=sl.incubator_id "
                    "WHERE t.batch_id=? ORDER BY t.code", (bid,))]
                s["ledger"] = [dict(r) for r in self.conn.execute(
                    "SELECT l.*,u.display_name operator FROM quantity_ledger l "
                    "JOIN users u ON u.id=l.operator_id WHERE l.batch_id=? ORDER BY l.id",
                    (bid,))]
                s["photos"] = [dict(r) for r in self.conn.execute(
                    "SELECT p.id,p.file_path,p.caption,p.created_at,cr.tray_id,"
                    "t.code tray_code FROM candle_photos p "
                    "JOIN candle_records cr ON cr.id=p.candle_record_id "
                    "JOIN trays t ON t.id=cr.tray_id WHERE cr.batch_id=? ORDER BY p.id",
                    (bid,))]
                s["incidents"] = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM incidents WHERE batch_id=? ORDER BY id", (bid,))]
                s["candles"] = [dict(r) for r in self.conn.execute(
                    "SELECT cr.*, t.code tray_code FROM candle_records cr "
                    "JOIN trays t ON t.id=cr.tray_id WHERE cr.batch_id=? ORDER BY cr.id",
                    (bid,))]
                return self._json(s)
            if p == "/api/incubators":
                return self._json(self._incubator_map())
            if p == "/api/incidents":
                rows = self.conn.execute(
                    "SELECT ic.*, b.code batch_code, t.code tray_code, i.code incubator_code,"
                    "u.display_name operator FROM incidents ic "
                    "LEFT JOIN batches b ON b.id=ic.batch_id "
                    "LEFT JOIN trays t ON t.id=ic.tray_id "
                    "LEFT JOIN incubators i ON i.id=ic.incubator_id "
                    "JOIN users u ON u.id=ic.operator_id ORDER BY ic.id DESC"
                ).fetchall()
                return self._json([dict(r) for r in rows])
            if p.startswith("/api/photos/"):
                return self._serve_photo(int(p.rsplit("/", 1)[-1]))
            if p == "/api/temp":
                return self._json(temp_adapter.read())
            return self._serve_static(p)
        except services.DomainError as e:
            return self._err(str(e))
        except (ValueError, KeyError) as e:
            return self._err(f"请求参数错误：{e}", 400)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/batches":
                d = self._read_json(); uid = self._uid(d)
                bid = services.create_batch(
                    self.conn, source_flock=d["source_flock"],
                    collected_date=d["collected_date"],
                    storage_temp_c=float(d["storage_temp_c"]),
                    code=d.get("code"), operator_id=uid)
                return self._json({"id": bid}, 201)
            if p == "/api/place":
                d = self._read_json(); uid = self._uid(d)
                services.place_trays(
                    self.conn, batch_id=int(d["batch_id"]),
                    placements=d["placements"], operator_id=uid,
                    set_date=d.get("set_date"))
                return self._json({"ok": True}, 201)
            if p == "/api/move":
                d = self._read_json(); uid = self._uid(d)
                services.move_tray(
                    self.conn, tray_code=d["tray"], incubator_code=d["incubator"],
                    shelf=int(d["shelf"]), pos=int(d["pos"]), operator_id=uid,
                    incident_id=d.get("incident_id"), note=d.get("note", "转盘"),
                    expected_slot_id=d.get("expected_slot_id"))
                return self._json({"ok": True})
            if p == "/api/candle":
                return self._post_candle()
            if p == "/api/hatch":
                d = self._read_json(); uid = self._uid(d)
                hid = services.record_hatch(
                    self.conn, batch_id=int(d["batch_id"]),
                    healthy=int(d["healthy"]), watch=int(d["watch"]),
                    weak=int(d.get("weak", 0)), dead=int(d.get("dead", 0)),
                    delayed=int(d.get("delayed", 0)),
                    note=d.get("note", ""), operator_id=uid)
                return self._json({"id": hid}, 201)
            if p == "/api/reviews":
                d = self._read_json(); uid = self._uid(d)
                rid = services.sign_health_review(
                    self.conn, batch_id=int(d["batch_id"]),
                    conclusion=d["conclusion"], note=d.get("note", ""), user_id=uid)
                return self._json({"id": rid}, 201)
            if p == "/api/incidents":
                d = self._read_json(); uid = self._uid(d)
                iid = services.create_incident(
                    self.conn, type_=d["type"], batch_id=d.get("batch_id"),
                    tray_id=d.get("tray_id"), incubator_id=d.get("incubator_id"),
                    description=d["description"], disposition=d["disposition"],
                    operator_id=uid)
                return self._json({"id": iid}, 201)
            if p.startswith("/api/incidents/") and p.endswith("/handle"):
                iid = int(p.split("/")[3]); d = self._read_json(); uid = self._uid(d)
                services.handle_incident(self.conn, iid, uid, d.get("disposition"))
                return self._json({"ok": True})
            if p == "/api/incubator/stop":
                d = self._read_json(); uid = self._uid(d)
                iid = services.report_incubator_stop(
                    self.conn, incubator_id=int(d["incubator_id"]),
                    note=d.get("note", "模拟停电"), operator_id=uid,
                    batch_id=d.get("batch_id"))
                return self._json({"incident_id": iid}, 201)
            if p == "/api/incubator/resume":
                d = self._read_json(); uid = self._uid(d)
                services.resume_incubator(
                    self.conn, incubator_id=int(d["incubator_id"]),
                    incident_id=int(d["incident_id"]), operator_id=uid,
                    note=d.get("note", ""))
                return self._json({"ok": True})
            self._err("未知接口", 404)
        except sqlite3.IntegrityError as e:
            self._err(f"数据库约束拒绝：{e}", 409)
        except services.DomainError as e:
            self._err(str(e))

    # ---- 业务视图 ----
    def _incubator_map(self):
        out = []
        for inc in self.conn.execute("SELECT * FROM incubators ORDER BY code"):
            last = self.conn.execute(
                "SELECT event,temp_c,created_at FROM incubator_events "
                "WHERE incubator_id=? ORDER BY id DESC LIMIT 1", (inc["id"],)).fetchone()
            slots = []
            for s in self.conn.execute(
                "SELECT * FROM slots WHERE incubator_id=? ORDER BY shelf_no,pos_no",
                (inc["id"],)):
                occ = self.conn.execute(
                    "SELECT t.code,t.batch_id,b.code batch_code FROM slot_occupancy o "
                    "JOIN trays t ON t.id=o.tray_id LEFT JOIN batches b ON b.id=t.batch_id "
                    "WHERE o.slot_id=?", (s["id"],)).fetchone()
                slots.append({**dict(s), "occupant": dict(occ) if occ else None})
            out.append({**dict(inc),
                        "state": last["event"] if last else "running",
                        "last_event": dict(last) if last else None,
                        "slots": slots})
        return out

    def _post_candle(self):
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            n = int(self.headers.get("Content-Length", 0))
            msg = BytesParser(policy=email_default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + self.rfile.read(n))
            fields, files = {}, []
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if not name:
                    continue
                fn = part.get_filename()
                if fn:
                    files.append((name, fn, part.get_payload(decode=True)))
                else:
                    fields[name] = part.get_payload(decode=True).decode("utf-8")
            uid = int(self.headers.get("X-User-Id") or fields.pop("_user_id", 0))
            if not uid:
                raise services.DomainError("缺少操作员身份（X-User-Id）")
            photo_paths = self._save_photos(files)
            data = fields
        else:
            data = self._read_json(); uid = self._uid(data); photo_paths = []

        rid = services.candle_tray(
            self.conn, tray_code=data["tray"],
            fertile=int(data["fertile"]), stopped=int(data["stopped"]),
            cracked=int(data["cracked"]), operator_id=uid,
            note=data.get("note", ""), photo_paths=photo_paths)
        return self._json({"id": rid}, 201)

    def _save_photos(self, files):
        UPLOAD_DIR.mkdir(exist_ok=True)
        saved = []
        for _name, fn, blob in files:
            if not blob:
                continue
            rel = f"photos/{_safe_name(fn)}"
            path = UPLOAD_DIR / rel
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(blob)
            saved.append((rel, Path(fn).stem[:40]))
        return saved

    def _serve_photo(self, photo_id):
        row = self.conn.execute(
            "SELECT file_path,caption FROM candle_photos WHERE id=?", (photo_id,)
        ).fetchone()
        if not row:
            return self._err("照片不存在", 404)
        path = (UPLOAD_DIR / row["file_path"]).resolve()
        if UPLOAD_DIR.resolve() not in path.parents and path.parent != UPLOAD_DIR.resolve():
            return self._err("非法路径", 403)
        if not path.exists():
            return self._err("照片文件丢失", 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, p):
        if p == "/":
            p = "/index.html"
        path = (STATIC_DIR / p.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            return self._err("Not Found", 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE_DIR / "hatchery.db"))
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--init-seed", action="store_true", help="初始化并写入演示数据（含停机样例）")
    args = ap.parse_args()

    conn = connect(args.db)
    init_db(conn)
    if args.init_seed:
        from .seed import seed_all
        seed_all(conn)
        print("已写入演示数据（含孵化箱中途停机处置样例）")
    conn.close()
    Handler.db_path = args.db
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"种蛋孵化台账已启动 → http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
