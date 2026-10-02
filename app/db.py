"""蛋厂孵化管理 —— 数据库结构与连接。

数量流水 quantity_ledger 只追加：UPDATE/DELETE 由触发器直接拒绝，
损耗只能以负数流水冲抵，不能从历史里删掉。
"""
import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('vet','operator','keeper')),
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS flocks (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    house       TEXT
);

CREATE TABLE IF NOT EXISTS incubators (
    id           INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running','stopped')),
    layer_count  INTEGER NOT NULL,
    slot_count   INTEGER NOT NULL,          -- 每层槽位数
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rack_slots (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    layer_no      INTEGER NOT NULL,
    slot_no       INTEGER NOT NULL,
    UNIQUE (incubator_id, layer_no, slot_no)
);

CREATE TABLE IF NOT EXISTS trays (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

-- 种蛋批次
CREATE TABLE IF NOT EXISTS egg_batches (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT NOT NULL UNIQUE,
    flock_id              INTEGER NOT NULL REFERENCES flocks(id),
    collected_at          TEXT NOT NULL,   -- 收集日期
    storage_temp_c        REAL NOT NULL,   -- 储存温度
    registered_egg_count  INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'registered'
                          CHECK (status IN ('registered','incubating','candled',
                                            'hatched','reviewed')),
    incubator_id          INTEGER REFERENCES incubators(id),
    set_at                TEXT,
    created_at            TEXT NOT NULL,
    notes                 TEXT
);

-- 异常处置记录（混放/超期/停机/漏盘/移位/破壳延期）
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('mixed_source','storage_overdue','incubator_stop',
                                  'missed_tray','tray_shift','late_hatch')),
    batch_id      INTEGER REFERENCES egg_batches(id),
    incubator_id  INTEGER REFERENCES incubators(id),
    title         TEXT NOT NULL,
    description   TEXT,
    action        TEXT,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','handled')),
    reporter_id   INTEGER REFERENCES users(id),
    handler_id    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL,
    handled_at    TEXT
);

CREATE TABLE IF NOT EXISTS stop_events (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    incident_id   INTEGER REFERENCES incidents(id),
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS signal_events (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    kind          TEXT NOT NULL,           -- power_off / power_on
    occurred_at   TEXT NOT NULL,
    detail        TEXT
);

CREATE TABLE IF NOT EXISTS temp_readings (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    temp_c        REAL NOT NULL,
    read_at       TEXT NOT NULL
);

-- 托盘当前盘位（tray_id 唯一：一盘蛋不可能同时在两台机器）
CREATE TABLE IF NOT EXISTS tray_positions (
    tray_id     INTEGER PRIMARY KEY REFERENCES trays(id),
    batch_id    INTEGER NOT NULL REFERENCES egg_batches(id),
    slot_id     INTEGER REFERENCES rack_slots(id),
    egg_count   INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','quarantined','removed')),
    updated_at  TEXT NOT NULL
);
-- 同一槽位至多一个在盘托盘（部分唯一索引，出雏下架的 NULL 不占槽）
CREATE UNIQUE INDEX IF NOT EXISTS ux_slot_active
    ON tray_positions(slot_id) WHERE slot_id IS NOT NULL;

-- 转盘流水（含入孵上架 / 疏散 / 移位纠正 / 出雏下架）
CREATE TABLE IF NOT EXISTS tray_movements (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES egg_batches(id),
    tray_id       INTEGER NOT NULL REFERENCES trays(id),
    from_slot_id  INTEGER REFERENCES rack_slots(id),
    to_slot_id    INTEGER REFERENCES rack_slots(id),
    reason        TEXT NOT NULL,
    incident_id   INTEGER REFERENCES incidents(id),
    operator_id   INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL
);

-- 只追加数量流水
CREATE TABLE IF NOT EXISTS quantity_ledger (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES egg_batches(id),
    stage       TEXT NOT NULL CHECK (stage IN ('set','candle','hatch')),
    entry_type  TEXT NOT NULL CHECK (entry_type IN (
                    'set_in','candle_infertile','candle_stopped','candle_broken',
                    'hatch_healthy','hatch_observe')),
    delta       INTEGER NOT NULL CHECK (delta <> 0),
    tray_id     INTEGER REFERENCES trays(id),
    operator_id INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete
BEFORE DELETE ON quantity_ledger
BEGIN
    SELECT RAISE(ABORT, '数量流水只追加，禁止删除（损耗以负数流水记录）');
END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE ON quantity_ledger
BEGIN
    SELECT RAISE(ABORT, '数量流水只追加，禁止修改');
END;

-- 照蛋记录（每批每盘一条）
CREATE TABLE IF NOT EXISTS candling_records (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES egg_batches(id),
    tray_id     INTEGER NOT NULL REFERENCES trays(id),
    candled_at  TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    egg_count   INTEGER NOT NULL,
    fertile     INTEGER NOT NULL,        -- 受精（继续发育）
    infertile   INTEGER NOT NULL,        -- 无精
    stopped     INTEGER NOT NULL,        -- 停育
    broken      INTEGER NOT NULL,        -- 破损
    note        TEXT,
    UNIQUE (batch_id, tray_id)
);

CREATE TABLE IF NOT EXISTS candling_photos (
    id           INTEGER PRIMARY KEY,
    candling_id  INTEGER NOT NULL REFERENCES candling_records(id),
    kind         TEXT NOT NULL,          -- overview / fertile / stopped / broken
    path         TEXT NOT NULL,          -- 本地文件相对路径
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hatch_records (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL UNIQUE REFERENCES egg_batches(id),
    hatched_at  TEXT NOT NULL,
    operator_id INTEGER NOT NULL REFERENCES users(id),
    healthy     INTEGER NOT NULL,        -- 健康雏鸡
    observe     INTEGER NOT NULL,        -- 待观察个体
    late        INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS health_reviews (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL UNIQUE REFERENCES egg_batches(id),
    vet_id      INTEGER NOT NULL REFERENCES users(id),
    conclusion  TEXT NOT NULL CHECK (conclusion IN ('pass','observe','fail')),
    note        TEXT,
    signed_at   TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
