-- 种蛋孵化台账 · 数据模型
-- 核心原则：数量全部以追加流水表达，任何历史记录不可删除/不可改写。

PRAGMA foreign_keys = ON;

-- 操作员（角色：operator 操作员 / keeper 饲养员 / vet 兽医）
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('operator','keeper','vet'))
);

-- 种蛋批次（按来源鸡群、收集日期、储存温度登记）
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- 批次编号 B2026-…
    source_flock    TEXT NOT NULL,                 -- 来源鸡群
    collected_date  TEXT NOT NULL,                 -- 收集日期 YYYY-MM-DD
    storage_temp_c  REAL NOT NULL,                 -- 储存温度 ℃
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 孵化箱
CREATE TABLE IF NOT EXISTS incubators (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,              -- 如 IQ-01
    name        TEXT NOT NULL,
    shelf_rows  INTEGER NOT NULL,                  -- 层数
    shelf_cols  INTEGER NOT NULL                   -- 每层托盘位数
);

-- 孵化箱盘位（每个托盘格）
CREATE TABLE IF NOT EXISTS slots (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    shelf_no      INTEGER NOT NULL,                -- 层（自上而下）
    pos_no        INTEGER NOT NULL,                -- 本层位置（自左而右）
    UNIQUE (incubator_id, shelf_no, pos_no)
);

-- 托盘：一张物理托盘，同一时刻至多占用一个盘位（current_slot_id 唯一）
CREATE TABLE IF NOT EXISTS trays (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- 托盘编号 T-A1 …
    batch_id        INTEGER REFERENCES batches(id),
    egg_count       INTEGER NOT NULL DEFAULT 0,    -- 托盘当前在孵蛋数
    current_slot_id INTEGER REFERENCES slots(id) UNIQUE,
    CHECK (egg_count >= 0)
);

-- 数量阶段
-- set     入孵登记（每盘仅一次，建立基数，正数）
-- loss    损耗（负数）：照蛋停育/破损，出雏弱雏/死胎
-- harvest 收成（负数，蛋以雏鸡形态离账）：健康雏 / 待观察
-- move    转盘不改变数量（delta 恒为 0），只留痕
CREATE TABLE IF NOT EXISTS quantity_ledger (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    tray_id     INTEGER REFERENCES trays(id),
    stage       TEXT NOT NULL CHECK (stage IN ('set','candle','hatch')),
    change_type TEXT NOT NULL CHECK (change_type IN ('set','loss','harvest','move')),
    reason      TEXT,                              -- stopped/cracked/weak/dead/healthy/watch
    delta       INTEGER NOT NULL,                  -- set 为正；loss/harvest 为负；move 为 0
    balance_after INTEGER NOT NULL,                -- 批次该阶段结余
    operator_id INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    reversed    INTEGER NOT NULL DEFAULT 0,        -- 冲正标记（1=已被红字冲正，原行保留）
    CHECK (
        (change_type='set'     AND delta > 0) OR
        (change_type='loss'    AND delta < 0) OR
        (change_type='harvest' AND delta < 0) OR
        (change_type='move'    AND delta = 0)
    )
);

-- 托盘位置流水（追加；最新一行 = 当前位置）。唯一索引保证同一盘位同时刻只有一张托盘。
CREATE TABLE IF NOT EXISTS tray_movements (
    id            INTEGER PRIMARY KEY,
    tray_id       INTEGER NOT NULL REFERENCES trays(id),
    from_slot_id  INTEGER REFERENCES slots(id),
    to_slot_id    INTEGER REFERENCES slots(id),    -- NULL = 出箱（出雏落盘）
    incident_id   INTEGER,                         -- 关联异常处置（可为普通转盘）
    operator_id   INTEGER NOT NULL REFERENCES users(id),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 唯一的“当前占用”视图实体化：一个盘位最多一张在箱托盘
CREATE TABLE IF NOT EXISTS slot_occupancy (
    slot_id INTEGER PRIMARY KEY REFERENCES slots(id),
    tray_id INTEGER NOT NULL UNIQUE REFERENCES trays(id),
    CHECK (tray_id IS NOT NULL)
);

-- 照蛋记录（每盘每次照蛋一行；一盘只允许一条，补照须先有漏盘处置，重复照蛋走异常更正）
CREATE TABLE IF NOT EXISTS candle_records (
    id            INTEGER PRIMARY KEY,
    tray_id       INTEGER NOT NULL UNIQUE REFERENCES trays(id),
    batch_id      INTEGER NOT NULL REFERENCES batches(id),
    fertile       INTEGER NOT NULL CHECK (fertile >= 0),
    stopped       INTEGER NOT NULL CHECK (stopped >= 0),
    cracked       INTEGER NOT NULL CHECK (cracked >= 0),
    operator_id   INTEGER NOT NULL REFERENCES users(id),
    candled_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    note          TEXT
);

-- 照蛋数必须能对上托盘蛋数（CHECK 不支持子查询，用触发器强制）
CREATE TRIGGER IF NOT EXISTS trg_candle_count_match
BEFORE INSERT ON candle_records
FOR EACH ROW
WHEN (NEW.fertile + NEW.stopped + NEW.cracked <>
      (SELECT trays.egg_count - COALESCE(SUM(CASE WHEN quantity_ledger.change_type='loss' THEN -quantity_ledger.delta ELSE 0 END),0)
       FROM trays LEFT JOIN quantity_ledger ON quantity_ledger.tray_id = trays.id
       WHERE trays.id = NEW.tray_id))
BEGIN
    SELECT RAISE(ABORT, '照蛋三项（受精/停育/破损）必须等于托盘当前在孵蛋数，逐盘对得上');
END;

-- 照蛋照片（本地磁盘存储，库存相对路径）
CREATE TABLE IF NOT EXISTS candle_photos (
    id               INTEGER PRIMARY KEY,
    candle_record_id INTEGER NOT NULL REFERENCES candle_records(id),
    file_path        TEXT NOT NULL,                 -- uploads/ 下相对路径
    caption          TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 异常处置
-- 类型：mixed_source 混放来源 / over_storage 储存超期 / incubator_stop 孵化箱中途停机
--      missed_tray 照蛋漏盘 / tray_shift 托盘移位 / delayed_pip 破壳延期
CREATE TABLE IF NOT EXISTS incidents (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL CHECK (type IN (
                    'mixed_source','over_storage','incubator_stop',
                    'missed_tray','tray_shift','delayed_pip')),
    batch_id      INTEGER REFERENCES batches(id),
    tray_id       INTEGER REFERENCES trays(id),
    incubator_id  INTEGER REFERENCES incubators(id),
    description   TEXT NOT NULL,
    disposition   TEXT NOT NULL,                    -- 处置措施
    status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','handled')),
    operator_id   INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    handled_at    TEXT
);

-- 孵化箱信号事件（模拟适配器写入：running/stopped 等）
CREATE TABLE IF NOT EXISTS incubator_events (
    id            INTEGER PRIMARY KEY,
    incubator_id  INTEGER NOT NULL REFERENCES incubators(id),
    event         TEXT NOT NULL,                    -- running/stopped/resumed
    temp_c        REAL,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 出雏统计（每批一行）
CREATE TABLE IF NOT EXISTS hatch_records (
    id           INTEGER PRIMARY KEY,
    batch_id     INTEGER NOT NULL UNIQUE REFERENCES batches(id),
    healthy      INTEGER NOT NULL CHECK (healthy >= 0),
    watch        INTEGER NOT NULL CHECK (watch >= 0),   -- 待观察个体
    weak         INTEGER NOT NULL DEFAULT 0 CHECK (weak >= 0),
    dead         INTEGER NOT NULL DEFAULT 0 CHECK (dead >= 0),
    operator_id  INTEGER NOT NULL REFERENCES users(id),
    hatched_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 出雏守恒：健康+待观察+弱雏+死胎 必须等于照蛋后批次在孵结余（一个都不能凭空消失）
CREATE TRIGGER IF NOT EXISTS trg_hatch_balance
BEFORE INSERT ON hatch_records
FOR EACH ROW
WHEN (NEW.healthy + NEW.watch + NEW.weak + NEW.dead <> (
        SELECT COALESCE(SUM(delta),0) FROM quantity_ledger
        WHERE batch_id = NEW.batch_id AND reversed = 0))
BEGIN
    SELECT RAISE(ABORT, '出雏数与在孵结余对不上：健康+待观察+弱+死胎必须等于入孵-照蛋损耗');
END;

-- 健康复核（仅兽医可签署；饲养员不能代签）
CREATE TABLE IF NOT EXISTS health_reviews (
    id           INTEGER PRIMARY KEY,
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    conclusion   TEXT NOT NULL CHECK (conclusion IN ('pass','conditional','reject')),
    note         TEXT,
    vet_id       INTEGER NOT NULL REFERENCES users(id),
    signed_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TRIGGER IF NOT EXISTS trg_review_vet_only
BEFORE INSERT ON health_reviews
FOR EACH ROW
WHEN (SELECT role FROM users WHERE id = NEW.vet_id) <> 'vet'
BEGIN
    SELECT RAISE(ABORT, '只有兽医可以签署健康结论，饲养员不得代签');
END;

-- 防篡改：流水与关键记录只许追加，禁止 DELETE/UPDATE（冲正只能追加反向行）
CREATE TRIGGER IF NOT EXISTS trg_ledger_no_del
BEFORE DELETE ON quantity_ledger
BEGIN SELECT RAISE(ABORT, '数量流水禁止删除：损耗只能红字冲正'); END;

CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update
BEFORE UPDATE OF batch_id, tray_id, stage, change_type, delta, created_at ON quantity_ledger
BEGIN SELECT RAISE(ABORT, '数量流水核心字段禁止修改'); END;

CREATE TRIGGER IF NOT EXISTS trg_movements_no_del
BEFORE DELETE ON tray_movements
BEGIN SELECT RAISE(ABORT, '转盘流水禁止删除'); END;

CREATE INDEX IF NOT EXISTS idx_ledger_batch ON quantity_ledger(batch_id, stage);
CREATE INDEX IF NOT EXISTS idx_movements_tray ON tray_movements(tray_id);
