"""种子数据：一条贯穿全流程的样例（含孵化箱中途停机），另留两个可现场操作的批次。"""
import os
from datetime import date, datetime, timedelta

from . import db, service
from .adapters import SET_TEMP_C
from .photos import save_photo


def seed(conn, data_dir: str, reset: bool = False):
    if reset:
        conn.execute("PRAGMA foreign_keys = OFF")
        for t in ("quantity_ledger", "tray_movements", "tray_positions", "candling_photos",
                  "candling_records", "hatch_records", "health_reviews", "incidents",
                  "stop_events", "signal_events", "temp_readings", "egg_batches",
                  "trays", "rack_slots", "incubators", "flocks", "users"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("PRAGMA foreign_keys = ON")
        db.init_db(conn)

    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return False

    t0 = datetime.now().replace(microsecond=0)
    # ---- 人员：兽医 / 操作员 ×2 / 饲养员 ----
    users = {}
    for name, role in (("王芳", "vet"), ("李强", "operator"),
                       ("赵敏", "operator"), ("孙大壮", "keeper")):
        cur = conn.execute("INSERT INTO users(name,role) VALUES (?,?)", (name, role))
        users[role if role != "operator" else f"op{name}"] = cur.lastrowid
    VET, OP_LI, OP_ZHAO, KEEPER = (users["vet"], users["op李强"], users["op赵敏"], users["keeper"])

    # ---- 鸡群 ----
    f1 = conn.execute("INSERT INTO flocks(code,name,house) VALUES ('FL-A01','白羽肉鸡 A 群','A栋')").lastrowid
    f2 = conn.execute("INSERT INTO flocks(code,name,house) VALUES ('FL-B02','褐羽蛋鸡 B 群','B栋')").lastrowid

    # ---- 孵化箱：6 层 × 8 槽位 ----
    incs = {}
    for code, name in (("IQ-A", "1号孵化箱"), ("IQ-B", "2号孵化箱")):
        cur = conn.execute(
            "INSERT INTO incubators(code,name,status,layer_count,slot_count,created_at) "
            "VALUES (?,?,'running',6,8,?)", (code, name, t0.isoformat()))
        iid = cur.lastrowid
        incs[code] = iid
        conn.executemany(
            "INSERT INTO rack_slots(incubator_id,layer_no,slot_no) VALUES (?,?,?)",
            [(iid, L, S) for L in range(1, 7) for S in range(1, 9)])
    IQ_A, IQ_B = incs["IQ-A"], incs["IQ-B"]

    # ---- 批次1：13 天前收集（超期），600 枚 ----
    d13 = (date.today() - timedelta(days=13)).isoformat()
    b1 = service.register_batch(conn, f1, d13, 15.5, 600, OP_LI,
                                "种蛋批次样例：超期-入孵-停机疏散-出雏-签署")["id"]
    # 超期处置：登记并完成后才允许入孵
    iid = service.open_incident_api(conn, "storage_overdue", b1, None,
                                    "种蛋储存超期", "收集已 13 天，超过 7 天上限，评估活力。", OP_LI)
    service.handle_incident(conn, iid, OP_LI,
                            "抽检照视活力正常，提前入孵并标记，出雏率单独统计。")
    service.set_batch(conn, b1, IQ_A, [
        {"layer_no": 1, "slot_no": 1, "egg_count": 240},
        {"layer_no": 1, "slot_no": 2, "egg_count": 200},
        {"layer_no": 1, "slot_no": 3, "egg_count": 160},
    ], OP_LI)

    # ---- 孵化箱中途停机：温度下掉，3 盘疏散到 2号箱 ----
    service.stop_incubator(conn, IQ_A, OP_ZHAO,
                           "15:20 供电模块告警断电，启用模拟信号适配器记录")
    for tray_id, (L, S) in ((1, (1, 1)), (2, (1, 2)), (3, (1, 3))):
        service.move_tray(conn, tray_id, IQ_B, L, S, OP_ZHAO, reason="evacuation",
                          incident_id=service._incident_id(conn, IQ_A))
    service.resume_incubator(conn, IQ_A, OP_ZHAO,
                             "停机 10 分钟内将 3 盘全部疏散至 IQ-B 1层1-3号；"
                             "恢复供电后空机试运行，温度曲线已留档。")

    # ---- 照蛋（四分类守恒）----
    candle_plan = {
        1: (240, {"fertile": 210, "infertile": 12, "stopped": 12, "broken": 6}),
        2: (200, {"fertile": 170, "infertile": 14, "stopped": 10, "broken": 6}),
        3: (160, {"fertile": 138, "infertile": 10, "stopped": 8, "broken": 4}),
    }
    for tray_id, (_n, c) in candle_plan.items():
        r = service.add_candling(conn, b1, tray_id, c, OP_LI, note="照蛋留档")
        slot = conn.execute(
            "SELECT t.code FROM trays t WHERE t.id=?", (tray_id,)).fetchone()["code"]
        ts = t0.strftime("%Y-%m-%d")
        kinds = {1: ["overview", "fertile"], 2: ["overview", "stopped"],
                 3: ["overview", "broken"]}[tray_id]
        for kind in kinds:
            path = save_photo(data_dir, r["id"], kind, slot, ts)
            conn.execute("INSERT INTO candling_photos(candling_id,kind,path,created_at) "
                         "VALUES (?,?,?,?)", (r["id"], kind, path, t0.isoformat()))
    service.finalize_candling(conn, b1, OP_LI)

    # ---- 批次2：新鲜蛋 360 枚，入 IQ-B 与批次1同箱 → 自动开混放处置 ----
    d2 = (date.today() - timedelta(days=2)).isoformat()
    b2 = service.register_batch(conn, f2, d2, 16.0, 360, OP_ZHAO,
                                "样例：与批次1来源不同，同箱触发混放处置")["id"]
    service.set_batch(conn, b2, IQ_B, [
        {"layer_no": 2, "slot_no": 1, "egg_count": 190},
        {"layer_no": 2, "slot_no": 2, "egg_count": 170},
    ], OP_ZHAO)
    mixed = conn.execute(
        "SELECT id FROM incidents WHERE batch_id=? AND kind='mixed_source'", (b2,)).fetchone()["id"]
    service.handle_incident(conn, mixed, OP_LI,
                            "分层隔离：批次1占 1层、批次2占 2-4层，托盘加鸡群色标，转盘记录注明来源。")

    # ---- 托盘移位：T0005 实物在 4层5号，按实物纠正 ----
    service.report_shift(conn, 5, IQ_B, 4, 5, OP_ZHAO,
                         "巡检发现 T0005 与台账位置（2层2号）不符，查转盘记录后按实物定位。")

    # 批次2照蛋（未出雏，留给界面操作）
    r4 = service.add_candling(conn, b2, 4,
                              {"fertile": 166, "infertile": 12, "stopped": 8, "broken": 4},
                              OP_ZHAO)
    for kind in ("overview", "fertile"):
        path = save_photo(data_dir, r4["id"], kind, "T0004", t0.strftime("%Y-%m-%d"))
        conn.execute("INSERT INTO candling_photos(candling_id,kind,path,created_at) "
                     "VALUES (?,?,?,?)", (r4["id"], kind, path, t0.isoformat()))
    r5 = service.add_candling(conn, b2, 5,
                              {"fertile": 148, "infertile": 10, "stopped": 8, "broken": 4},
                              OP_ZHAO)
    for kind in ("overview", "infertile"):
        path = save_photo(data_dir, r5["id"], kind, "T0005", t0.strftime("%Y-%m-%d"))
        conn.execute("INSERT INTO candling_photos(candling_id,kind,path,created_at) "
                     "VALUES (?,?,?,?)", (r5["id"], kind, path, t0.isoformat()))
    service.finalize_candling(conn, b2, OP_ZHAO)

    # ---- 批次1出雏：486 健康 / 22 待观察 / 10 破壳延期（518=照蛋后存活）----
    late_i = service.open_incident_api(conn, "late_hatch", b1, IQ_B,
                                       "破壳延期", "10 枚破壳超 12 小时未出雏。", OP_LI)
    service.handle_incident(conn, late_i, OP_LI,
                            "转出雏盘延长观察 24 小时，保持湿度 70%，每 4 小时巡查并记录。")
    service.hatch_batch(conn, b1, 486, 22, 10, OP_LI, "健康雏鸡转育雏舍，22 只隔离观察。")
    service.health_review(conn, b1, VET, "pass",
                          "健康雏鸡 486 只精神良好，脐部闭合正常；同意转群。待观察 22 只继续隔离。")

    # ---- 批次3：超期未处置（界面入孵时会被阻断，用于演示拦截）----
    d10 = (date.today() - timedelta(days=10)).isoformat()
    service.register_batch(conn, f1, d10, 16.5, 240, OP_LI,
                           "演示用：储存 10 天且暂无处置记录，入孵应被阻断")

    # ---- 运行箱常态温度采样 ----
    for k in range(6):
        tt = (t0 - timedelta(minutes=30 - k * 5)).isoformat()
        conn.execute("INSERT INTO temp_readings(incubator_id,temp_c,read_at) VALUES (?,?,?)",
                     (IQ_B, SET_TEMP_C, tt))

    return True
