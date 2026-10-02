"""演示数据：用户、孵化箱/盘位、多个批次与全量异常处置样例。

重点样例：
- B-STOP  批次经历孵化箱 IQ-02 中途停机 → 异常处置 → 转盘 → 签收 → 恢复；
- B-OLD   储存超期批次（未处置时再次入孵会被阻断）；
- B-MIX   混放来源、托盘移位、照蛋漏盘、破壳延期各一例；
- B-DONE  已完成出雏并由兽医签署健康结论。
照蛋照片由本地生成的 PNG 提供（深色底上蛋体亮斑，可放大比对）。
"""
from __future__ import annotations

import sqlite3
import struct
import zlib
from datetime import date, timedelta
from pathlib import Path

from . import services

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads" / "photos"

# 固定操作员（角色权限演示用）
USERS = [
    ("zhang", "张师傅", "operator"),
    ("li", "李操作", "operator"),
    ("wang", "王饲养", "keeper"),
    ("chen", "陈兽医", "vet"),
]


def _png(path: Path, w: int, h: int, pixels: list[list[tuple]]):
    """无依赖写一张 PNG（RGB）。pixels: 行优先三元组。"""
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
        chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _candling_image(path: Path, label: str, variant: str):
    """合成照蛋图：黑底 + 6x4 枚蛋的亮影，停育/破损用暗红标记。"""
    w = h = 480
    bg = (24, 26, 25)
    px = [[bg for _ in range(w)] for _ in range(h)]
    import random
    rnd = random.Random(hash(label) & 0xffff)
    cols, rows = 6, 4
    for r in range(rows):
        for c in range(cols):
            cx, cy = 48 + c * 77, 60 + r * 105
            state = "fertile"
            if variant == "stopped" and (r, c) in {(1, 2), (2, 4)}:
                state = "stopped"
            if variant == "cracked" and (r, c) == (0, 3):
                state = "cracked"
            for dy in range(-26, 27):
                for dx in range(-22, 23):
                    if dx * dx / 484 + dy * dy / 676 > 1:
                        continue
                    x, y = cx + dx, cy + dy
                    if not (0 <= x < w and 0 <= y < h):
                        continue
                    j = rnd.randint(-8, 8)
                    if state == "fertile":
                        px[y][x] = (max(0, 214 + j), min(255, 196 + j), 120 + j // 2)
                    elif state == "stopped":  # 停育：暗、无血管网
                        px[y][x] = (96 + j, 60, 48)
                    else:                      # 破损：裂纹状亮线
                        edge = abs(dx) > 16 or abs(dy) > 22
                        px[y][x] = (200 + j, 170 + j, 110) if not edge else (235, 230, 210)
    _png(path, w, h, px)


def seed_all(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]:
        return  # 已初始化，幂等

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for u in USERS:
        conn.execute(
            "INSERT INTO users(username,display_name,role) VALUES(?,?,?)", u)
    uid = {n: conn.execute("SELECT id FROM users WHERE username=?", (n,)).fetchone()["id"]
           for n, *_ in USERS}
    OP, OP2, KEEPER, VET = uid["zhang"], uid["li"], uid["wang"], uid["chen"]

    # 两台孵化箱：3 层 × 4 位
    for code, name in (("IQ-01", "1号孵化箱"), ("IQ-02", "2号孵化箱")):
        cur = conn.execute("INSERT INTO incubators(code,name,shelf_rows,shelf_cols) VALUES(?,?,3,4)",
                           (code, name))
        inc = cur.lastrowid
        for s in range(1, 4):
            for p in range(1, 5):
                conn.execute("INSERT INTO slots(incubator_id,shelf_no,pos_no) VALUES(?,?,?)",
                             (inc, s, p))
    conn.execute("INSERT INTO incubator_events(incubator_id,event,note) VALUES(1,'running','上电')")
    conn.execute("INSERT INTO incubator_events(incubator_id,event,note) VALUES(2,'running','上电')")

    today = date.today()

    # ---- 批次1：正常在孵（部分照蛋完成）----
    b1 = services.create_batch(
        conn, code="B2026-NORMAL", source_flock="A栋·岭南黄鸡群",
        collected_date=(today - timedelta(days=3)).isoformat(),
        storage_temp_c=16.5, operator_id=OP)
    services.place_trays(conn, batch_id=b1, operator_id=OP, placements=[
        {"tray": "T-N1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 120},
        {"tray": "T-N2", "incubator": "IQ-01", "shelf": 1, "pos": 2, "eggs": 120},
    ])
    _candling_image(UPLOAD_DIR / "seed_n1.png", "T-N1", "fertile")
    services.candle_tray(conn, tray_code="T-N1", fertile=112, stopped=6, cracked=2,
                         operator_id=OP, note="照蛋正常，血管网清晰",
                         photo_paths=[("photos/seed_n1.png", "T-N1 照蛋（停育6 破损2）")])

    # ---- 批次2：停机样例（核心演示链路）----
    b2 = services.create_batch(
        conn, code="B2026-STOP", source_flock="B栋·清远麻鸡群",
        collected_date=(today - timedelta(days=5)).isoformat(),
        storage_temp_c=17.0, operator_id=OP2)
    services.place_trays(conn, batch_id=b2, operator_id=OP2, placements=[
        {"tray": "T-S1", "incubator": "IQ-02", "shelf": 2, "pos": 1, "eggs": 120},
        {"tray": "T-S2", "incubator": "IQ-02", "shelf": 2, "pos": 2, "eggs": 120},
        {"tray": "T-S3", "incubator": "IQ-02", "shelf": 2, "pos": 3, "eggs": 118},
    ])
    # 第 8 天 IQ-02 中途停机（模拟信号适配器触发）
    stop_incident = services.report_incubator_stop(
        conn, incubator_id=2, note="第8天夜间停电约40分钟，箱温降至33.1℃",
        operator_id=OP, batch_id=b2)
    # 处置：蛋盘转移到 IQ-01 空位，逐盘检查胚蛋
    ts3 = conn.execute("SELECT id FROM trays WHERE code='T-S3'").fetchone()["id"]
    shift = services.create_incident(
        conn, type_="tray_shift", batch_id=b2, tray_id=ts3, incubator_id=2,
        description="停机处置过程中 T-S3 由 2-2-3 转移，需核对托盘实际位置防移位",
        disposition="双人核对托盘编号与层架位置，转盘全程留痕", operator_id=OP2)
    services.move_tray(conn, tray_code="T-S1", incubator_code="IQ-01",
                       shelf=2, pos=1, operator_id=OP, incident_id=stop_incident,
                       note="停机应急：转入1号箱")
    services.move_tray(conn, tray_code="T-S2", incubator_code="IQ-01",
                       shelf=2, pos=2, operator_id=OP2, incident_id=stop_incident,
                       note="停机应急：转入1号箱")
    services.move_tray(conn, tray_code="T-S3", incubator_code="IQ-01",
                       shelf=2, pos=3, operator_id=OP,
                       note="停机应急：转入1号箱（双人核对）")
    services.handle_incident(conn, shift, OP2, "已双人核对 T-S3 层架位置与托盘编号一致")
    services.handle_incident(conn, stop_incident, OP,
                             disposition="三盘全部转移至IQ-01并逐盘照检，未见新增异常；停电40分钟在可接受窗内")
    services.resume_incubator(conn, incubator_id=2, incident_id=stop_incident,
                              operator_id=OP, note="复电试运行正常，空箱恢复")
    _candling_image(UPLOAD_DIR / "seed_s1.png", "T-S1", "stopped")
    services.candle_tray(conn, tray_code="T-S1", fertile=104, stopped=13, cracked=3,
                         operator_id=OP, note="停机后停育偏高，持续跟踪",
                         photo_paths=[("photos/seed_s1.png", "T-S1 停机后照蛋：停育13")])
    services.candle_tray(conn, tray_code="T-S2", fertile=110, stopped=8, cracked=2,
                         operator_id=OP2)
    services.candle_tray(conn, tray_code="T-S3", fertile=109, stopped=7, cracked=2,
                         operator_id=OP)

    # ---- 批次3：储存超期（未处置，入孵应被阻断）----
    b3 = services.create_batch(
        conn, code="B2026-OLD", source_flock="C栋·竹丝鸡群",
        collected_date=(today - timedelta(days=10)).isoformat(),
        storage_temp_c=18.2, operator_id=OP)
    ov = services.create_incident(
        conn, type_="over_storage", batch_id=b3,
        description="收集后已储存10天，超过7天上限",
        disposition="拟降级处理：待主管与兽医会签后决定是否入孵", operator_id=OP)
    # 注意：保持 open 未签收 → 页面上对该批入孵会被阻断（测试覆盖）

    # ---- 批次4：混放来源 + 照蛋漏盘 + 破壳延期，已出雏、已签署 ----
    b4 = services.create_batch(
        conn, code="B2026-MIX", source_flock="A栋·岭南黄鸡群",
        collected_date=(today - timedelta(days=23)).isoformat(),
        storage_temp_c=16.0, operator_id=OP)
    services.place_trays(conn, batch_id=b4, operator_id=OP,
                         set_date=(today - timedelta(days=21)).isoformat(),
                         placements=[
        {"tray": "T-M1", "incubator": "IQ-02", "shelf": 3, "pos": 1, "eggs": 100},
        {"tray": "T-M2", "incubator": "IQ-02", "shelf": 3, "pos": 2, "eggs": 100},
    ])
    tm1 = conn.execute("SELECT id FROM trays WHERE code='T-M1'").fetchone()["id"]
    tm2 = conn.execute("SELECT id FROM trays WHERE code='T-M2'").fetchone()["id"]
    mix = services.create_incident(
        conn, type_="mixed_source", batch_id=b4, tray_id=tm2,
        description="T-M2 中发现3枚蛋来自D栋鸡群，与标签来源不符",
        disposition="3枚异源蛋做标记隔离，单独跟踪，不混入出雏统计", operator_id=VET)
    services.handle_incident(conn, mix, VET)
    _candling_image(UPLOAD_DIR / "seed_m2.png", "T-M2", "cracked")
    services.candle_tray(conn, tray_code="T-M2", fertile=92, stopped=6, cracked=2,
                         operator_id=OP,
                         photo_paths=[("photos/seed_m2.png", "T-M2 照蛋：破损2（异源蛋已隔离）")])
    # T-M1 漏盘：登记并签收后才允许出雏
    miss = services.create_incident(
        conn, type_="missed_tray", batch_id=b4, tray_id=tm1,
        description="照蛋当日 T-M1 漏检一整盘",
        disposition="出雏前补照：受精88 停育9 破损3，结果登记在案", operator_id=OP)
    services.handle_incident(conn, miss, OP)
    # 补照数量（守恒仍按流水：损耗照常入账）
    services.candle_tray(conn, tray_code="T-M1", fertile=88, stopped=9, cracked=3,
                         operator_id=OP2, note="漏盘补照")
    # 出雏：入孵200 − 照蛋损耗(6+2+9+3)=180；健康165 待观察7（含破壳延期4）弱4 死4
    services.record_hatch(conn, batch_id=b4, healthy=165, watch=7, weak=4, dead=4,
                          delayed=4, operator_id=OP)
    services.sign_health_review(
        conn, batch_id=b4, conclusion="conditional",
        note="健康雏165只合格；7只待观察（含4只破壳延期）24h复查；混放来源批次隔离发苗",
        user_id=VET)


if __name__ == "__main__":
    from .db import connect, init_db
    c = connect()
    init_db(c)
    seed_all(c)
    print("seed done")
