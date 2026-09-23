PRAGMA foreign_keys = ON;

-- 古树档案
CREATE TABLE IF NOT EXISTS trees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    species TEXT,
    location TEXT,
    risk_level TEXT NOT NULL DEFAULT 'low',
    created_at TEXT NOT NULL
);

-- 养护人员（负责人）
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'technician',
    status TEXT NOT NULL DEFAULT 'active', -- active | transferred | inactive
    created_at TEXT NOT NULL
);

-- 传感设备：一棵树某类指标可有多个设备（label 区分）
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    metric TEXT NOT NULL, -- diameter | moisture | tilt | pest
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'online', -- online | offline
    installed_at TEXT NOT NULL,
    UNIQUE(tree_id, metric, label)
);

-- 设备状态变更（离线/上线）留痕，供时间线与回放
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    event_type TEXT NOT NULL, -- status_change
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL
);

-- 设备校准记录
CREATE TABLE IF NOT EXISTS device_calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    calibrated_at TEXT NOT NULL,
    method TEXT,
    deviation REAL,
    note TEXT,
    recorded_at TEXT NOT NULL
);

-- 观测读数。observed_at 为采集时刻，received_at 为入库时刻；
-- is_backfill=1 表示乱序补报（落在已确认阶段结论区间内），只存档不改写结论。
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    is_backfill INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'device' -- device | manual_import
);

-- 人工巡查（含病虫害）
CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    inspector_id INTEGER REFERENCES staff(id),
    inspected_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general', -- pest | bark | root | canopy | general
    severity TEXT NOT NULL DEFAULT 'none',    -- none | minor | severe
    finding TEXT,
    recorded_at TEXT NOT NULL
);

-- 养护措施
CREATE TABLE IF NOT EXISTS measures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    measure_type TEXT NOT NULL, -- irrigation | pest_control | bracing | fertilization | ...
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active', -- active | completed
    note TEXT,
    recorded_at TEXT NOT NULL
);

-- 气象窗口（按区域，区域与 trees.location 对应）
CREATE TABLE IF NOT EXISTS weather_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    region TEXT NOT NULL,
    window_type TEXT NOT NULL, -- frost | drought | storm | heatwave
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    note TEXT
);

-- 研判规则版本：effective_from 之后的研判才使用新版本
CREATE TABLE IF NOT EXISTS rule_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    config TEXT NOT NULL, -- JSON
    created_at TEXT NOT NULL
);

-- 阶段结论。status=confirmed 后不可改写；新区间不得与已确认区间重叠。
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    trend TEXT NOT NULL, -- improving | stable | declining | insufficient_data
    detail TEXT NOT NULL, -- JSON：各指标前后窗口均值/增量/判定
    status TEXT NOT NULL DEFAULT 'draft', -- draft | confirmed
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

-- 异常读数研判：可信度与证据快照
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_id INTEGER NOT NULL REFERENCES readings(id),
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    metric TEXT NOT NULL,
    classification TEXT NOT NULL, -- sensor_drift | seasonal | measure_failure | unclassified
    credibility REAL NOT NULL,    -- 0..1，异常为真实树体变化的可信度
    evidence TEXT NOT NULL,       -- JSON：邻近设备/巡查/校准/气象等证据快照
    rule_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | attached | dismissed
    created_at TEXT NOT NULL
);

-- 告警
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    metric TEXT NOT NULL,
    level TEXT NOT NULL,   -- watch | warning | critical
    status TEXT NOT NULL DEFAULT 'open', -- open | escalated | merged | resolved
    merged_into INTEGER REFERENCES alerts(id),
    rule_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 告警 ↔ 异常 关联（回放依据）
CREATE TABLE IF NOT EXISTS alert_anomalies (
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    anomaly_id INTEGER NOT NULL REFERENCES anomalies(id),
    PRIMARY KEY (alert_id, anomaly_id)
);

-- 告警事件流：合并/升级/解除等全部留痕，支持回放
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    event_type TEXT NOT NULL, -- created | merged | escalated | resolved | note
    reason TEXT NOT NULL,
    actor TEXT,
    detail TEXT, -- JSON
    created_at TEXT NOT NULL
);

-- 处置任务：可认领、有处置期限与升级期限；负责人调岗/设备离线后仍持续追踪
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alerts(id),
    tree_id INTEGER NOT NULL REFERENCES trees(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open | claimed | done
    claimed_by INTEGER REFERENCES staff(id),
    claimed_at TEXT,
    due_at TEXT NOT NULL,
    escalation_deadline TEXT NOT NULL,
    escalated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    event_type TEXT NOT NULL, -- created | claimed | released | escalated | done
    reason TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_version(version) VALUES (2);
