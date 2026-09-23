"""研判闭环核心逻辑。

不变量：
1. 乱序补报（落在已确认阶段结论区间内的读数）只存档标记，不改写已确认结论；
   新的阶段研判区间不得与任何已确认区间重叠。
2. 异常读数的可信度由邻近设备、人工巡查、设备校准状态、气象窗口共同决定，
   证据以快照形式随异常记录保存，供回放。
3. 规则以版本生效（effective_from），研判只使用研判时刻已生效的最新版本；
   历史结论与异常记录保留当时的 rule_version。
4. 高风险变化生成可认领的处置任务（含处置期限与升级期限）；负责人调岗任务
   退回待认领池、设备离线不影响任务，超期未办结自动升级，全程留痕可回放。
"""
import json
import statistics
from datetime import timedelta

from .rules import DEFAULT_CONFIG, merged_config
from .util import iso, parse, utcnow

LEVELS = ["watch", "warning", "critical"]
OPEN_ALERT_STATUSES = ("open", "escalated")


class DomainError(Exception):
    def __init__(self, message, status=400, payload=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload or {}


def _one(db, sql, args=(), what="记录"):
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise DomainError(f"{what}不存在", 404)
    return row


def _now_or(value):
    return iso(parse(value)) if value else iso(utcnow())


# ---------------------------------------------------------------- 规则版本

def ensure_rule_sets(db):
    """首次使用时落地 v1 默认规则，保证任何时刻都有可用版本。"""
    count = db.execute("SELECT COUNT(*) AS c FROM rule_sets").fetchone()["c"]
    if count == 0:
        db.execute(
            "INSERT INTO rule_sets(version, effective_from, config, created_at)"
            " VALUES (1, '1970-01-01T00:00:00Z', ?, ?)",
            (json.dumps(DEFAULT_CONFIG, ensure_ascii=False), iso(utcnow())),
        )


def get_rule_set(db, at):
    """返回 at 时刻已生效的最新规则版本 (version, config)。"""
    ensure_rule_sets(db)
    row = db.execute(
        "SELECT * FROM rule_sets WHERE effective_from <= ?"
        " ORDER BY version DESC LIMIT 1",
        (iso(parse(at)),),
    ).fetchone()
    if row is None:  # 所有版本都在未来生效：退回最早版本
        row = db.execute("SELECT * FROM rule_sets ORDER BY version ASC LIMIT 1").fetchone()
    return row["version"], merged_config(json.loads(row["config"]))


def create_rule_set(db, effective_from, config, now=None):
    ensure_rule_sets(db)
    version = db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM rule_sets").fetchone()["v"]
    db.execute(
        "INSERT INTO rule_sets(version, effective_from, config, created_at) VALUES (?,?,?,?)",
        (version, iso(parse(effective_from)), json.dumps(config or {}, ensure_ascii=False), _now_or(now)),
    )
    return {"version": version, "effective_from": iso(parse(effective_from))}


# ---------------------------------------------------------------- 基础档案

def create_tree(db, code, name, species=None, location=None, risk_level="low", now=None):
    cur = db.execute(
        "INSERT INTO trees(code, name, species, location, risk_level, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (code, name, species, location, risk_level, _now_or(now)),
    )
    return _one(db, "SELECT * FROM trees WHERE id=?", (cur.lastrowid,))


def create_staff(db, name, role="technician", now=None):
    cur = db.execute(
        "INSERT INTO staff(name, role, status, created_at) VALUES (?,?, 'active', ?)",
        (name, role, _now_or(now)),
    )
    return _one(db, "SELECT * FROM staff WHERE id=?", (cur.lastrowid,))


def create_device(db, tree_id, metric, label="", installed_at=None, now=None):
    _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    cur = db.execute(
        "INSERT INTO devices(tree_id, metric, label, status, installed_at) VALUES (?,?,?, 'online', ?)",
        (tree_id, metric, label or "", _now_or(installed_at) if installed_at else _now_or(now)),
    )
    return _one(db, "SELECT * FROM devices WHERE id=?", (cur.lastrowid,))


def set_device_status(db, device_id, status, reason=None, now=None):
    if status not in ("online", "offline"):
        raise DomainError("status 只能是 online 或 offline")
    dev = _one(db, "SELECT * FROM devices WHERE id=?", (device_id,), "设备")
    ts = _now_or(now)
    db.execute("UPDATE devices SET status=? WHERE id=?", (status, device_id))
    db.execute(
        "INSERT INTO device_events(device_id, event_type, from_status, to_status, reason, created_at)"
        " VALUES (?, 'status_change', ?, ?, ?, ?)",
        (device_id, dev["status"], status, reason, ts),
    )
    # 设备离线不影响已生成的告警与处置任务，它们挂在树上继续追踪。
    return _one(db, "SELECT * FROM devices WHERE id=?", (device_id,))


def calibrate_device(db, device_id, calibrated_at, method=None, deviation=None, note=None, now=None):
    _one(db, "SELECT * FROM devices WHERE id=?", (device_id,), "设备")
    cur = db.execute(
        "INSERT INTO device_calibrations(device_id, calibrated_at, method, deviation, note, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (device_id, iso(parse(calibrated_at)), method, deviation, note, _now_or(now)),
    )
    return _one(db, "SELECT * FROM device_calibrations WHERE id=?", (cur.lastrowid,))


def create_inspection(db, tree_id, inspected_at, inspector_id=None, category="general",
                      severity="none", finding=None, now=None):
    _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    if inspector_id is not None:
        _one(db, "SELECT * FROM staff WHERE id=?", (inspector_id,), "人员")
    cur = db.execute(
        "INSERT INTO inspections(tree_id, inspector_id, inspected_at, category, severity, finding, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (tree_id, inspector_id, iso(parse(inspected_at)), category, severity, finding, _now_or(now)),
    )
    return _one(db, "SELECT * FROM inspections WHERE id=?", (cur.lastrowid,))


def create_measure(db, tree_id, measure_type, started_at, ended_at=None, note=None, now=None):
    _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    status = "completed" if ended_at else "active"
    cur = db.execute(
        "INSERT INTO measures(tree_id, measure_type, started_at, ended_at, status, note, recorded_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (tree_id, measure_type, iso(parse(started_at)),
         iso(parse(ended_at)) if ended_at else None, status, note, _now_or(now)),
    )
    return _one(db, "SELECT * FROM measures WHERE id=?", (cur.lastrowid,))


def create_weather_window(db, region, window_type, started_at, ended_at, note=None):
    cur = db.execute(
        "INSERT INTO weather_windows(region, window_type, started_at, ended_at, note)"
        " VALUES (?,?,?,?,?)",
        (region, window_type, iso(parse(started_at)), iso(parse(ended_at)), note),
    )
    return _one(db, "SELECT * FROM weather_windows WHERE id=?", (cur.lastrowid,))


# ---------------------------------------------------------------- 读数摄入与异常研判

def _is_backfill(db, tree_id, observed_at):
    """observed_at 落在任一已确认阶段结论区间内 → 乱序补报。"""
    row = db.execute(
        "SELECT id FROM assessments WHERE tree_id=? AND status='confirmed'"
        " AND period_start <= ? AND period_end >= ? LIMIT 1",
        (tree_id, observed_at, observed_at),
    ).fetchone()
    return row is not None


def ingest_reading(db, device_id, value, observed_at, received_at=None, source="device", now=None):
    dev = _one(db, "SELECT * FROM devices WHERE id=?", (device_id,), "设备")
    obs = iso(parse(observed_at))
    rec = _now_or(received_at) if received_at else _now_or(now)
    backfill = 1 if _is_backfill(db, dev["tree_id"], obs) else 0
    cur = db.execute(
        "INSERT INTO readings(device_id, metric, value, observed_at, received_at, is_backfill, source)"
        " VALUES (?,?,?,?,?,?,?)",
        (device_id, dev["metric"], float(value), obs, rec, backfill, source),
    )
    reading = _one(db, "SELECT * FROM readings WHERE id=?", (cur.lastrowid,))
    # 补报读数同样做异常研判（可能产生新的工作项），但绝不会触碰已确认结论。
    anomaly = evaluate_reading(db, reading, now=rec)
    return {"reading": dict(reading), "anomaly": anomaly}


def _baseline(db, device_id, observed_at, cfg):
    start = iso(parse(observed_at) - timedelta(days=cfg["baseline_window_days"]))
    rows = db.execute(
        "SELECT value FROM readings WHERE device_id=? AND observed_at >= ? AND observed_at < ?"
        " ORDER BY observed_at",
        (device_id, start, observed_at),
    ).fetchall()
    values = [r["value"] for r in rows]
    if len(values) < cfg["min_baseline_samples"]:
        return None
    std = statistics.pstdev(values) if len(values) > 1 else 0.0
    return {"mean": statistics.fmean(values), "std": max(std, 1e-6), "samples": len(values)}


def _neighbor_evidence(db, dev, observed_at, cfg):
    """同区域（location 相同）其他树上同指标设备的同期偏离情况。"""
    tree = _one(db, "SELECT * FROM trees WHERE id=?", (dev["tree_id"],), "古树")
    devices = db.execute(
        "SELECT d.* FROM devices d JOIN trees t ON t.id = d.tree_id"
        " WHERE d.metric=? AND d.tree_id != ? AND t.location = ?",
        (dev["metric"], dev["tree_id"], tree["location"] or ""),
    ).fetchall()
    window = timedelta(hours=cfg["neighbor_window_hours"])
    checked, corroborated, device_ids = 0, 0, []
    for other in devices:
        base = _baseline(db, other["id"], observed_at, cfg)
        if base is None:
            continue
        row = db.execute(
            "SELECT value FROM readings WHERE device_id=? AND observed_at BETWEEN ? AND ?"
            " ORDER BY ABS(strftime('%s', observed_at) - strftime('%s', ?)) LIMIT 1",
            (other["id"], iso(parse(observed_at) - window), iso(parse(observed_at) + window), observed_at),
        ).fetchone()
        if row is None:
            continue
        checked += 1
        # 邻近设备同期是否也被判定过异常（其自身基线可能已被污染，z 分仅作补充）
        flagged = db.execute(
            "SELECT a.id FROM anomalies a JOIN readings r ON r.id = a.reading_id"
            " WHERE r.device_id=? AND r.observed_at BETWEEN ? AND ? LIMIT 1",
            (other["id"], iso(parse(observed_at) - window), iso(parse(observed_at) + window)),
        ).fetchone()
        z = (row["value"] - base["mean"]) / base["std"]
        if flagged is not None or abs(z) >= cfg["anomaly_sigma"]:
            corroborated += 1
            device_ids.append(other["id"])
    ratio = (corroborated / checked) if checked else 0.0
    return {"checked": checked, "corroborated": corroborated, "ratio": ratio, "device_ids": device_ids}


def _inspection_evidence(db, tree_id, observed_at, cfg):
    window = timedelta(hours=cfg["inspection_window_hours"])
    rows = db.execute(
        "SELECT id, category, severity, inspected_at, finding FROM inspections"
        " WHERE tree_id=? AND severity != 'none' AND inspected_at BETWEEN ? AND ?"
        " ORDER BY inspected_at",
        (tree_id, iso(parse(observed_at) - window), iso(parse(observed_at) + window)),
    ).fetchall()
    return [dict(r) for r in rows]


def _calibration_evidence(db, device_id, observed_at, cfg):
    row = db.execute(
        "SELECT calibrated_at FROM device_calibrations WHERE device_id=? AND calibrated_at <= ?"
        " ORDER BY calibrated_at DESC LIMIT 1",
        (device_id, observed_at),
    ).fetchone()
    if row is None:
        return {"ever_calibrated": False, "days_since": None, "fresh": False}
    days = (parse(observed_at) - parse(row["calibrated_at"])).days
    return {"ever_calibrated": True, "days_since": days, "fresh": days <= cfg["calibration_fresh_days"]}


def _weather_evidence(db, tree, observed_at, metric, cfg):
    rows = db.execute(
        "SELECT window_type, started_at, ended_at FROM weather_windows"
        " WHERE region=? AND started_at <= ? AND ended_at >= ?",
        (tree["location"] or "", observed_at, observed_at),
    ).fetchall()
    mapping = cfg["weather_metric_map"]
    for r in rows:
        if metric in mapping.get(r["window_type"], []):
            return dict(r)
    return None


def _active_measure(db, tree_id, observed_at):
    row = db.execute(
        "SELECT id, measure_type, started_at FROM measures WHERE tree_id=?"
        " AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)"
        " ORDER BY started_at DESC LIMIT 1",
        (tree_id, observed_at, observed_at),
    ).fetchone()
    return dict(row) if row else None


def evaluate_reading(db, reading, now=None):
    """对单条读数做异常研判；返回异常记录（非异常返回 None）。"""
    dev = _one(db, "SELECT * FROM devices WHERE id=?", (reading["device_id"],), "设备")
    tree = _one(db, "SELECT * FROM trees WHERE id=?", (dev["tree_id"],), "古树")
    now = _now_or(now)
    rule_version, cfg = get_rule_set(db, now)
    observed_at = reading["observed_at"]

    base = _baseline(db, dev["id"], observed_at, cfg)
    if base is None:
        return None  # 基线样本不足，不研判
    z = (reading["value"] - base["mean"]) / base["std"]
    if abs(z) < cfg["anomaly_sigma"]:
        return None

    neighbors = _neighbor_evidence(db, dev, observed_at, cfg)
    inspections = _inspection_evidence(db, tree["id"], observed_at, cfg)
    calibration = _calibration_evidence(db, dev["id"], observed_at, cfg)
    weather = _weather_evidence(db, tree, observed_at, dev["metric"], cfg)
    measure = _active_measure(db, tree["id"], observed_at)

    w = cfg["weights"]
    parts = {"base": 0.5}
    if neighbors["checked"] > 0:
        parts["neighbor"] = w["neighbor"] * (2 * neighbors["ratio"] - 1)
    else:
        parts["neighbor"] = 0.0
    parts["inspection"] = w["inspection"] if inspections else 0.0
    device_suspect = (not calibration["fresh"]) or dev["status"] == "offline"
    parts["calibration"] = -w["calibration"] if device_suspect else 0.5 * w["calibration"]
    parts["weather"] = -w["weather"] if weather else 0.0
    credibility = max(0.0, min(1.0, sum(parts.values())))

    # 分类：季节/气象可解释优先，其次设备漂移，再次措施失效
    if weather and not inspections:
        classification = "seasonal"
    elif device_suspect and neighbors["corroborated"] == 0:
        classification = "sensor_drift"
    elif credibility >= cfg["alert_credibility_threshold"] and measure:
        classification = "measure_failure"
    else:
        classification = "unclassified"

    evidence = {
        "z": round(z, 3),
        "baseline": base,
        "neighbors": neighbors,
        "inspections": inspections,
        "calibration": calibration,
        "device_status": dev["status"],
        "weather": weather,
        "active_measure": measure,
        "backfill": bool(reading["is_backfill"]),
        "weights": w,
        "score_parts": {k: round(v, 4) for k, v in parts.items()},
    }
    status = "dismissed" if classification in ("sensor_drift", "seasonal") else "open"
    cur = db.execute(
        "INSERT INTO anomalies(reading_id, tree_id, metric, classification, credibility,"
        " evidence, rule_version, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (reading["id"], tree["id"], dev["metric"], classification, round(credibility, 4),
         json.dumps(evidence, ensure_ascii=False), rule_version, status, now),
    )
    anomaly = _one(db, "SELECT * FROM anomalies WHERE id=?", (cur.lastrowid,))
    alert = _maybe_alert(db, anomaly, cfg, now)
    result = dict(anomaly)
    result["evidence"] = evidence
    result["alert_id"] = alert["id"] if alert else None
    return result


def _maybe_alert(db, anomaly, cfg, now):
    if anomaly["classification"] in ("sensor_drift", "seasonal"):
        return None
    if anomaly["credibility"] < cfg["alert_credibility_threshold"]:
        return None
    level = "critical" if anomaly["credibility"] >= cfg["critical_credibility_threshold"] else "warning"

    window_start = iso(parse(now) - timedelta(hours=cfg["merge_window_hours"]))
    target = db.execute(
        "SELECT * FROM alerts WHERE tree_id=? AND metric=? AND status IN ('open','escalated')"
        " AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (anomaly["tree_id"], anomaly["metric"], window_start),
    ).fetchone()

    if target is not None:
        db.execute(
            "INSERT OR IGNORE INTO alert_anomalies(alert_id, anomaly_id) VALUES (?,?)",
            (target["id"], anomaly["id"]),
        )
        db.execute("UPDATE anomalies SET status='attached' WHERE id=?", (anomaly["id"],))
        _alert_event(db, target["id"], "merged",
                     f"异常#{anomaly['id']} 与告警#{target['id']} 同树同指标且处于合并窗口，并入处理",
                     detail={"anomaly_id": anomaly["id"]}, now=now)
        if LEVELS.index(level) > LEVELS.index(target["level"]):
            db.execute("UPDATE alerts SET level=?, updated_at=? WHERE id=?", (level, now, target["id"]))
            _alert_event(db, target["id"], "escalated",
                         f"新证据可信度 {anomaly['credibility']:.2f} 将级别提升为 {level}",
                         detail={"anomaly_id": anomaly["id"]}, now=now)
        else:
            db.execute("UPDATE alerts SET updated_at=? WHERE id=?", (now, target["id"]))
        return _one(db, "SELECT * FROM alerts WHERE id=?", (target["id"],))

    cur = db.execute(
        "INSERT INTO alerts(tree_id, metric, level, status, rule_version, created_at, updated_at)"
        " VALUES (?,?,?, 'open', ?, ?, ?)",
        (anomaly["tree_id"], anomaly["metric"], level, anomaly["rule_version"], now, now),
    )
    alert_id = cur.lastrowid
    db.execute(
        "INSERT OR IGNORE INTO alert_anomalies(alert_id, anomaly_id) VALUES (?,?)",
        (alert_id, anomaly["id"]),
    )
    db.execute("UPDATE anomalies SET status='attached' WHERE id=?", (anomaly["id"],))
    _alert_event(db, alert_id, "created",
                 f"异常#{anomaly['id']} 分类 {anomaly['classification']} "
                 f"可信度 {anomaly['credibility']:.2f} 达到阈值，生成 {level} 告警",
                 detail={"anomaly_id": anomaly["id"]}, now=now)
    _create_task(db, alert_id, anomaly["tree_id"], anomaly["metric"], cfg, now)
    return _one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,))


def _alert_event(db, alert_id, event_type, reason, actor=None, detail=None, now=None):
    db.execute(
        "INSERT INTO alert_events(alert_id, event_type, reason, actor, detail, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (alert_id, event_type, reason, actor,
         json.dumps(detail, ensure_ascii=False) if detail else None, _now_or(now)),
    )


def _task_event(db, task_id, event_type, reason, actor=None, now=None):
    db.execute(
        "INSERT INTO task_events(task_id, event_type, reason, actor, created_at) VALUES (?,?,?,?,?)",
        (task_id, event_type, reason, actor, _now_or(now)),
    )


def _create_task(db, alert_id, tree_id, metric, cfg, now):
    tree = _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    due = iso(parse(now) + timedelta(hours=cfg["task_due_hours"]))
    escalation = iso(parse(now) + timedelta(hours=cfg["task_due_hours"] + cfg["escalation_hours"]))
    cur = db.execute(
        "INSERT INTO tasks(alert_id, tree_id, title, status, due_at, escalation_deadline,"
        " created_at, updated_at) VALUES (?,?,?, 'open', ?,?,?,?)",
        (alert_id, tree_id, f"处置告警#{alert_id}（{tree['code']}/{metric}）",
         due, escalation, now, now),
    )
    _task_event(db, cur.lastrowid, "created",
                f"高风险变化生成处置任务，处置期限 {due}，升级期限 {escalation}", now=now)
    return cur.lastrowid


# ---------------------------------------------------------------- 阶段结论

def run_assessment(db, tree_id, period_end, period_start=None, now=None):
    tree = _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    now = _now_or(now)
    rule_version, cfg = get_rule_set(db, now)

    if period_start is None:
        row = db.execute(
            "SELECT MAX(period_end) AS last_end FROM assessments WHERE tree_id=? AND status='confirmed'",
            (tree_id,),
        ).fetchone()
        period_start = row["last_end"] or tree["created_at"]
    period_start = iso(parse(period_start))
    period_end = iso(parse(period_end))
    if not period_start < period_end:
        raise DomainError("period_start 必须早于 period_end")
    if period_end > now:
        raise DomainError("不能对未来区间做阶段研判")

    conflicts = db.execute(
        "SELECT id FROM assessments WHERE tree_id=? AND status='confirmed'"
        " AND NOT (period_end <= ? OR period_start >= ?)",
        (tree_id, period_start, period_end),
    ).fetchall()
    if conflicts:
        raise DomainError(
            "研判区间与已确认的阶段结论重叠，已确认结论不可改写",
            status=409,
            payload={"conflicts": [r["id"] for r in conflicts]},
        )

    detail = {}
    states = []
    metrics = [r["metric"] for r in db.execute(
        "SELECT DISTINCT metric FROM devices WHERE tree_id=?", (tree_id,)).fetchall()]
    midpoint = iso(parse(period_start) + (parse(period_end) - parse(period_start)) / 2)
    for metric in metrics:
        rows = db.execute(
            "SELECT r.value, r.observed_at FROM readings r"
            " JOIN devices d ON d.id = r.device_id"
            " WHERE d.tree_id=? AND r.metric=? AND r.observed_at BETWEEN ? AND ?"
            " AND r.id NOT IN (SELECT reading_id FROM anomalies WHERE classification='sensor_drift')"
            " ORDER BY r.observed_at",
            (tree_id, metric, period_start, period_end),
        ).fetchall()
        if len(rows) < 2:
            detail[metric] = {"state": "insufficient_data", "samples": len(rows)}
            continue
        before = [r["value"] for r in rows if r["observed_at"] < midpoint]
        after = [r["value"] for r in rows if r["observed_at"] >= midpoint]
        if not before or not after:
            detail[metric] = {"state": "insufficient_data", "samples": len(rows)}
            continue
        delta = statistics.fmean(after) - statistics.fmean(before)
        state = _trend_state(metric, delta, cfg)
        detail[metric] = {
            "state": state,
            "samples": len(rows),
            "before_mean": round(statistics.fmean(before), 4),
            "after_mean": round(statistics.fmean(after), 4),
            "delta": round(delta, 4),
            "thresholds": cfg["trend"].get(metric),
        }
        states.append(state)

    if not states:
        trend = "insufficient_data"
    elif "declining" in states:
        trend = "declining"
    elif "improving" in states:
        trend = "improving"
    else:
        trend = "stable"

    cur = db.execute(
        "INSERT INTO assessments(tree_id, period_start, period_end, rule_version, trend, detail,"
        " status, created_at) VALUES (?,?,?,?,?,?, 'draft', ?)",
        (tree_id, period_start, period_end, rule_version, trend,
         json.dumps(detail, ensure_ascii=False), now),
    )
    return _assessment_dict(_one(db, "SELECT * FROM assessments WHERE id=?", (cur.lastrowid,)))


def _trend_state(metric, delta, cfg):
    t = cfg["trend"].get(metric)
    if not t:
        return "unknown"
    dec, imp = t.get("declining"), t.get("improving")
    if dec is not None and ((dec < 0 and delta <= dec) or (dec > 0 and delta >= dec)):
        return "declining"
    if imp is not None and ((imp < 0 and delta <= imp) or (imp > 0 and delta >= imp)):
        return "improving"
    return "stable"


def confirm_assessment(db, assessment_id, now=None):
    row = _one(db, "SELECT * FROM assessments WHERE id=?", (assessment_id,), "阶段结论")
    if row["status"] == "confirmed":
        raise DomainError("该阶段结论已确认，不可重复确认", status=409)
    ts = _now_or(now)
    db.execute("UPDATE assessments SET status='confirmed', confirmed_at=? WHERE id=?",
               (ts, assessment_id))
    return _assessment_dict(_one(db, "SELECT * FROM assessments WHERE id=?", (assessment_id,)))


def _assessment_dict(row):
    d = dict(row)
    d["detail"] = json.loads(d["detail"])
    return d


# ---------------------------------------------------------------- 告警生命周期

def resolve_alert(db, alert_id, reason, actor=None, now=None):
    if not reason:
        raise DomainError("解除告警必须填写原因")
    alert = _one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,), "告警")
    if alert["status"] in ("resolved", "merged"):
        raise DomainError(f"告警已处于 {alert['status']} 状态", status=409)
    ts = _now_or(now)
    db.execute("UPDATE alerts SET status='resolved', updated_at=? WHERE id=?", (ts, alert_id))
    _alert_event(db, alert_id, "resolved", reason, actor=actor, now=ts)
    # 解除告警同时关闭其未办结任务，留痕说明
    for task in db.execute(
            "SELECT id FROM tasks WHERE alert_id=? AND status != 'done'", (alert_id,)).fetchall():
        db.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?", (ts, task["id"]))
        _task_event(db, task["id"], "done", f"告警#{alert_id} 解除：{reason}", actor=actor, now=ts)
    return dict(_one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,)))


def merge_alert(db, alert_id, into_id, reason, actor=None, now=None):
    if not reason:
        raise DomainError("合并告警必须填写原因")
    alert = _one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,), "告警")
    target = _one(db, "SELECT * FROM alerts WHERE id=?", (into_id,), "目标告警")
    if target["status"] not in OPEN_ALERT_STATUSES:
        raise DomainError("只能合并到未结告警", status=409)
    ts = _now_or(now)
    db.execute("UPDATE alerts SET status='merged', merged_into=?, updated_at=? WHERE id=?",
               (into_id, ts, alert_id))
    _alert_event(db, alert_id, "merged", reason, actor=actor, detail={"into": into_id}, now=ts)
    _alert_event(db, into_id, "merged", f"合并告警#{alert_id}：{reason}",
                 actor=actor, detail={"from": alert_id}, now=ts)
    return dict(_one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,)))


def replay_alert(db, alert_id):
    alert = _one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,), "告警")
    events = [dict(r) for r in db.execute(
        "SELECT * FROM alert_events WHERE alert_id=? ORDER BY id", (alert_id,)).fetchall()]
    anomalies = []
    for r in db.execute(
            "SELECT a.* FROM anomalies a JOIN alert_anomalies x ON x.anomaly_id = a.id"
            " WHERE x.alert_id=? ORDER BY a.id", (alert_id,)).fetchall():
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"])
        anomalies.append(d)
    tasks = []
    for t in db.execute("SELECT * FROM tasks WHERE alert_id=? ORDER BY id", (alert_id,)).fetchall():
        td = dict(t)
        td["events"] = [dict(e) for e in db.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (t["id"],)).fetchall()]
        tasks.append(td)
    return {"alert": dict(alert), "events": events, "anomalies": anomalies, "tasks": tasks}


# ---------------------------------------------------------------- 处置任务

def claim_task(db, task_id, staff_id, now=None):
    task = _one(db, "SELECT * FROM tasks WHERE id=?", (task_id,), "任务")
    person = _one(db, "SELECT * FROM staff WHERE id=?", (staff_id,), "人员")
    if person["status"] != "active":
        raise DomainError("只有在职人员可以认领任务", status=409)
    if task["status"] != "open":
        raise DomainError(f"任务当前为 {task['status']} 状态，不可认领", status=409)
    ts = _now_or(now)
    db.execute("UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=?, updated_at=?"
               " WHERE id=?", (staff_id, ts, ts, task_id))
    _task_event(db, task_id, "claimed", f"{person['name']} 认领任务", actor=person["name"], now=ts)
    return dict(_one(db, "SELECT * FROM tasks WHERE id=?", (task_id,)))


def complete_task(db, task_id, note=None, actor=None, now=None):
    task = _one(db, "SELECT * FROM tasks WHERE id=?", (task_id,), "任务")
    if task["status"] == "done":
        raise DomainError("任务已办结", status=409)
    ts = _now_or(now)
    db.execute("UPDATE tasks SET status='done', updated_at=? WHERE id=?", (ts, task_id))
    _task_event(db, task_id, "done", note or "处置完成", actor=actor, now=ts)
    return dict(_one(db, "SELECT * FROM tasks WHERE id=?", (task_id,)))


def transfer_staff(db, staff_id, reason=None, now=None):
    """负责人调岗：其名下未办结任务退回待认领池，继续被追踪，不随人消失。"""
    person = _one(db, "SELECT * FROM staff WHERE id=?", (staff_id,), "人员")
    ts = _now_or(now)
    db.execute("UPDATE staff SET status='transferred' WHERE id=?", (staff_id,))
    released = []
    for task in db.execute(
            "SELECT * FROM tasks WHERE claimed_by=? AND status='claimed'", (staff_id,)).fetchall():
        db.execute("UPDATE tasks SET status='open', claimed_by=NULL, claimed_at=NULL,"
                   " updated_at=? WHERE id=?", (ts, task["id"]))
        why = f"负责人 {person['name']} 调岗，任务退回待认领池" + (f"：{reason}" if reason else "")
        _task_event(db, task["id"], "released", why, now=ts)
        _alert_event(db, task["alert_id"], "note",
                     f"任务#{task['id']} 因负责人调岗退回待认领，继续追踪", now=ts)
        released.append(task["id"])
    return {"staff_id": staff_id, "status": "transferred", "released_tasks": released}


def sweep_escalations(db, now=None):
    """扫描超期未办结任务：标记升级并提升告警级别。设备离线/无人认领均不影响扫描。"""
    ts = _now_or(now)
    escalated = []
    rows = db.execute(
        "SELECT * FROM tasks WHERE status != 'done' AND escalated = 0 AND escalation_deadline < ?",
        (ts,),
    ).fetchall()
    for task in rows:
        db.execute("UPDATE tasks SET escalated=1, updated_at=? WHERE id=?", (ts, task["id"]))
        _task_event(db, task["id"], "escalated",
                    f"超过升级期限 {task['escalation_deadline']} 仍未办结，自动升级", now=ts)
        alert = _one(db, "SELECT * FROM alerts WHERE id=?", (task["alert_id"],), "告警")
        if alert["status"] in OPEN_ALERT_STATUSES:
            idx = LEVELS.index(alert["level"])
            new_level = LEVELS[min(idx + 1, len(LEVELS) - 1)]
            db.execute("UPDATE alerts SET level=?, status='escalated', updated_at=? WHERE id=?",
                       (new_level, ts, alert["id"]))
            _alert_event(db, alert["id"], "escalated",
                         f"处置任务#{task['id']} 超期未办结，告警升级为 {new_level}",
                         detail={"task_id": task["id"]}, now=ts)
        escalated.append(task["id"])
    return {"escalated_tasks": escalated, "count": len(escalated)}


# ---------------------------------------------------------------- 时间线与措施窗口

def timeline(db, tree_id, frm=None, to=None, include_readings=False):
    tree = _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")
    frm = iso(parse(frm)) if frm else "0000-01-01T00:00:00Z"
    to = iso(parse(to)) if to else "9999-12-31T23:59:59Z"
    items = []

    def add(ts, type_, summary, ref_id, extra=None):
        if ts is None or not (frm <= ts <= to):
            return
        item = {"time": ts, "type": type_, "summary": summary, "ref_id": ref_id}
        if extra:
            item["detail"] = extra
        items.append(item)

    for r in db.execute(
            "SELECT c.*, d.metric, d.label FROM device_calibrations c"
            " JOIN devices d ON d.id = c.device_id WHERE d.tree_id=?", (tree_id,)).fetchall():
        add(r["calibrated_at"], "calibration",
            f"设备#{r['device_id']}（{r['metric']}）校准，偏差 {r['deviation']}", r["id"])
    for r in db.execute(
            "SELECT e.*, d.metric FROM device_events e JOIN devices d ON d.id = e.device_id"
            " WHERE d.tree_id=?", (tree_id,)).fetchall():
        add(r["created_at"], "device_event",
            f"设备#{r['device_id']}（{r['metric']}）状态 {r['from_status']} → {r['to_status']}"
            + (f"：{r['reason']}" if r["reason"] else ""), r["id"])
    for r in db.execute("SELECT * FROM inspections WHERE tree_id=?", (tree_id,)).fetchall():
        add(r["inspected_at"], "inspection",
            f"人工巡查（{r['category']}）严重度 {r['severity']}" + (f"：{r['finding']}" if r["finding"] else ""),
            r["id"])
    for r in db.execute("SELECT * FROM measures WHERE tree_id=?", (tree_id,)).fetchall():
        add(r["started_at"], "measure", f"养护措施开始：{r['measure_type']}", r["id"])
        if r["ended_at"]:
            add(r["ended_at"], "measure", f"养护措施结束：{r['measure_type']}", r["id"])
    for r in db.execute("SELECT * FROM weather_windows WHERE region=?", (tree["location"] or "",)).fetchall():
        if r["ended_at"] >= frm and r["started_at"] <= to:
            add(r["started_at"], "weather_window",
                f"气象窗口：{r['window_type']}（{r['started_at']} ~ {r['ended_at']}）", r["id"])
    for r in db.execute("SELECT * FROM assessments WHERE tree_id=?", (tree_id,)).fetchall():
        add(r["created_at"], "assessment",
            f"阶段研判[{r['period_start']} ~ {r['period_end']}] 趋势 {r['trend']}"
            f"（规则v{r['rule_version']}，{r['status']}）", r["id"])
    for r in db.execute("SELECT * FROM anomalies WHERE tree_id=?", (tree_id,)).fetchall():
        add(r["created_at"], "anomaly",
            f"异常读数（{r['metric']}）判定 {r['classification']}，可信度 {r['credibility']:.2f}", r["id"])
    for r in db.execute(
            "SELECT e.* FROM alert_events e JOIN alerts a ON a.id = e.alert_id"
            " WHERE a.tree_id=?", (tree_id,)).fetchall():
        add(r["created_at"], "alert_event", f"告警#{r['alert_id']} {r['event_type']}：{r['reason']}", r["id"])
    for r in db.execute(
            "SELECT e.* FROM task_events e JOIN tasks t ON t.id = e.task_id"
            " WHERE t.tree_id=?", (tree_id,)).fetchall():
        add(r["created_at"], "task_event", f"任务#{r['task_id']} {r['event_type']}：{r['reason']}", r["id"])
    if include_readings:
        for r in db.execute(
                "SELECT r.* FROM readings r JOIN devices d ON d.id = r.device_id"
                " WHERE d.tree_id=?", (tree_id,)).fetchall():
            add(r["observed_at"], "reading",
                f"{r['metric']}={r['value']}" + ("（补报）" if r["is_backfill"] else ""), r["id"])

    items.sort(key=lambda x: (x["time"], x["ref_id"]))
    reading_counts = {r["metric"]: r["c"] for r in db.execute(
        "SELECT d.metric, COUNT(*) AS c FROM readings r JOIN devices d ON d.id = r.device_id"
        " WHERE d.tree_id=? AND r.observed_at BETWEEN ? AND ? GROUP BY d.metric",
        (tree_id, frm, to)).fetchall()}
    return {"tree_id": tree_id, "from": frm, "to": to,
            "reading_counts": reading_counts, "items": items}


def measure_effectiveness(db, measure_id, window_days=14, metric=None):
    """比较措施前后等长有效窗口（剔除判定为传感器漂移的读数）。"""
    measure = _one(db, "SELECT * FROM measures WHERE id=?", (measure_id,), "养护措施")
    _, cfg = get_rule_set(db, iso(utcnow()))
    start = parse(measure["started_at"])
    before_start, before_end = iso(start - timedelta(days=window_days)), iso(start)
    after_start, after_end = iso(start), iso(start + timedelta(days=window_days))

    metrics = [metric] if metric else [r["metric"] for r in db.execute(
        "SELECT DISTINCT metric FROM devices WHERE tree_id=?", (measure["tree_id"],)).fetchall()]
    result = {}
    for m in metrics:
        def window_stats(frm, to):
            rows = db.execute(
                "SELECT r.value FROM readings r JOIN devices d ON d.id = r.device_id"
                " WHERE d.tree_id=? AND d.metric=? AND r.observed_at >= ? AND r.observed_at < ?"
                " AND r.id NOT IN (SELECT reading_id FROM anomalies"
                "                  WHERE classification='sensor_drift')",
                (measure["tree_id"], m, frm, to),
            ).fetchall()
            values = [r["value"] for r in rows]
            if not values:
                return {"samples": 0, "mean": None}
            return {"samples": len(values), "mean": round(statistics.fmean(values), 4),
                    "min": min(values), "max": max(values)}

        before = window_stats(before_start, before_end)
        after = window_stats(after_start, after_end)
        delta = (round(after["mean"] - before["mean"], 4)
                 if before["mean"] is not None and after["mean"] is not None else None)
        verdict = "insufficient_data" if delta is None else _effectiveness_verdict(m, delta, cfg)
        result[m] = {
            "before_window": {"from": before_start, "to": before_end, **before},
            "after_window": {"from": after_start, "to": after_end, **after},
            "delta": delta,
            "verdict": verdict,
        }
    return {"measure": dict(measure), "window_days": window_days, "metrics": result}


def _effectiveness_verdict(metric, delta, cfg):
    t = cfg["trend"].get(metric)
    if not t:
        return "no_significant_change"
    dec, imp = t.get("declining"), t.get("improving")
    if imp is not None and ((imp > 0 and delta >= imp) or (imp < 0 and delta <= imp)):
        return "effective"
    if dec is not None and ((dec < 0 and delta <= dec) or (dec > 0 and delta >= dec)):
        return "degraded"
    return "no_significant_change"
