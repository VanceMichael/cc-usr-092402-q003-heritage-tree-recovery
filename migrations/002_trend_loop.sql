PRAGMA foreign_keys = ON;

-- 古树档案
CREATE TABLE IF NOT EXISTS trees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    species TEXT,
    location TEXT
);

-- 班组人员；active=0 表示已调岗/离岗
CREATE TABLE IF NOT EXISTS staff (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'technician',
    active INTEGER NOT NULL DEFAULT 1
);

-- 监测设备，metric ∈ diameter|moisture|tilt|pest
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    metric TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'online',   -- online|offline
    installed_at TEXT NOT NULL
);

-- 设备校准事件（时间线的一部分）
CREATE TABLE IF NOT EXISTS device_calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id),
    calibrated_at TEXT NOT NULL,
    drift REAL,
    note TEXT
);

-- 观测读数。observed_at 为采集时刻，reported_at 为入库时刻（补报时二者不同）。
-- 异常读数带可信度与隔离标记；late=1 表示观测时刻早于最近一次已确认结论。
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id),
    tree_id TEXT NOT NULL REFERENCES trees(id),
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    observed_at TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    is_anomaly INTEGER NOT NULL DEFAULT 0,
    credibility REAL NOT NULL DEFAULT 1.0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    late INTEGER NOT NULL DEFAULT 0,
    credibility_reasons TEXT NOT NULL DEFAULT '[]'
);

-- 人工巡查
CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    inspector_id TEXT REFERENCES staff(id),
    inspected_at TEXT NOT NULL,
    finding TEXT NOT NULL,                   -- normal|concern|critical
    note TEXT
);

-- 养护措施
CREATE TABLE IF NOT EXISTS measures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    measure_type TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    applied_by TEXT REFERENCES staff(id),
    note TEXT
);

-- 气象窗口
CREATE TABLE IF NOT EXISTS weather_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    kind TEXT NOT NULL,                      -- rain|heatwave|frost|wind
    started_at TEXT NOT NULL,
    ended_at TEXT,
    severity TEXT NOT NULL DEFAULT 'moderate'
);

-- 规则版本，effective_from 之后的研判才使用
CREATE TABLE IF NOT EXISTS rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    rules TEXT NOT NULL,                     -- JSON，缺省键回落到内置默认
    created_at TEXT NOT NULL
);

-- 阶段结论：committed=1 的行一旦写入永不修改；
-- 乱序补报只进入下一次研判的 evidence，不回写历史结论。
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    assessed_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    risk_level TEXT NOT NULL,                -- low|medium|high|critical
    phase TEXT NOT NULL,                     -- stable|improving|declining
    committed INTEGER NOT NULL DEFAULT 1,
    evidence TEXT NOT NULL DEFAULT '{}',
    supersedes INTEGER REFERENCES assessments(id)
);

-- 高风险告警：可认领、有升级期限；合并时不新建行，只在 alert_events 留痕
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id TEXT NOT NULL REFERENCES trees(id),
    risk_type TEXT NOT NULL,
    severity TEXT NOT NULL,                  -- high|critical
    status TEXT NOT NULL DEFAULT 'open',     -- open|claimed|resolved
    owner_id TEXT REFERENCES staff(id),
    created_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    resolved_at TEXT
);

-- 告警事件流：合并/升级/认领/转派/解除/降级追踪的全部原因，供回放
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    event_type TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_tree_metric ON readings(tree_id, metric, observed_at);
CREATE INDEX IF NOT EXISTS idx_readings_reported ON readings(tree_id, reported_at);
CREATE INDEX IF NOT EXISTS idx_alerts_tree_status ON alerts(tree_id, status);
CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events(alert_id, id);

INSERT OR IGNORE INTO schema_version(version) VALUES (2);
