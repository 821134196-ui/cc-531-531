"""领域规则测试：数量守恒、盘位独占（并发转盘）、超期阻断、角色权限。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from hatchery import services
from hatchery.db import connect, init_db


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = connect(self.tmp.name)
        init_db(self.conn)
        for name, disp, role in (
            ("op1", "操作员甲", "operator"),
            ("op2", "操作员乙", "operator"),
            ("keeper", "饲养员丙", "keeper"),
            ("vet", "兽医丁", "vet"),
        ):
            self.conn.execute(
                "INSERT INTO users(username,display_name,role) VALUES(?,?,?)",
                (name, disp, role))
        self.OP1 = self.uid("op1"); self.OP2 = self.uid("op2")
        self.KEEPER = self.uid("keeper"); self.VET = self.uid("vet")
        for code in ("IQ-01", "IQ-02"):
            cur = self.conn.execute(
                "INSERT INTO incubators(code,name,shelf_rows,shelf_cols) VALUES(?,?,3,4)",
                (code, code))
            inc = cur.lastrowid
            for s in range(1, 4):
                for p in range(1, 5):
                    self.conn.execute(
                        "INSERT INTO slots(incubator_id,shelf_no,pos_no) VALUES(?,?,?)",
                        (inc, s, p))

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            Path(self.tmp.name + suffix).unlink(missing_ok=True)

    def uid(self, username):
        return self.conn.execute(
            "SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]

    def balance(self, batch_id, conn=None):
        return services._balance(conn or self.conn, batch_id)

    def make_batch(self, days_ago=2, code=None):
        return services.create_batch(
            self.conn, code=code, source_flock="测试鸡群",
            collected_date=(date.today() - timedelta(days=days_ago)).isoformat(),
            storage_temp_c=16.5, operator_id=self.OP1)


class TestQuantityConservation(LedgerTestBase):
    def _two_trays(self, days=2):
        b = self.make_batch(days)
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 100},
            {"tray": "T2", "incubator": "IQ-01", "shelf": 1, "pos": 2, "eggs": 80},
        ])
        return b

    def test_set_balance(self):
        b = self._two_trays()
        self.assertEqual(self.balance(b), 180)

    def test_candle_must_match_tray_count(self):
        b = self._two_trays()
        # 90+5+3=98 ≠ 100：对不上必须拒绝
        with self.assertRaises(services.DomainError):
            services.candle_tray(self.conn, tray_code="T1",
                                 fertile=90, stopped=5, cracked=3,
                                 operator_id=self.OP1)

    def test_candle_reduces_balance_by_loss_only(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=92, stopped=6, cracked=2, operator_id=self.OP1)
        self.assertEqual(self.balance(b), 172)  # 受精不是损耗，只减停育+破损

    def test_cannot_over_draw_loss(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=92, stopped=6, cracked=2, operator_id=self.OP1)
        with self.assertRaises(services.DomainError):
            services.candle_tray(self.conn, tray_code="T1",
                                 fertile=90, stopped=2, cracked=0, operator_id=self.OP1)

    def test_hatch_must_conserve(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=92, stopped=6, cracked=2, operator_id=self.OP1)
        services.candle_tray(self.conn, tray_code="T2",
                             fertile=74, stopped=4, cracked=2, operator_id=self.OP2)
        # 结余 166；错报 167 必须拒绝
        with self.assertRaises(services.DomainError):
            services.record_hatch(self.conn, batch_id=b, healthy=160, watch=3,
                                  weak=2, dead=2, operator_id=self.OP1)
        # 正确对账：153 + 5 + 4 + 4 = 166
        services.record_hatch(self.conn, batch_id=b, healthy=153, watch=5,
                              weak=4, dead=4, operator_id=self.OP1)
        self.assertEqual(self.balance(b), 0)  # 全量入账，余额归零

    def test_delayed_pip_counts_as_watch(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=100, stopped=0, cracked=0, operator_id=self.OP1)
        services.candle_tray(self.conn, tray_code="T2",
                             fertile=80, stopped=0, cracked=0, operator_id=self.OP1)
        with self.assertRaises(services.DomainError):
            services.record_hatch(self.conn, batch_id=b, healthy=175, watch=2,
                                  weak=0, dead=0, delayed=5, operator_id=self.OP1)

    def test_ledger_rows_are_never_deleted(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=92, stopped=6, cracked=2, operator_id=self.OP1)
        before = self.conn.execute(
            "SELECT COUNT(*) c FROM quantity_ledger WHERE batch_id=?", (b,)).fetchone()["c"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM quantity_ledger WHERE batch_id=?", (b,))
        after = self.conn.execute(
            "SELECT COUNT(*) c FROM quantity_ledger WHERE batch_id=?", (b,)).fetchone()["c"]
        self.assertEqual(before, after)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE quantity_ledger SET delta=-1 WHERE batch_id=?", (b,))

    def test_missed_tray_blocks_hatch(self):
        b = self._two_trays()
        services.candle_tray(self.conn, tray_code="T1",
                             fertile=100, stopped=0, cracked=0, operator_id=self.OP1)
        # T2 漏盘且无处置 → 拒绝出雏
        with self.assertRaises(services.DomainError):
            services.record_hatch(self.conn, batch_id=b, healthy=180, watch=0,
                                  weak=0, dead=0, operator_id=self.OP1)
        # 签收漏盘处置并补照后可出雏
        t2 = self.conn.execute("SELECT id FROM trays WHERE code='T2'").fetchone()["id"]
        inc = services.create_incident(
            self.conn, type_="missed_tray", batch_id=b, tray_id=t2,
            description="漏盘", disposition="补照", operator_id=self.OP1)
        services.handle_incident(self.conn, inc, self.OP1)
        services.candle_tray(self.conn, tray_code="T2",
                             fertile=80, stopped=0, cracked=0, operator_id=self.OP1)
        services.record_hatch(self.conn, batch_id=b, healthy=180, watch=0,
                              weak=0, dead=0, operator_id=self.OP1)
        self.assertEqual(self.balance(b), 0)


class TestSlotExclusivity(LedgerTestBase):
    def test_place_rejects_occupied_slot(self):
        b = self.make_batch()
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
        ])
        with self.assertRaises(services.DomainError):
            services.place_trays(self.conn, batch_id=b, operator_id=self.OP2, placements=[
                {"tray": "T2", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
            ])

    def test_move_rejects_target_occupied(self):
        b = self.make_batch()
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
            {"tray": "T2", "incubator": "IQ-01", "shelf": 1, "pos": 2, "eggs": 50},
        ])
        with self.assertRaises(services.DomainError):
            services.move_tray(self.conn, tray_code="T2", incubator_code="IQ-01",
                               shelf=1, pos=1, operator_id=self.OP2)

    def test_concurrent_moves_one_winner(self):
        """两名操作员同时把两张盘转到同一个盘位：只能有一个成功。"""
        b = self.make_batch()
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
            {"tray": "T2", "incubator": "IQ-01", "shelf": 1, "pos": 2, "eggs": 50},
        ])
        barrier = threading.Barrier(2)
        results = {}

        def worker(which, op, tray):
            c = connect(self.tmp.name)
            try:
                barrier.wait()
                services.move_tray(c, tray_code=tray, incubator_code="IQ-02",
                                   shelf=3, pos=4, operator_id=op, note="并发转盘")
                results[which] = "ok"
            except Exception as e:  # noqa: BLE001
                results[which] = f"fail:{type(e).__name__}"
            finally:
                c.close()

        t1 = threading.Thread(target=worker, args=(1, self.OP1, "T1"))
        t2 = threading.Thread(target=worker, args=(2, self.OP2, "T2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(results.values()).count("ok"), 1)
        occ = self.conn.execute(
            "SELECT COUNT(*) c FROM slot_occupancy o JOIN slots s ON s.id=o.slot_id "
            "JOIN incubators i ON i.id=s.incubator_id "
            "WHERE i.code='IQ-02' AND s.shelf_no=3 AND s.pos_no=4").fetchone()["c"]
        self.assertEqual(occ, 1)  # 同一盘位绝无两张托盘

    def test_concurrent_moves_same_tray_one_position(self):
        """同一张盘被两人同时转到不同机器：最终只能出现在一台机器上。"""
        b = self.make_batch()
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
        ])
        barrier = threading.Barrier(2)
        results = {}

        def worker(which, op, inc):
            c = connect(self.tmp.name)
            try:
                barrier.wait()
                services.move_tray(c, tray_code="T1", incubator_code=inc,
                                   shelf=2, pos=3, operator_id=op)
                results[which] = "ok"
            except Exception as e:  # noqa: BLE001
                results[which] = f"fail:{type(e).__name__}"
            finally:
                c.close()

        t1 = threading.Thread(target=worker, args=(1, self.OP1, "IQ-01"))
        t2 = threading.Thread(target=worker, args=(2, self.OP2, "IQ-02"))
        t1.start(); t2.start(); t1.join(); t2.join()

        positions = self.conn.execute(
            "SELECT i.code FROM slot_occupancy o JOIN slots s ON s.id=o.slot_id "
            "JOIN incubators i ON i.id=s.incubator_id JOIN trays t ON t.id=o.tray_id "
            "WHERE t.code='T1'").fetchall()
        self.assertEqual(len(positions), 1)  # 一盘蛋只在一台机器
        self.assertEqual(sorted(results.values()).count("ok"), 1)

    def test_move_into_stopped_incubator_blocked(self):
        b = self.make_batch()
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
        ])
        inc = services.report_incubator_stop(
            self.conn, incubator_id=2, note="停电", operator_id=self.OP1)
        with self.assertRaises(services.DomainError):
            services.move_tray(self.conn, tray_code="T1", incubator_code="IQ-02",
                               shelf=1, pos=1, operator_id=self.OP1)
        services.handle_incident(self.conn, inc, self.OP1, "已转移处置")
        services.resume_incubator(self.conn, incubator_id=2,
                                  incident_id=inc, operator_id=self.OP1)
        services.move_tray(self.conn, tray_code="T1", incubator_code="IQ-02",
                           shelf=1, pos=1, operator_id=self.OP1)  # 恢复后可转


class TestOverdueBlocking(LedgerTestBase):
    def test_overdue_blocked_without_disposition(self):
        b = self.make_batch(days_ago=10)
        with self.assertRaises(services.DomainError) as ctx:
            services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
                {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
            ])
        self.assertIn("储存超期", str(ctx.exception))

    def test_open_incident_does_not_unblock(self):
        b = self.make_batch(days_ago=10)
        services.create_incident(
            self.conn, type_="over_storage", batch_id=b,
            description="超期", disposition="待研究", operator_id=self.OP1)
        with self.assertRaises(services.DomainError):
            services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
                {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
            ])

    def test_handled_incident_unblocks(self):
        b = self.make_batch(days_ago=10)
        inc = services.create_incident(
            self.conn, type_="over_storage", batch_id=b,
            description="超期", disposition="主管批准降级", operator_id=self.OP1)
        services.handle_incident(self.conn, inc, self.OP1)
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
        ])
        self.assertEqual(self.balance(b), 50)

    def test_within_limit_ok(self):
        b = self.make_batch(days_ago=7)  # 恰好上限
        services.place_trays(self.conn, batch_id=b, operator_id=self.OP1, placements=[
            {"tray": "T1", "incubator": "IQ-01", "shelf": 1, "pos": 1, "eggs": 50},
        ])
        self.assertEqual(self.balance(b), 50)


class TestRolePermissions(LedgerTestBase):
    def test_keeper_cannot_sign_health_review(self):
        b = self.make_batch()
        with self.assertRaises(services.DomainError):
            services.sign_health_review(
                self.conn, batch_id=b, conclusion="pass",
                note="饲养员代签", user_id=self.KEEPER)

    def test_operator_cannot_sign_either(self):
        b = self.make_batch()
        with self.assertRaises(services.DomainError):
            services.sign_health_review(
                self.conn, batch_id=b, conclusion="pass",
                note="操作员代签", user_id=self.OP1)

    def test_database_trigger_blocks_keeper_signature(self):
        """即使绕过服务层，数据库触发器也拒绝非兽医签署。"""
        b = self.make_batch()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO health_reviews(batch_id,conclusion,note,vet_id) "
                "VALUES(?,?,?,?)", (b, "pass", "直接插库代签", self.KEEPER))

    def test_vet_can_sign(self):
        b = self.make_batch()
        rid = services.sign_health_review(
            self.conn, batch_id=b, conclusion="pass",
            note="健雏合格，同意发苗", user_id=self.VET)
        row = self.conn.execute(
            "SELECT u.role FROM health_reviews hr JOIN users u ON u.id=hr.vet_id "
            "WHERE hr.id=?", (rid,)).fetchone()
        self.assertEqual(row["role"], "vet")

    def test_keeper_cannot_register_batch(self):
        with self.assertRaises(services.DomainError):
            services.create_batch(
                self.conn, source_flock="X", collected_date=date.today().isoformat(),
                storage_temp_c=16, operator_id=self.KEEPER)


if __name__ == "__main__":
    unittest.main()
