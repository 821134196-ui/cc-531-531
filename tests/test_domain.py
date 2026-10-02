"""领域规则测试：数量守恒 / 盘位独占（含并发转盘）/ 超期阻断 / 角色权限。

运行：python -m unittest discover -s tests   或   ./run_tests.sh
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta

from app import db, service
from app.errors import (ConflictError, ConservationError, OverdueError,
                        PermissionError)


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hatch-test-")
        self.path = os.path.join(self.dir, "t.db")
        self.conn = db.connect(self.path)
        db.init_db(self.conn)
        self._fixtures()

    def tearDown(self):
        self.conn.close()

    def _fixtures(self):
        c = self.conn
        self.vet = c.execute("INSERT INTO users(name,role) VALUES ('兽医','vet')").lastrowid
        self.op1 = c.execute("INSERT INTO users(name,role) VALUES ('操作员甲','operator')").lastrowid
        self.op2 = c.execute("INSERT INTO users(name,role) VALUES ('操作员乙','operator')").lastrowid
        self.keeper = c.execute("INSERT INTO users(name,role) VALUES ('饲养员','keeper')").lastrowid
        self.flock = c.execute(
            "INSERT INTO flocks(code,name,house) VALUES ('F1','测试鸡群','Z栋')").lastrowid
        t = service.now().isoformat()
        self.incA = c.execute(
            "INSERT INTO incubators(code,name,status,layer_count,slot_count,created_at) "
            "VALUES ('IA','A箱','running',2,2,?)", (t,)).lastrowid
        self.incB = c.execute(
            "INSERT INTO incubators(code,name,status,layer_count,slot_count,created_at) "
            "VALUES ('IB','B箱','running',2,2,?)", (t,)).lastrowid
        for iid in (self.incA, self.incB):
            for L in (1, 2):
                for S in (1, 2):
                    c.execute("INSERT INTO rack_slots(incubator_id,layer_no,slot_no) VALUES (?,?,?)",
                              (iid, L, S))

    def register(self, days_ago=1, count=100, temp=16.0):
        d = (date.today() - timedelta(days=days_ago)).isoformat()
        return service.register_batch(self.conn, self.flock, d, temp, count, self.op1)["id"]

    def set_two_trays(self, bid, a=60, b=40):
        service.set_batch(self.conn, bid, self.incA, [
            {"layer_no": 1, "slot_no": 1, "egg_count": a},
            {"layer_no": 1, "slot_no": 2, "egg_count": b},
        ], self.op1)

    def candle_all(self, bid, plan):
        for tray_id, counts in plan.items():
            service.add_candling(self.conn, bid, tray_id, counts, self.op1)
        service.finalize_candling(self.conn, bid, self.op1)


class TestConservation(CaseBase):
    def test_set_count_must_match_registration(self):
        bid = self.register(count=100)
        with self.assertRaises(ConservationError):
            service.set_batch(self.conn, bid, self.incA, [
                {"layer_no": 1, "slot_no": 1, "egg_count": 55},
                {"layer_no": 1, "slot_no": 2, "egg_count": 40},  # 合计 95
            ], self.op1)

    def test_candle_classes_must_sum_to_tray_count(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        with self.assertRaises(ConservationError):
            # 50+4+4+1=59 ≠ 在盘 60
            service.add_candling(self.conn, bid, 1,
                                 {"fertile": 50, "infertile": 4, "stopped": 4, "broken": 1},
                                 self.op1)

    def test_full_flow_balances_and_ledger_is_immutable(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        self.candle_all(bid, {
            1: {"fertile": 50, "infertile": 4, "stopped": 4, "broken": 2},   # 60
            2: {"fertile": 30, "infertile": 4, "stopped": 4, "broken": 2},   # 40
        })
        bal = service.ledger_balance(self.conn, bid)
        self.assertEqual((bal["set_in"], bal["candle_loss"], bal["after_candle"]),
                         (100, 20, 80))
        self.assertTrue(bal["balanced"])

        # 破壳延期 2 枚：先处置再出雏
        iid = service.open_incident_api(self.conn, "late_hatch", bid, self.incA,
                                        "破壳延期", "2 枚", self.op1)
        service.handle_incident(self.conn, iid, self.op1, "延长观察 24h")
        with self.assertRaises(ConservationError):
            service.hatch_batch(self.conn, bid, 76, 3, 2, self.op1)  # 合计 81 ≠ 基数 80
        # 正确数：70 健康 + 8 待观察 + 2 延期 = 80
        service.hatch_batch(self.conn, bid, 70, 8, 2, self.op1)
        bal = service.ledger_balance(self.conn, bid)
        self.assertEqual((bal["healthy"], bal["observe"], bal["remaining"]), (70, 8, 2))
        d = service.batch_detail(self.conn, bid)
        self.assertEqual(d["hatch_rate_pct"], 70.0)

        # 损耗不能从历史里删掉：流水禁止 DELETE / UPDATE
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("DELETE FROM quantity_ledger WHERE delta < 0")
        with self.assertRaises(sqlite3.Error):
            self.conn.execute("UPDATE quantity_ledger SET delta=0 WHERE id=1")
        # 流水仍完整
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM quantity_ledger WHERE batch_id=?", (bid,)).fetchone()["c"],
            2 + 6 + 2)  # 2 盘入孵 + 6 照蛋损耗(每盘3条非零) + 2 出雏（延期2枚留在基数里）

    def test_missed_tray_blocks_candling_finalize(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        service.add_candling(self.conn, bid, 1,
                             {"fertile": 50, "infertile": 4, "stopped": 4, "broken": 2},
                             self.op1)
        with self.assertRaises(ConservationError):
            service.finalize_candling(self.conn, bid, self.op1)
        # 漏盘自动开处置单
        self.assertTrue(service.has_handled_incident(self.conn, bid, "missed_tray") is False)
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM incidents WHERE batch_id=? AND kind='missed_tray'",
            (bid,)).fetchone()["c"]
        self.assertEqual(n, 1)


class TestSlotExclusivity(CaseBase):
    def test_same_slot_cannot_hold_two_trays(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        # 托盘1已在 A箱1-1，把托盘2也转到 1-1 → 冲突
        with self.assertRaises(ConflictError):
            service.move_tray(self.conn, 2, self.incA, 1, 1, self.op2)

    def test_tray_can_only_be_in_one_machine(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        service.move_tray(self.conn, 1, self.incB, 2, 1, self.op2, reason="transfer")
        pos = service.get_tray_position(self.conn, 1)
        self.assertEqual(pos["incubator_id"], self.incB)
        # 原槽位已空，可放入别的盘
        service.move_tray(self.conn, 2, self.incA, 1, 1, self.op2)

    def test_stopped_incubator_rejects_inbound(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        service.stop_incubator(self.conn, self.incB, self.op1, "断电演练")
        with self.assertRaises(ConflictError):
            service.move_tray(self.conn, 1, self.incB, 2, 2, self.op2, reason="evacuation")

    def test_concurrent_operators_same_destination_one_wins(self):
        """两名操作员同时把不同盘转到同一槽位：恰好一个成功，一盘绝不同时在两台机器。"""
        bid = self.register(count=100)
        self.set_two_trays(bid)
        results = {}
        barrier = threading.Barrier(2)

        def move(tray_id, op, key):
            conn = db.connect(self.path)
            try:
                barrier.wait(timeout=10)
                service.move_tray(conn, tray_id, self.incB, 2, 2, op, reason="transfer")
                results[key] = "ok"
            except ConflictError as e:
                results[key] = f"conflict:{e}"
            except Exception as e:  # noqa
                results[key] = f"other:{type(e).__name__}"
            finally:
                conn.close()

        t1 = threading.Thread(target=move, args=(1, self.op1, "t1"))
        t2 = threading.Thread(target=move, args=(2, self.op2, "t2"))
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        oks = [k for k, v in results.items() if v == "ok"]
        conflicts = [k for k, v in results.items() if v.startswith("conflict")]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(conflicts), 1, results)

        # 落库不变量：每槽至多一盘；每盘只在一个槽；两盘不在同一槽
        dup = self.conn.execute(
            "SELECT slot_id, COUNT(*) c FROM tray_positions WHERE slot_id IS NOT NULL "
            "AND status='active' GROUP BY slot_id HAVING c>1").fetchall()
        self.assertEqual(dup, [])
        p1 = service.get_tray_position(self.conn, 1)
        p2 = service.get_tray_position(self.conn, 2)
        self.assertNotEqual(p1["slot_id"], p2["slot_id"])


class TestStorageOverdue(CaseBase):
    def test_overdue_blocked_then_allowed_after_handling(self):
        bid = self.register(days_ago=10, count=100)
        with self.assertRaises(OverdueError):
            self.set_two_trays(bid)
        # 处置闭环后放行
        iid = service.open_incident_api(self.conn, "storage_overdue", bid, None,
                                        "储存超期", "收集后 10 天", self.op1)
        service.handle_incident(self.conn, iid, self.op1, "抽检合格，提前入孵单独统计")
        self.set_two_trays(bid)
        self.assertEqual(service.get_batch(self.conn, bid)["status"], "incubating")

    def test_fresh_eggs_pass(self):
        bid = self.register(days_ago=2, count=100)
        self.set_two_trays(bid)  # 不抛异常


class TestRoles(CaseBase):
    def test_keeper_cannot_register_or_candle(self):
        with self.assertRaises(PermissionError):
            service.register_batch(self.conn, self.flock,
                                   (date.today() - timedelta(days=1)).isoformat(),
                                   16.0, 10, self.keeper)
        bid = self.register(count=100)
        self.set_two_trays(bid)
        with self.assertRaises(PermissionError):
            service.add_candling(self.conn, bid, 1,
                                 {"fertile": 50, "infertile": 4, "stopped": 4, "broken": 2},
                                 self.keeper)

    def test_only_vet_signs_health_review(self):
        bid = self.register(count=100)
        self.set_two_trays(bid)
        self.candle_all(bid, {
            1: {"fertile": 50, "infertile": 4, "stopped": 4, "broken": 2},
            2: {"fertile": 30, "infertile": 4, "stopped": 4, "broken": 2},
        })
        service.hatch_batch(self.conn, bid, 64, 16, 0, self.op1)
        # 饲养员不能代签
        with self.assertRaises(PermissionError):
            service.health_review(self.conn, bid, self.keeper, "pass")
        # 操作员也不能签
        with self.assertRaises(PermissionError):
            service.health_review(self.conn, bid, self.op1, "pass")
        # 兽医签署成功
        r = service.health_review(self.conn, bid, self.vet, "pass", "同意转群")
        self.assertEqual(r["conclusion"], "pass")
        # 签署不可更改/重复签
        with self.assertRaises(Exception):
            service.health_review(self.conn, bid, self.vet, "fail")

    def test_operator_can_handle_incidents(self):
        bid = self.register(days_ago=10)
        iid = service.open_incident_api(self.conn, "storage_overdue", bid, None,
                                        "超期", "10 天", self.op2)
        service.handle_incident(self.conn, iid, self.op1, "处置完成")  # 不抛异常
        self.assertTrue(service.has_handled_incident(self.conn, bid, "storage_overdue"))


if __name__ == "__main__":
    unittest.main()
