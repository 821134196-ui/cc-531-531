"""领域服务：入孵、转盘、照蛋、异常处置、出雏、健康复核。

所有写操作都在一个 BEGIN IMMEDIATE 事务里完成：
- 数量只通过 quantity_ledger 追加（set/loss/harvest/move），历史不可删；
- 盘位占用由 slot_occupancy 实体表保证（slot 与 tray 双向唯一）；
- 角色在服务层与数据库触发器双重校验。
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

STORAGE_LIMIT_DAYS = 7  # 种蛋储存上限（收集日 → 入孵日）

INCIDENT_TYPES = {
    "mixed_source": "混放来源",
    "over_storage": "储存超期",
    "incubator_stop": "孵化箱中途停机",
    "missed_tray": "照蛋漏盘",
    "tray_shift": "托盘移位",
    "delayed_pip": "破壳延期",
}


class DomainError(Exception):
    """业务规则冲突（超期、守恒、独占、权限等）。"""


# ---------- 基础工具 ----------

def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _require_role(conn, user_id: int, allowed: tuple[str, ...], action: str):
    u = _row(conn, "SELECT id, role, display_name FROM users WHERE id=?", (user_id,))
    if u is None:
        raise DomainError("操作员不存在")
    if u["role"] not in allowed:
        names = {"operator": "操作员", "keeper": "饲养员", "vet": "兽医"}
        raise DomainError(
            f"{names[u['role']]}无权执行「{action}」（需要{'/'.join(names[r] for r in allowed)}）"
        )
    return u


def _balance(conn, batch_id: int) -> int:
    """批次数量结余（入孵 − 已入账损耗/收成）。"""
    r = _row(
        conn,
        "SELECT COALESCE(SUM(delta),0) AS b FROM quantity_ledger "
        "WHERE batch_id=? AND reversed=0",
        (batch_id,),
    )
    return r["b"]


def _tray_balance(conn, tray_id: int) -> int:
    r = _row(
        conn,
        "SELECT COALESCE(SUM(delta),0) AS b FROM quantity_ledger "
        "WHERE tray_id=? AND reversed=0",
        (tray_id,),
    )
    return r["b"]


def _current_stage(conn, batch_id: int) -> str:
    """按批次实际进度推断当前阶段（set/candle/hatch），转盘流水归入正确阶段。"""
    if _row(conn, "SELECT 1 FROM hatch_records WHERE batch_id=? LIMIT 1", (batch_id,)):
        return "hatch"
    if _row(conn, "SELECT 1 FROM candle_records WHERE batch_id=? LIMIT 1", (batch_id,)):
        return "candle"
    return "set"


def _slot(conn, incubator_code: str, shelf_no: int, pos_no: int) -> int:
    r = _row(
        conn,
        "SELECT s.id FROM slots s JOIN incubators i ON i.id=s.incubator_id "
        "WHERE i.code=? AND s.shelf_no=? AND s.pos_no=?",
        (incubator_code, shelf_no, pos_no),
    )
    if r is None:
        raise DomainError(f"盘位 {incubator_code} {shelf_no}层{pos_no}位 不存在")
    return r["id"]


def _incubator_running(conn, incubator_id: int) -> bool:
    r = _row(
        conn,
        "SELECT event FROM incubator_events WHERE incubator_id=? "
        "ORDER BY id DESC LIMIT 1",
        (incubator_id,),
    )
    return r is None or r["event"] != "stopped"


def _next_code(conn, prefix: str, table: str) -> str:
    n = conn.execute(f"SELECT COALESCE(MAX(id),0)+1 AS n FROM {table}").fetchone()["n"]
    return f"{prefix}{date.today():%Y%m%d}-{n:03d}"


def _append_ledger(conn, *, batch_id, tray_id, stage, change_type, reason,
                   delta, operator_id):
    bal = _balance(conn, batch_id) + delta
    if bal < 0:
        raise DomainError("数量不能为负：本批损耗/收成超过在孵结余")
    conn.execute(
        "INSERT INTO quantity_ledger(batch_id,tray_id,stage,change_type,reason,"
        "delta,balance_after,operator_id) VALUES(?,?,?,?,?,?,?,?)",
        (batch_id, tray_id, stage, change_type, reason, delta, bal, operator_id),
    )
    return bal


# ---------- 批次与入孵 ----------

def create_batch(conn, *, code=None, source_flock, collected_date,
                 storage_temp_c, operator_id) -> int:
    _require_role(conn, operator_id, ("operator", "vet"), "登记种蛋批次")
    datetime.strptime(collected_date, "%Y-%m-%d")  # 校验日期格式
    code = code or _next_code(conn, "B", "batches")
    cur = conn.execute(
        "INSERT INTO batches(code,source_flock,collected_date,storage_temp_c) "
        "VALUES(?,?,?,?)",
        (code, source_flock, collected_date, storage_temp_c),
    )
    return cur.lastrowid


def _storage_days(collected_date: str, set_date: date) -> int:
    return (set_date - date.fromisoformat(collected_date)).days


def place_trays(conn, *, batch_id, placements, operator_id, set_date=None):
    """入孵上盘。placements: [{tray, incubator, shelf, pos, eggs}]"""
    _require_role(conn, operator_id, ("operator", "vet"), "入孵上盘")
    if isinstance(set_date, str):
        set_date = date.fromisoformat(set_date)
    set_date = set_date or date.today()
    batch = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if batch is None:
        raise DomainError("批次不存在")

    days = _storage_days(batch["collected_date"], set_date)
    overdue = days > STORAGE_LIMIT_DAYS
    if overdue:
        ok = _row(
            conn,
            "SELECT 1 FROM incidents WHERE batch_id=? AND type='over_storage' "
            "AND status='handled' LIMIT 1",
            (batch_id,),
        )
        if not ok:
            raise DomainError(
                f"储存超期：收集日 {batch['collected_date']} 至入孵已 {days} 天 "
                f"（上限 {STORAGE_LIMIT_DAYS} 天）。请先登记「储存超期」异常处置并签收后再入孵"
            )

    conn.execute("BEGIN IMMEDIATE")
    try:
        for p in placements:
            inc = _row(conn, "SELECT id FROM incubators WHERE code=?",
                       (p["incubator"],))
            if inc is None:
                raise DomainError(f"孵化箱 {p['incubator']} 不存在")
            if not _incubator_running(conn, inc["id"]):
                raise DomainError(f"孵化箱 {p['incubator']} 处于停机状态，禁止上盘")
            slot_id = _slot(conn, p["incubator"], p["shelf"], p["pos"])

            occ = _row(conn, "SELECT tray_id FROM slot_occupancy WHERE slot_id=?",
                       (slot_id,))
            if occ is not None:
                t = _row(conn, "SELECT code FROM trays WHERE id=?", (occ["tray_id"],))
                raise DomainError(f"盘位已被托盘 {t['code']} 占用")

            tray = _row(conn, "SELECT * FROM trays WHERE code=?", (p["tray"],))
            if tray is None:
                cur = conn.execute(
                    "INSERT INTO trays(code,batch_id,egg_count,current_slot_id) "
                    "VALUES(?,?,?,?)",
                    (p["tray"], batch_id, p["eggs"], slot_id),
                )
                tray_id = cur.lastrowid
            else:
                tray_id = tray["id"]
                if tray["batch_id"] is not None and tray["batch_id"] != batch_id:
                    raise DomainError(
                        f"托盘 {p['tray']} 已属于另一批次，疑似混放来源，请先登记异常处置"
                    )
                if tray["current_slot_id"] is not None:
                    raise DomainError(f"托盘 {p['tray']} 已在箱内，不能重复上盘")
                conn.execute(
                    "UPDATE trays SET batch_id=?,egg_count=?,current_slot_id=? WHERE id=?",
                    (batch_id, p["eggs"], slot_id, tray_id),
                )

            conn.execute(
                "INSERT INTO slot_occupancy(slot_id,tray_id) VALUES(?,?)",
                (slot_id, tray_id),
            )
            conn.execute(
                "INSERT INTO tray_movements(tray_id,from_slot_id,to_slot_id,"
                "operator_id,note) VALUES(?,?,?,?,?)",
                (tray_id, None, slot_id, operator_id, "入孵上盘"),
            )
            _append_ledger(conn, batch_id=batch_id, tray_id=tray_id, stage="set",
                           change_type="set", reason="set", delta=p["eggs"],
                           operator_id=operator_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 异常处置 ----------

def create_incident(conn, *, type_, batch_id=None, tray_id=None, incubator_id=None,
                    description, disposition, operator_id) -> int:
    if type_ not in INCIDENT_TYPES:
        raise DomainError("异常类型无效")
    _require_role(conn, operator_id, ("operator", "vet"), "登记异常处置")
    code = _next_code(conn, "YC", "incidents")
    cur = conn.execute(
        "INSERT INTO incidents(code,type,batch_id,tray_id,incubator_id,"
        "description,disposition,operator_id,status) VALUES(?,?,?,?,?,?,?,?,'open')",
        (code, type_, batch_id, tray_id, incubator_id, description,
         disposition, operator_id),
    )
    return cur.lastrowid


def handle_incident(conn, incident_id: int, operator_id: int,
                    disposition: str | None = None) -> None:
    _require_role(conn, operator_id, ("operator", "vet"), "签收异常处置")
    inc = _row(conn, "SELECT * FROM incidents WHERE id=?", (incident_id,))
    if inc is None:
        raise DomainError("异常记录不存在")
    if inc["status"] == "handled":
        raise DomainError("该异常已处置签收，不能重复签收")
    conn.execute(
        "UPDATE incidents SET status='handled', handled_at=datetime('now','localtime'),"
        "disposition=COALESCE(?,disposition) WHERE id=?",
        (disposition, incident_id),
    )


# ---------- 孵化箱停机（配合模拟信号适配器） ----------

def report_incubator_stop(conn, *, incubator_id, note, operator_id,
                          batch_id=None) -> int:
    """停机信号到达：写事件并立异常案（open，待处置）。"""
    _require_role(conn, operator_id, ("operator", "vet"), "上报孵化箱停机")
    conn.execute(
        "INSERT INTO incubator_events(incubator_id,event,note) VALUES(?, 'stopped', ?)",
        (incubator_id, note),
    )
    return create_incident(
        conn, type_="incubator_stop", batch_id=batch_id, incubator_id=incubator_id,
        description=f"孵化箱中途停机：{note}",
        disposition="停机待处置：转移蛋盘并评估胚蛋，处置后签收恢复",
        operator_id=operator_id,
    )


def resume_incubator(conn, *, incubator_id, incident_id, operator_id, note="") -> None:
    """恢复运行：必须先签收停机异常。"""
    _require_role(conn, operator_id, ("operator", "vet"), "恢复孵化箱")
    inc = _row(conn, "SELECT * FROM incidents WHERE id=? AND type='incubator_stop'",
               (incident_id,))
    if inc is None or inc["status"] != "handled":
        raise DomainError("停机异常未处置签收，孵化箱不能恢复运行")
    conn.execute(
        "INSERT INTO incubator_events(incubator_id,event,note) VALUES(?, 'resumed', ?)",
        (incubator_id, note),
    )


# ---------- 转盘 ----------

def move_tray(conn, *, tray_code, incubator_code, shelf, pos, operator_id,
              incident_id=None, note="转盘", expected_slot_id=None) -> None:
    """两名操作员并发转盘时，BEGIN IMMEDIATE + 唯一约束保证一盘不会同时占两位。

    expected_slot_id：操作员发起转盘时看到的源盘位（乐观锁）。
    若该盘已被另一人先转走（当前位置变化），本次移动拒绝并要求刷新重盘，
    避免两人都以为自己转成功、一盘蛋“出现在两台机器”。
    """
    _require_role(conn, operator_id, ("operator", "vet"), "转盘")
    tray = _row(conn, "SELECT * FROM trays WHERE code=?", (tray_code,))
    if tray is None:
        raise DomainError(f"托盘 {tray_code} 不存在")
    if tray["batch_id"] is None:
        raise DomainError("空托盘无批次记录")
    if expected_slot_id is None:
        expected_slot_id = tray["current_slot_id"]
    elif tray["current_slot_id"] != expected_slot_id:
        raise DomainError(
            f"托盘 {tray_code} 的位置已变动（可能已被其他操作员转走），请刷新盘位图后重新操作"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 锁内重读，防止取快照后被抢先提交
        fresh = _row(conn, "SELECT * FROM trays WHERE id=?", (tray["id"],))
        if fresh["current_slot_id"] != expected_slot_id:
            raise DomainError(
                f"转盘冲突：托盘 {tray_code} 已被其他操作员移动，一盘蛋不能同时出现在两台机器"
            )
        inc = _row(conn, "SELECT id FROM incubators WHERE code=?", (incubator_code,))
        if inc is None:
            raise DomainError(f"孵化箱 {incubator_code} 不存在")
        if not _incubator_running(conn, inc["id"]):
            raise DomainError(f"目标孵化箱 {incubator_code} 已停机，禁止转入")
        slot_id = _slot(conn, incubator_code, shelf, pos)

        occ = _row(conn, "SELECT tray_id FROM slot_occupancy WHERE slot_id=?",
                   (slot_id,))
        if occ is not None and occ["tray_id"] != tray["id"]:
            t = _row(conn, "SELECT code FROM trays WHERE id=?", (occ["tray_id"],))
            raise DomainError(
                f"盘位冲突：{incubator_code} {shelf}层{pos}位 已被托盘 {t['code']} 占用，"
                "一盘蛋不能同时出现在两台机器/两个盘位"
            )

        from_slot = fresh["current_slot_id"]
        conn.execute("DELETE FROM slot_occupancy WHERE tray_id=?", (tray["id"],))
        conn.execute(
            "INSERT INTO slot_occupancy(slot_id,tray_id) VALUES(?,?)",
            (slot_id, tray["id"]),
        )
        cur = conn.execute(
            "UPDATE trays SET current_slot_id=? WHERE id=? AND current_slot_id IS ?",
            (slot_id, tray["id"], from_slot),
        )
        if cur.rowcount == 0:
            raise DomainError(f"转盘冲突：托盘 {tray_code} 刚被其他操作员移动")
        conn.execute(
            "INSERT INTO tray_movements(tray_id,from_slot_id,to_slot_id,incident_id,"
            "operator_id,note) VALUES(?,?,?,?,?,?)",
            (tray["id"], from_slot, slot_id, incident_id, operator_id, note),
        )
        _append_ledger(conn, batch_id=tray["batch_id"], tray_id=tray["id"],
                       stage=_current_stage(conn, tray["batch_id"]),
                       change_type="move", reason="move",
                       delta=0, operator_id=operator_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------- 照蛋 ----------

def candle_tray(conn, *, tray_code, fertile, stopped, cracked, operator_id,
                note="", photo_paths=None) -> int:
    _require_role(conn, operator_id, ("operator", "vet"), "照蛋登记")
    tray = _row(conn, "SELECT * FROM trays WHERE code=?", (tray_code,))
    if tray is None or tray["batch_id"] is None:
        raise DomainError(f"托盘 {tray_code} 不存在或未上盘")
    if _row(conn, "SELECT 1 FROM candle_records WHERE tray_id=? LIMIT 1", (tray["id"],)):
        raise DomainError(
            f"托盘 {tray_code} 已照蛋，不能重复照蛋；如需更正请登记异常处置并红字冲正")
    alive = _tray_balance(conn, tray["id"])
    total = fertile + stopped + cracked
    if total != alive:
        raise DomainError(
            f"照蛋数量对不上：受精{fertile}+停育{stopped}+破损{cracked}={total}，"
            f"托盘当前在孵 {alive}，三者必须相等（不能把损耗直接从历史里删掉）"
        )
    cur = conn.execute(
        "INSERT INTO candle_records(tray_id,batch_id,fertile,stopped,cracked,"
        "operator_id,note) VALUES(?,?,?,?,?,?,?)",
        (tray["id"], tray["batch_id"], fertile, stopped, cracked, operator_id, note),
    )
    record_id = cur.lastrowid
    for path, caption in photo_paths or []:
        conn.execute(
            "INSERT INTO candle_photos(candle_record_id,file_path,caption) VALUES(?,?,?)",
            (record_id, path, caption),
        )
    # 照蛋损耗入账（停育、破损各一条流水，原因可追溯）
    for qty, reason in ((stopped, "stopped"), (cracked, "cracked")):
        if qty:
            _append_ledger(conn, batch_id=tray["batch_id"], tray_id=tray["id"],
                           stage="candle", change_type="loss", reason=reason,
                           delta=-qty, operator_id=operator_id)
    return record_id


# ---------- 出雏 ----------

def record_hatch(conn, *, batch_id, healthy, watch, weak, dead,
                 operator_id, delayed=0, note="") -> int:
    _require_role(conn, operator_id, ("operator", "vet"), "出雏统计")
    if min(healthy, watch, weak, dead, delayed) < 0:
        raise DomainError("数量不能为负")
    if delayed > watch:
        raise DomainError("破壳延期个体必须计入待观察，延期数不能大于待观察数")

    # 照蛋漏盘校验：每盘要么有照蛋记录，要么有已签收的漏盘处置
    trays = conn.execute(
        "SELECT id, code FROM trays WHERE batch_id=?", (batch_id,)
    ).fetchall()
    for t in trays:
        has_candle = _row(conn, "SELECT 1 FROM candle_records WHERE tray_id=? LIMIT 1",
                          (t["id"],))
        if has_candle:
            continue
        excused = _row(
            conn,
            "SELECT 1 FROM incidents WHERE tray_id=? AND type='missed_tray' "
            "AND status='handled' LIMIT 1",
            (t["id"],),
        )
        if not excused:
            raise DomainError(
                f"托盘 {t['code']} 没有照蛋记录（照蛋漏盘），请先登记并签收漏盘处置再出雏"
            )

    balance = _balance(conn, batch_id)
    accounted = healthy + watch + weak + dead
    if accounted != balance:
        raise DomainError(
            f"出雏数量守恒失败：健康{healthy}+待观察{watch}+弱雏{weak}+死胎{dead}"
            f"={accounted}，照蛋后在孵结余 {balance}，必须逐一对上"
        )

    cur = conn.execute(
        "INSERT INTO hatch_records(batch_id,healthy,watch,weak,dead,operator_id) "
        "VALUES(?,?,?,?,?,?)",
        (batch_id, healthy, watch, weak, dead, operator_id),
    )
    hatch_id = cur.lastrowid
    for qty, ctype, reason in (
        (weak, "loss", "weak"),
        (dead, "loss", "dead"),
        (healthy, "harvest", "healthy"),
        (watch, "harvest", "watch"),
    ):
        if qty:
            _append_ledger(conn, batch_id=batch_id, tray_id=None, stage="hatch",
                           change_type=ctype, reason=reason, delta=-qty,
                           operator_id=operator_id)

    if delayed:
        iid = create_incident(
            conn, type_="delayed_pip", batch_id=batch_id,
            description=f"出雏时 {delayed} 只破壳延期，已计入待观察",
            disposition="转入待观察栏继续护理，24 小时后复查", operator_id=operator_id,
        )
        conn.execute(
            "UPDATE incidents SET status='handled', handled_at=datetime('now','localtime') "
            "WHERE id=?", (iid,),
        )

    # 雏鸡离箱：托盘出箱留痕（数量已全部入账，余额归零）
    for t in trays:
        conn.execute(
            "INSERT INTO tray_movements(tray_id,from_slot_id,to_slot_id,"
            "operator_id,note) "
            "SELECT id,current_slot_id,NULL,?,'出雏落盘' FROM trays WHERE id=?",
            (operator_id, t["id"]),
        )
        conn.execute("DELETE FROM slot_occupancy WHERE tray_id=?", (t["id"],))
        conn.execute(
            "UPDATE trays SET current_slot_id=NULL,egg_count=0 WHERE id=?", (t["id"],)
        )
    return hatch_id


# ---------- 健康复核（兽医专用） ----------

def sign_health_review(conn, *, batch_id, conclusion, note, user_id) -> int:
    """触发器兜底：非兽医签署直接被数据库拒绝（饲养员不能代签）。"""
    _require_role(conn, user_id, ("vet",), "签署健康结论")
    cur = conn.execute(
        "INSERT INTO health_reviews(batch_id,conclusion,note,vet_id) VALUES(?,?,?,?)",
        (batch_id, conclusion, note, user_id),
    )
    return cur.lastrowid


# ---------- 查询 ----------

def batch_summary(conn, batch_id: int) -> dict:
    b = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if b is None:
        raise DomainError("批次不存在")
    led = conn.execute(
        "SELECT stage,change_type,reason,SUM(delta) d FROM quantity_ledger "
        "WHERE batch_id=? AND reversed=0 GROUP BY stage,change_type,reason",
        (batch_id,),
    ).fetchall()
    agg = {(r["stage"], r["change_type"], r["reason"]): r["d"] for r in led}
    set_total = sum(v for (s, c, _r), v in agg.items() if c == "set")
    stopped = -agg.get(("candle", "loss", "stopped"), 0)
    cracked = -agg.get(("candle", "loss", "cracked"), 0)
    weak = -agg.get(("hatch", "loss", "weak"), 0)
    dead = -agg.get(("hatch", "loss", "dead"), 0)
    healthy = -agg.get(("hatch", "harvest", "healthy"), 0)
    watch = -agg.get(("hatch", "harvest", "watch"), 0)
    hatch = _row(conn, "SELECT * FROM hatch_records WHERE batch_id=?", (batch_id,))
    review = _row(
        conn,
        "SELECT hr.*,u.display_name vet_name FROM health_reviews hr "
        "JOIN users u ON u.id=hr.vet_id WHERE hr.batch_id=? ORDER BY hr.id DESC LIMIT 1",
        (batch_id,),
    )
    return {
        "batch": dict(b),
        "set_total": set_total,
        "candle_losses": {"stopped": stopped, "cracked": cracked},
        "hatch_losses": {"weak": weak, "dead": dead},
        "healthy": healthy,
        "watch": watch,
        "balance": set_total - stopped - cracked - weak - dead - healthy - watch,
        "hatch_rate": round(healthy * 100.0 / set_total, 1) if set_total and hatch else None,
        "hatched": hatch is not None,
        "review": dict(review) if review else None,
        "storage_days": _storage_days(b["collected_date"], date.today()),
    }
