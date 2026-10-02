"""业务服务层：批次登记 / 入孵 / 照蛋 / 转盘 / 出雏 / 健康复核。

所有数量变更都经 quantity_ledger 落账，每步校验数量守恒；
转盘用 BEGIN IMMEDIATE + 乐观版本号 + 槽位唯一索引三重保证盘位独占。
"""
import sqlite3
from datetime import datetime, timedelta

from .adapters import SAFE_MIN_C, SET_TEMP_C, SimSignalAdapter, SimTempAdapter
from .errors import (ConflictError, ConservationError, DomainError, NotFound,
                     OverdueError, PermissionError)

MAX_STORAGE_DAYS = 7          # 种蛋储存上限，超期入孵需先有处置记录
VALID_ROLES = {'vet', 'operator', 'keeper'}


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _new_code(cur, table: str, prefix: str) -> str:
    n = cur.execute(f"SELECT COALESCE(MAX(id),0)+1 FROM {table}").fetchone()[0]
    return f"{prefix}{n:04d}"


# ---------------------------------------------------------------- 用户/权限

def require_user(conn, user_id: int, roles=None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if row is None:
        raise NotFound("操作员不存在或已停用")
    if roles and row['role'] not in roles:
        names = {'vet': '兽医', 'operator': '操作员', 'keeper': '饲养员'}
        need = "、".join(names[r] for r in roles)
        raise PermissionError(f"{names[row['role']]}无权执行该操作，需要：{need}")
    return row


# ---------------------------------------------------------------- 登记

def register_batch(conn, flock_id: int, collected_at: str, storage_temp_c: float,
                   egg_count: int, operator_id: int, notes: str = "") -> dict:
    require_user(conn, operator_id, {'operator', 'vet'})
    flock = conn.execute("SELECT * FROM flocks WHERE id=?", (flock_id,)).fetchone()
    if flock is None:
        raise NotFound("来源鸡群不存在")
    if egg_count <= 0:
        raise DomainError("登记数量必须为正")
    t = now()
    cur = conn.execute(
        """INSERT INTO egg_batches(code, flock_id, collected_at, storage_temp_c,
           registered_egg_count, created_at, notes)
           VALUES (?,?,?,?,?,?,?)""",
        (_new_code(conn.cursor(), 'egg_batches', 'EP'), flock_id, collected_at,
         storage_temp_c, egg_count, t.isoformat(), notes))
    return get_batch(conn, cur.lastrowid)


def storage_age_days(conn, batch: sqlite3.Row, at: datetime) -> float:
    return (at.date() - _parse_dt(batch['collected_at']).date()).days


def has_handled_incident(conn, batch_id: int, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM incidents WHERE batch_id=? AND kind=? AND status='handled' LIMIT 1",
        (batch_id, kind)).fetchone()
    return row is not None


# ---------------------------------------------------------------- 入孵

def _slot(conn, incubator_id: int, layer_no: int, slot_no: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM rack_slots WHERE incubator_id=? AND layer_no=? AND slot_no=?",
        (incubator_id, layer_no, slot_no)).fetchone()
    if row is None:
        raise NotFound(f"该孵化箱没有 {layer_no}层{slot_no}号位")
    return row


def set_batch(conn, batch_id: int, incubator_id: int, assignments: list[dict],
              operator_id: int) -> dict:
    """assignments: [{layer_no, slot_no, egg_count}]，核对总数=登记数。"""
    require_user(conn, operator_id, {'operator', 'vet'})
    batch = get_batch(conn, batch_id)
    if batch['status'] != 'registered':
        raise DomainError("只有已登记批次可以入孵")
    inc = conn.execute("SELECT * FROM incubators WHERE id=?", (incubator_id,)).fetchone()
    if inc is None:
        raise NotFound("孵化箱不存在")
    if inc['status'] == 'stopped':
        raise ConflictError(f"孵化箱 {inc['code']} 处于停机状态，不能入孵")

    total = sum(int(a['egg_count']) for a in assignments)
    if total != batch['registered_egg_count']:
        raise ConservationError(
            f"入孵数量对不上：登记 {batch['registered_egg_count']} 枚，托盘合计 {total} 枚")
    if any(int(a['egg_count']) <= 0 for a in assignments):
        raise DomainError("每盘数量必须为正")

    # 储存超期阻断：除非已有处置完成记录
    age = storage_age_days(conn, batch, now())
    if age > MAX_STORAGE_DAYS and not has_handled_incident(conn, batch_id, 'storage_overdue'):
        raise OverdueError(
            f"种蛋已储存 {age} 天（上限 {MAX_STORAGE_DAYS} 天），请先登记并完成超期处置")

    conn.execute("BEGIN IMMEDIATE")
    try:
        t = now()
        # 混放来源检测：该箱已有其他鸡群的蛋
        other = conn.execute(
            """SELECT b.flock_id FROM tray_positions p
               JOIN egg_batches b ON b.id=p.batch_id
               JOIN rack_slots s ON s.id=p.slot_id
               WHERE s.incubator_id=? AND p.status='active' AND b.flock_id<>?
               LIMIT 1""",
            (incubator_id, batch['flock_id'])).fetchone()
        mixed_incident = None
        if other is not None:
            mixed_incident = _open_incident(
                conn, 'mixed_source', batch_id, incubator_id,
                "来源混放预警",
                f"批次 {batch['code']} 与其他鸡群种蛋同箱孵化，待隔离标识与处置。",
                operator_id, t)

        for a in assignments:
            slot = _slot(conn, incubator_id, int(a['layer_no']), int(a['slot_no']))
            occ = conn.execute(
                "SELECT tray_id FROM tray_positions WHERE slot_id=? AND status='active'",
                (slot['id'],)).fetchone()
            if occ is not None:
                raise ConflictError(f"{inc['code']} {a['layer_no']}层{a['slot_no']}号位已被托盘占用")
            cur = conn.execute("INSERT INTO trays(code, created_at) VALUES (?,?)",
                               (_new_code(conn.cursor(), 'trays', 'T'), t.isoformat()))
            tray_id = cur.lastrowid
            conn.execute(
                """INSERT INTO tray_positions(tray_id,batch_id,slot_id,egg_count,updated_at)
                   VALUES (?,?,?,?,?)""",
                (tray_id, batch_id, slot['id'], int(a['egg_count']), t.isoformat()))
            conn.execute(
                """INSERT INTO tray_movements(batch_id,tray_id,from_slot_id,to_slot_id,
                   reason,incident_id,operator_id,created_at)
                   VALUES (?,?,NULL,?,'set_in',?,?,?)""",
                (batch_id, tray_id, slot['id'], mixed_incident, operator_id, t.isoformat()))
            conn.execute(
                """INSERT INTO quantity_ledger(batch_id,stage,entry_type,delta,tray_id,
                   operator_id,created_at,note)
                   VALUES (?,'set','set_in',?,?,?,?,?)""",
                (batch_id, int(a['egg_count']), tray_id, operator_id, t.isoformat(), "入孵上架"))
        conn.execute(
            "UPDATE egg_batches SET status='incubating', incubator_id=?, set_at=? WHERE id=?",
            (incubator_id, t.isoformat(), batch_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_batch(conn, batch_id)


def _open_incident(conn, kind, batch_id, incubator_id, title, desc, reporter_id, t):
    cur = conn.execute(
        """INSERT INTO incidents(code,kind,batch_id,incubator_id,title,description,
           reporter_id,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (_new_code(conn.cursor(), 'incidents', 'IC'), kind, batch_id, incubator_id,
         title, desc, reporter_id, t.isoformat()))
    return cur.lastrowid


# ---------------------------------------------------------------- 照蛋

def add_candling(conn, batch_id: int, tray_id: int, counts: dict,
                 operator_id: int, candled_at: str | None = None, note: str = "") -> dict:
    require_user(conn, operator_id, {'operator', 'vet'})
    batch = get_batch(conn, batch_id)
    if batch['status'] not in ('incubating', 'candled'):
        raise DomainError("批次尚未入孵，不能照蛋")
    pos = conn.execute(
        "SELECT * FROM tray_positions WHERE tray_id=? AND batch_id=? AND status='active'",
        (tray_id, batch_id)).fetchone()
    if pos is None:
        raise NotFound("该托盘不属于本批次或已下架")

    fertile, infertile, stopped, broken = (int(counts.get(k, 0)) for k in
                                           ('fertile', 'infertile', 'stopped', 'broken'))
    if min(fertile, infertile, stopped, broken) < 0:
        raise DomainError("照蛋数量不能为负")
    total = fertile + infertile + stopped + broken
    if total != pos['egg_count']:
        raise ConservationError(
            f"照蛋数量对不上：在盘 {pos['egg_count']} 枚，"
            f"受精{fertile}+无精{infertile}+停育{stopped}+破损{broken}={total}")

    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        dup = conn.execute(
            "SELECT 1 FROM candling_records WHERE batch_id=? AND tray_id=?",
            (batch_id, tray_id)).fetchone()
        if dup:
            raise DomainError("该盘已照蛋，流水不允许覆盖；如有误请走异常处置")
        cur = conn.execute(
            """INSERT INTO candling_records(batch_id,tray_id,candled_at,operator_id,
               egg_count,fertile,infertile,stopped,broken,note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, tray_id, candled_at or t.isoformat(), operator_id,
             total, fertile, infertile, stopped, broken, note))
        cid = cur.lastrowid
        for etype, qty, lbl in (('candle_infertile', infertile, '无精'),
                                ('candle_stopped', stopped, '停育'),
                                ('candle_broken', broken, '破损')):
            if qty:
                conn.execute(
                    """INSERT INTO quantity_ledger(batch_id,stage,entry_type,delta,tray_id,
                       operator_id,created_at,note)
                       VALUES (?,'candle',?,?,?,?,?,?)""",
                    (batch_id, etype, -qty, tray_id, operator_id, t.isoformat(), f"照蛋{lbl}"))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": cid, "tray_id": tray_id, "fertile": fertile,
            "infertile": infertile, "stopped": stopped, "broken": broken}


def missed_trays(conn, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.tray_id, t.code FROM tray_positions p JOIN trays t ON t.id=p.tray_id
           WHERE p.batch_id=? AND p.status='active'
             AND NOT EXISTS (SELECT 1 FROM candling_records c
                             WHERE c.tray_id=p.tray_id AND c.batch_id=p.batch_id)""",
        (batch_id,)).fetchall()


def finalize_candling(conn, batch_id: int, operator_id: int) -> dict:
    """照蛋收口：漏盘则阻断并开漏盘处置单。"""
    require_user(conn, operator_id, {'operator', 'vet'})
    batch = get_batch(conn, batch_id)
    if batch['status'] != 'incubating':
        raise DomainError("只有入孵中的批次可以收口照蛋")
    missed = missed_trays(conn, batch_id)
    if missed:
        t = now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            _open_incident(
                conn, 'missed_tray', batch_id, batch['incubator_id'],
                "照蛋漏盘",
                "漏盘：" + "、".join(r['code'] for r in missed) +
                "。须补照或核对实物位置后再收口。",
                operator_id, t)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        raise ConservationError(
            f"照蛋漏盘 {len(missed)} 个（{'、'.join(r['code'] for r in missed)}），"
            "已开处置记录，补照齐全后再收口")
    conn.execute("UPDATE egg_batches SET status='candled' WHERE id=?", (batch_id,))
    return get_batch(conn, batch_id)


# ---------------------------------------------------------------- 转盘

def move_tray(conn, tray_id: int, to_incubator_id: int, layer_no: int, slot_no: int,
              operator_id: int, reason: str = 'transfer',
              incident_id: int | None = None, expected_updated_at: str | None = None) -> dict:
    """转盘/疏散。并发安全：BEGIN IMMEDIATE 串行化 + 行版本号 + 槽位唯一索引。"""
    require_user(conn, operator_id, {'operator', 'vet'})
    if reason not in ('transfer', 'evacuation', 'position_correct', 'hatch_offload'):
        raise DomainError("未知转盘原因")
    target = conn.execute("SELECT * FROM incubators WHERE id=?", (to_incubator_id,)).fetchone()
    if target is None:
        raise NotFound("目标孵化箱不存在")
    if target['status'] == 'stopped' and reason != 'position_correct':
        raise ConflictError(f"孵化箱 {target['code']} 已停机，不能转入（请疏散到运行中的机器）")

    conn.execute("BEGIN IMMEDIATE")
    try:
        pos = conn.execute("SELECT * FROM tray_positions WHERE tray_id=?", (tray_id,)).fetchone()
        if pos is None:
            raise NotFound("托盘不存在")
        if pos['status'] != 'active':
            raise ConflictError("托盘已下架，不能转盘")
        if expected_updated_at and pos['updated_at'] != expected_updated_at:
            raise ConflictError("该托盘刚被另一名操作员移动，盘位已变化，请刷新后重试")

        dest = _slot(conn, to_incubator_id, int(layer_no), int(slot_no))
        occ = conn.execute(
            "SELECT tray_id FROM tray_positions WHERE slot_id=? AND status='active' AND tray_id<>?",
            (dest['id'], tray_id)).fetchone()
        if occ is not None:
            raise ConflictError(
                f"{target['code']} {layer_no}层{slot_no}号位已被托盘 T{occ['tray_id']:04d} 占用")

        t = now().isoformat()
        conn.execute("UPDATE tray_positions SET slot_id=?, updated_at=? WHERE tray_id=?",
                     (dest['id'], t, tray_id))
        conn.execute(
            """INSERT INTO tray_movements(batch_id,tray_id,from_slot_id,to_slot_id,reason,
               incident_id,operator_id,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (pos['batch_id'], tray_id, pos['slot_id'], dest['id'], reason,
             incident_id, operator_id, t))
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as e:
        conn.execute("ROLLBACK")
        raise ConflictError(f"盘位冲突，转盘被拒绝：{e}")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_tray_position(conn, tray_id)


def offload_batch(conn, batch_id: int, operator_id: int, reason_note: str = "出雏下架"):
    conn.execute("BEGIN IMMEDIATE")
    try:
        _offload_batch_locked(conn, batch_id, operator_id, now().isoformat())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _offload_batch_locked(conn, batch_id: int, operator_id: int, t: str):
    rows = conn.execute(
        "SELECT tray_id, slot_id FROM tray_positions WHERE batch_id=? AND status='active'",
        (batch_id,)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE tray_positions SET slot_id=NULL,status='removed',updated_at=? WHERE tray_id=?",
            (t, r['tray_id']))
        conn.execute(
            """INSERT INTO tray_movements(batch_id,tray_id,from_slot_id,to_slot_id,reason,
               incident_id,operator_id,created_at)
               VALUES (?,?,?,NULL,'hatch_offload',NULL,?,?)""",
            (batch_id, r['tray_id'], r['slot_id'], operator_id, t))


# ---------------------------------------------------------------- 异常处置

def report_shift(conn, tray_id: int, actual_incubator_id: int, layer_no: int, slot_no: int,
                 operator_id: int, description: str) -> dict:
    """托盘移位：实物与账不符，按实际位置纠正并留处置记录。"""
    pos = get_tray_position(conn, tray_id)
    batch_id = pos['batch_id']
    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        iid = _open_incident(conn, 'tray_shift', batch_id, actual_incubator_id,
                             "托盘移位核对", description, operator_id, t)
        conn.execute("UPDATE incidents SET status='handled', handler_id=?, handled_at=?, action=? "
                     "WHERE id=?",
                     (operator_id, t.isoformat(),
                      f"按实物位置纠正到 {layer_no}层{slot_no}号位", iid))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    move_tray(conn, tray_id, actual_incubator_id, layer_no, slot_no,
              operator_id, reason='position_correct', incident_id=iid)
    return {"incident_id": iid}


def open_incident_api(conn, kind: str, batch_id, incubator_id, title, description,
                      reporter_id) -> int:
    require_user(conn, reporter_id, {'operator', 'vet'})
    if kind not in ('mixed_source', 'storage_overdue', 'incubator_stop',
                    'missed_tray', 'tray_shift', 'late_hatch'):
        raise DomainError("未知异常类型")
    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        iid = _open_incident(conn, kind, batch_id, incubator_id, title, description,
                             reporter_id, t)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return iid


def handle_incident(conn, incident_id: int, handler_id: int, action: str):
    require_user(conn, handler_id, {'operator', 'vet'})
    if not action.strip():
        raise DomainError("处置措施不能为空")
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if row is None:
        raise NotFound("处置单不存在")
    conn.execute("UPDATE incidents SET status='handled', handler_id=?, handled_at=?, action=? "
                 "WHERE id=?", (handler_id, now().isoformat(), action, incident_id))


# ---------------------------------------------------------------- 停机（信号/温度模拟）

def stop_incubator(conn, incubator_id: int, operator_id: int, detail: str = "") -> dict:
    require_user(conn, operator_id, {'operator', 'vet'})
    inc = conn.execute("SELECT * FROM incubators WHERE id=?", (incubator_id,)).fetchone()
    if inc is None:
        raise NotFound("孵化箱不存在")
    if inc['status'] == 'stopped':
        raise ConflictError("已处于停机状态")
    sig = SimSignalAdapter()
    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        iid = _open_incident(conn, 'incubator_stop', None, incubator_id,
                             f"{inc['code']} 中途停机", detail or "信号丢失，供电中断",
                             operator_id, t)
        cur = conn.execute(
            "INSERT INTO stop_events(incubator_id,incident_id,started_at,detail) VALUES (?,?,?,?)",
            (incubator_id, iid, t.isoformat(), detail))
        conn.execute("UPDATE incubators SET status='stopped' WHERE id=?", (incubator_id,))
        evt = sig.emit(inc['code'], 'power_off', t, detail)
        conn.execute(
            "INSERT INTO signal_events(incubator_id,kind,occurred_at,detail) VALUES (?,?,?,?)",
            (incubator_id, evt['kind'], evt['occurred_at'], evt['detail']))
        # 停机期间温度曲线（每 1 分钟一个点，持续 10 分钟供展示）
        adapter = SimTempAdapter(stop_started_at=t)
        for k in range(11):
            tt = t + timedelta(minutes=k)
            conn.execute("INSERT INTO temp_readings(incubator_id,temp_c,read_at) VALUES (?,?,?)",
                         (incubator_id, adapter.temp_at(tt), tt.isoformat()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"incident_id": _incident_id(conn, incubator_id), "stop_event_id": cur.lastrowid}


def resume_incubator(conn, incubator_id: int, operator_id: int,
                     evacuation_action: str | None = None) -> dict:
    require_user(conn, operator_id, {'operator', 'vet'})
    inc = conn.execute("SELECT * FROM incubators WHERE id=?", (incubator_id,)).fetchone()
    if inc['status'] != 'stopped':
        raise ConflictError("孵化箱当前不在停机状态")
    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        se = conn.execute(
            "SELECT * FROM stop_events WHERE incubator_id=? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (incubator_id,)).fetchone()
        conn.execute("UPDATE stop_events SET ended_at=? WHERE id=?", (t.isoformat(), se['id']))
        conn.execute("UPDATE incubators SET status='running' WHERE id=?", (incubator_id,))
        sig = SimSignalAdapter()
        evt = sig.emit(inc['code'], 'power_on', t, "供电恢复")
        conn.execute(
            "INSERT INTO signal_events(incubator_id,kind,occurred_at,detail) VALUES (?,?,?,?)",
            (incubator_id, evt['kind'], evt['occurred_at'], evt['detail']))
        started = _parse_dt(se['started_at'])
        adapter = SimTempAdapter(started, t)
        for k in range(1, 7):
            tt = t + timedelta(minutes=k)
            conn.execute("INSERT INTO temp_readings(incubator_id,temp_c,read_at) VALUES (?,?,?)",
                         (incubator_id, adapter.temp_at(tt), tt.isoformat()))
        # 疏散处置：把停机时仍在箱内的托盘写明处置措施并关单
        if evacuation_action:
            conn.execute(
                "UPDATE incidents SET status='handled', handler_id=?, handled_at=?, action=? "
                "WHERE id=?", (operator_id, t.isoformat(), evacuation_action, se['incident_id']))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True}


def _incident_id(conn, incubator_id):
    r = conn.execute(
        "SELECT id FROM incidents WHERE incubator_id=? AND kind='incubator_stop' "
        "ORDER BY id DESC LIMIT 1", (incubator_id,)).fetchone()
    return r['id'] if r else None


# ---------------------------------------------------------------- 出雏

def hatch_batch(conn, batch_id: int, healthy: int, observe: int, late: int,
                operator_id: int, note: str = "") -> dict:
    require_user(conn, operator_id, {'operator', 'vet'})
    batch = get_batch(conn, batch_id)
    if batch['status'] != 'candled':
        raise DomainError("只有照蛋收口后的批次可以统计出雏")
    if min(healthy, observe, late) < 0:
        raise DomainError("数量不能为负")
    bal = ledger_balance(conn, batch_id)
    remaining = bal['remaining']
    total = healthy + observe + late
    if total != remaining:
        raise ConservationError(
            f"出雏数量对不上：照蛋后在孵 {remaining} 枚，"
            f"健康{healthy}+待观察{observe}+破壳延期{late}={total}")
    if late > 0 and not has_handled_incident(conn, batch_id, 'late_hatch'):
        raise DomainError(f"有 {late} 枚破壳延期，须先登记并完成延期处置记录")

    t = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """INSERT INTO hatch_records(batch_id,hatched_at,operator_id,healthy,observe,late,note)
               VALUES (?,?,?,?,?,?,?)""",
            (batch_id, t.isoformat(), operator_id, healthy, observe, late, note))
        for etype, qty, lbl in (('hatch_healthy', healthy, '健康雏鸡'),
                                ('hatch_observe', observe, '待观察个体')):
            if qty:
                conn.execute(
                    """INSERT INTO quantity_ledger(batch_id,stage,entry_type,delta,tray_id,
                       operator_id,created_at,note)
                       VALUES (?,'hatch',?,?,NULL,?,?,?)""",
                    (batch_id, etype, -qty, operator_id, t.isoformat(), f"出雏{lbl}"))
        offload = conn.execute(
            "SELECT COUNT(*) c FROM tray_positions WHERE batch_id=? AND status='active'",
            (batch_id,)).fetchone()['c']
        if offload:
            _offload_batch_locked(conn, batch_id, operator_id, t.isoformat())
        conn.execute("UPDATE egg_batches SET status='hatched' WHERE id=?", (batch_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"id": cur.lastrowid, "healthy": healthy, "observe": observe, "late": late}


# ---------------------------------------------------------------- 健康复核（兽医签署）

def health_review(conn, batch_id: int, user_id: int, conclusion: str, note: str = "") -> dict:
    # 仅兽医可签；饲养员/操作员显式拒绝，不能代签
    user = require_user(conn, user_id, {'vet'})
    batch = get_batch(conn, batch_id)
    if batch['status'] not in ('hatched', 'reviewed'):
        raise DomainError("出雏统计完成后才能进行健康复核")
    if conclusion not in ('pass', 'observe', 'fail'):
        raise DomainError("结论必须是 pass / observe / fail")
    t = now()
    existing = conn.execute("SELECT id FROM health_reviews WHERE batch_id=?", (batch_id,)).fetchone()
    if existing:
        raise DomainError("兽医已签署，结论不可更改（如需变更请新开复核流程）")
    cur = conn.execute(
        "INSERT INTO health_reviews(batch_id,vet_id,conclusion,note,signed_at) VALUES (?,?,?,?,?)",
        (batch_id, user['id'], conclusion, note, t.isoformat()))
    conn.execute("UPDATE egg_batches SET status='reviewed' WHERE id=?", (batch_id,))
    return {"id": cur.lastrowid, "vet": user['name'], "conclusion": conclusion}


# ---------------------------------------------------------------- 查询

def get_batch(conn, batch_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT b.*, f.code AS flock_code, f.name AS flock_name,
                  i.code AS incubator_code
           FROM egg_batches b JOIN flocks f ON f.id=b.flock_id
           LEFT JOIN incubators i ON i.id=b.incubator_id
           WHERE b.id=?""", (batch_id,)).fetchone()
    if row is None:
        raise NotFound("批次不存在")
    return row


def get_tray_position(conn, tray_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT p.*, s.incubator_id, s.layer_no, s.slot_no, i.code AS incubator_code,
                  t.code AS tray_code, b.code AS batch_code
           FROM tray_positions p
           JOIN trays t ON t.id=p.tray_id
           JOIN egg_batches b ON b.id=p.batch_id
           LEFT JOIN rack_slots s ON s.id=p.slot_id
           LEFT JOIN incubators i ON i.id=s.incubator_id
           WHERE p.tray_id=?""", (tray_id,)).fetchone()
    if row is None:
        raise NotFound("托盘不存在")
    return row


def ledger_balance(conn, batch_id: int) -> dict:
    """汇总各阶段数量：set_in - 各项损耗，校验账平。"""
    rows = conn.execute(
        "SELECT entry_type, SUM(delta) AS d FROM quantity_ledger WHERE batch_id=? GROUP BY entry_type",
        (batch_id,)).fetchall()
    agg = {r['entry_type']: r['d'] for r in rows}
    set_in = agg.get('set_in', 0)
    infertile = -agg.get('candle_infertile', 0)
    stopped = -agg.get('candle_stopped', 0)
    broken = -agg.get('candle_broken', 0)
    healthy = -agg.get('hatch_healthy', 0)
    observe = -agg.get('hatch_observe', 0)
    candle_loss = infertile + stopped + broken
    hatch_out = healthy + observe
    return {
        "set_in": set_in,
        "infertile": infertile, "stopped": stopped, "broken": broken,
        "candle_loss": candle_loss,
        "after_candle": set_in - candle_loss,
        "healthy": healthy, "observe": observe, "hatch_out": hatch_out,
        "remaining": set_in - candle_loss - hatch_out,
        "balanced": set_in - candle_loss - hatch_out >= 0,
    }


def batch_detail(conn, batch_id: int) -> dict:
    batch = dict(get_batch(conn, batch_id))
    bal = ledger_balance(conn, batch_id)
    trays = [dict(r) for r in conn.execute(
        """SELECT p.tray_id,t.code AS tray_code,p.egg_count,p.status,s.layer_no,s.slot_no,
                  i.code AS incubator_code,
                  (SELECT fertile FROM candling_records c WHERE c.tray_id=p.tray_id) AS fertile,
                  (SELECT infertile FROM candling_records c WHERE c.tray_id=p.tray_id) AS infertile,
                  (SELECT stopped FROM candling_records c WHERE c.tray_id=p.tray_id) AS stopped,
                  (SELECT broken FROM candling_records c WHERE c.tray_id=p.tray_id) AS broken
           FROM tray_positions p JOIN trays t ON t.id=p.tray_id
           LEFT JOIN rack_slots s ON s.id=p.slot_id
           LEFT JOIN incubators i ON i.id=s.incubator_id
           WHERE p.batch_id=? ORDER BY p.tray_id""", (batch_id,)).fetchall()]
    ledger = [dict(r) for r in conn.execute(
        """SELECT l.*, u.name AS operator_name FROM quantity_ledger l
           JOIN users u ON u.id=l.operator_id WHERE l.batch_id=? ORDER BY l.id""",
        (batch_id,)).fetchall()]
    candles = [dict(r) for r in conn.execute(
        """SELECT c.*, u.name AS operator_name, t.code AS tray_code FROM candling_records c
           JOIN users u ON u.id=c.operator_id JOIN trays t ON t.id=c.tray_id
           WHERE c.batch_id=? ORDER BY c.id""", (batch_id,)).fetchall()]
    for c in candles:
        c['photos'] = [dict(r) for r in conn.execute(
            "SELECT * FROM candling_photos WHERE candling_id=? ORDER BY id", (c['id'],)).fetchall()]
    hatch = conn.execute("SELECT * FROM hatch_records WHERE batch_id=?", (batch_id,)).fetchone()
    review = conn.execute(
        """SELECT r.*, u.name AS vet_name FROM health_reviews r
           JOIN users u ON u.id=r.vet_id WHERE r.batch_id=?""", (batch_id,)).fetchone()
    incidents = [dict(r) for r in conn.execute(
        """SELECT n.*, u1.name AS reporter_name, u2.name AS handler_name FROM incidents n
           LEFT JOIN users u1 ON u1.id=n.reporter_id
           LEFT JOIN users u2 ON u2.id=n.handler_id
           WHERE n.batch_id=? ORDER BY n.id""", (batch_id,)).fetchall()]
    movements = [dict(r) for r in conn.execute(
        """SELECT m.*, t.code AS tray_code, u.name AS operator_name,
                  si.layer_no AS fl_layer, si.slot_no AS fl_slot, ic1.code AS from_inc,
                  st.layer_no AS tl_layer, st.slot_no AS tl_slot, ic2.code AS to_inc
           FROM tray_movements m JOIN trays t ON t.id=m.tray_id JOIN users u ON u.id=m.operator_id
           LEFT JOIN rack_slots si ON si.id=m.from_slot_id
           LEFT JOIN incubators ic1 ON ic1.id=si.incubator_id
           LEFT JOIN rack_slots st ON st.id=m.to_slot_id
           LEFT JOIN incubators ic2 ON ic2.id=st.incubator_id
           WHERE m.batch_id=? ORDER BY m.id""", (batch_id,)).fetchall()]
    rate = round(bal['healthy'] / bal['set_in'] * 100, 1) if bal['set_in'] else None
    return {"batch": batch, "balance": bal, "trays": trays, "ledger": ledger,
            "candles": candles, "hatch": dict(hatch) if hatch else None,
            "review": dict(review) if review else None,
            "incidents": incidents, "movements": movements, "hatch_rate_pct": rate,
            "storage_age_days": storage_age_days(conn, get_batch(conn, batch_id), now()),
            "max_storage_days": MAX_STORAGE_DAYS}
