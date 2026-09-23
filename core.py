"""古树恢复趋势研判闭环的领域逻辑。

所有函数接收一个已开启外键的 sqlite3 连接（row_factory=sqlite3.Row）。
时间一律使用 UTC ISO8601 字符串（%Y-%m-%dT%H:%M:%SZ），可字典序直接比较。

核心不变量：
- assessments 表 committed=1 的行写入后永不修改；乱序补报（observed_at 早于
  最近一次已确认结论）只标记 late=1 并进入下一次研判的证据，不回写历史结论。
- 规则按 effective_from 生效：摄入与研判都取"当时有效"的规则版本。
- 告警的每次合并/升级/认领/转派/解除都在 alert_events 留痕，供回放。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
UNRESOLVED = ("open", "claimed")

DEFAULT_RULES = {
    "metrics": {
        "diameter": {"min": 0.0, "max": 500.0, "max_delta": 5.0},
        "moisture": {"min": 5.0, "max": 70.0, "max_delta": 20.0},
        "tilt": {"min": 0.0, "max": 20.0, "max_delta": 2.0},
        "pest": {"min": 0.0, "max": 100.0, "max_delta": 30.0},
    },
    "credibility": {
        "neighbor_window_hours": 24,
        "inspection_window_days": 7,
        "calibration_max_age_days": 90,
        "quarantine_threshold": 0.5,
        "offline_penalty": 0.4,
        "stale_calibration_penalty": 0.2,
        "neighbor_corroboration_bonus": 0.2,
        "neighbor_conflict_penalty": 0.3,
        "inspection_corroboration_bonus": 0.3,
    },
    "risk": {
        "metric_severity": {
            "tilt": "high",
            "pest": "high",
            "moisture": "medium",
            "diameter": "medium",
        },
        "inspection_severity": {"concern": "medium", "critical": "critical"},
        "multi_high_to_critical": True,
    },
    "escalation_hours": {"high": 72, "critical": 24},
    "effectiveness": {"window_days": 30, "exclude_weather_kinds": ["rain", "heatwave"]},
}


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 时间工具

def parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ts(s):
    """把任意 ISO 输入规范化为统一的 UTC 秒级格式。"""
    return fmt_ts(parse_ts(s))


def now_utc():
    return fmt_ts(datetime.now(timezone.utc))


def shift(ts, **kwargs):
    return fmt_ts(parse_ts(ts) + timedelta(**kwargs))


# ---------------------------------------------------------------- 基础校验

def _one(db, sql, args, what):
    row = db.execute(sql, args).fetchone()
    if row is None:
        raise DomainError(f"{what}不存在", 404)
    return row


def get_tree(db, tree_id):
    return _one(db, "SELECT * FROM trees WHERE id=?", (tree_id,), "古树")


def get_staff(db, staff_id):
    return _one(db, "SELECT * FROM staff WHERE id=?", (staff_id,), "人员")


def get_device(db, device_id):
    return _one(db, "SELECT * FROM devices WHERE id=?", (device_id,), "设备")


def get_alert(db, alert_id):
    return _one(db, "SELECT * FROM alerts WHERE id=?", (alert_id,), "告警")


# ---------------------------------------------------------------- 规则版本

def _merge_rules(custom):
    merged = json.loads(json.dumps(DEFAULT_RULES))
    for key, value in (custom or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def active_rules(db, at):
    """返回 at 时刻生效的 (version, rules)；无规则集时用内置默认。"""
    row = db.execute(
        "SELECT version, rules FROM rule_sets WHERE effective_from <= ? "
        "ORDER BY effective_from DESC, id DESC LIMIT 1",
        (norm_ts(at),),
    ).fetchone()
    if row is None:
        return "builtin", _merge_rules({})
    return row["version"], _merge_rules(json.loads(row["rules"]))


def create_rule_set(db, body):
    version = body.get("version")
    effective_from = body.get("effective_from")
    if not version or not effective_from:
        raise DomainError("规则集需要 version 与 effective_from")
    rules = body.get("rules") or {}
    if not isinstance(rules, dict):
        raise DomainError("rules 必须是对象")
    try:
        db.execute(
            "INSERT INTO rule_sets(version, effective_from, rules, created_at) VALUES (?,?,?,?)",
            (version, norm_ts(effective_from), json.dumps(rules, ensure_ascii=False), now_utc()),
        )
    except Exception as exc:
        raise DomainError(f"规则版本已存在或非法: {exc}", 409)
    return {"version": version, "effective_from": norm_ts(effective_from)}


# ---------------------------------------------------------------- 档案登记

def create_tree(db, body):
    if not body.get("id") or not body.get("name"):
        raise DomainError("古树需要 id 与 name")
    db.execute(
        "INSERT INTO trees(id, name, species, location) VALUES (?,?,?,?)",
        (body["id"], body["name"], body.get("species"), body.get("location")),
    )
    return dict(get_tree(db, body["id"]))


def create_staff(db, body):
    if not body.get("id") or not body.get("name"):
        raise DomainError("人员需要 id 与 name")
    db.execute(
        "INSERT INTO staff(id, name, role, active) VALUES (?,?,?,1)",
        (body["id"], body["name"], body.get("role", "technician")),
    )
    return dict(get_staff(db, body["id"]))


def create_device(db, body):
    if not body.get("id") or not body.get("metric"):
        raise DomainError("设备需要 id 与 metric")
    get_tree(db, body.get("tree_id"))
    installed_at = norm_ts(body.get("installed_at") or now_utc())
    db.execute(
        "INSERT INTO devices(id, tree_id, metric, status, installed_at) VALUES (?,?,?,'online',?)",
        (body["id"], body["tree_id"], body["metric"], installed_at),
    )
    return dict(get_device(db, body["id"]))


def set_device_status(db, device_id, body):
    device = get_device(db, device_id)
    status = body.get("status")
    if status not in ("online", "offline"):
        raise DomainError("status 只能是 online|offline")
    db.execute("UPDATE devices SET status=? WHERE id=?", (status, device_id))
    return dict(get_device(db, device_id))


def add_calibration(db, device_id, body):
    get_device(db, device_id)
    calibrated_at = norm_ts(body.get("calibrated_at") or now_utc())
    cur = db.execute(
        "INSERT INTO device_calibrations(device_id, calibrated_at, drift, note) VALUES (?,?,?,?)",
        (device_id, calibrated_at, body.get("drift"), body.get("note")),
    )
    return {"id": cur.lastrowid, "device_id": device_id, "calibrated_at": calibrated_at}


def add_inspection(db, body):
    get_tree(db, body.get("tree_id"))
    finding = body.get("finding")
    if finding not in ("normal", "concern", "critical"):
        raise DomainError("finding 只能是 normal|concern|critical")
    inspected_at = norm_ts(body.get("inspected_at") or now_utc())
    cur = db.execute(
        "INSERT INTO inspections(tree_id, inspector_id, inspected_at, finding, note) VALUES (?,?,?,?,?)",
        (body["tree_id"], body.get("inspector_id"), inspected_at, finding, body.get("note")),
    )
    return {"id": cur.lastrowid, "tree_id": body["tree_id"], "inspected_at": inspected_at, "finding": finding}


def add_measure(db, body):
    get_tree(db, body.get("tree_id"))
    if not body.get("measure_type"):
        raise DomainError("措施需要 measure_type")
    applied_at = norm_ts(body.get("applied_at") or now_utc())
    cur = db.execute(
        "INSERT INTO measures(tree_id, measure_type, applied_at, applied_by, note) VALUES (?,?,?,?,?)",
        (body["tree_id"], body["measure_type"], applied_at, body.get("applied_by"), body.get("note")),
    )
    return {"id": cur.lastrowid, "tree_id": body["tree_id"], "applied_at": applied_at}


def add_weather_window(db, body):
    get_tree(db, body.get("tree_id"))
    if not body.get("kind") or not body.get("started_at"):
        raise DomainError("气象窗口需要 kind 与 started_at")
    cur = db.execute(
        "INSERT INTO weather_windows(tree_id, kind, started_at, ended_at, severity) VALUES (?,?,?,?,?)",
        (
            body["tree_id"],
            body["kind"],
            norm_ts(body["started_at"]),
            norm_ts(body["ended_at"]) if body.get("ended_at") else None,
            body.get("severity", "moderate"),
        ),
    )
    return {"id": cur.lastrowid, "tree_id": body["tree_id"], "kind": body["kind"]}


# ---------------------------------------------------------------- 读数摄入

def _detect_anomaly(db, device, value, observed_at, rules):
    """越界或与同设备最近一次有效读数突变即为异常。"""
    spec = rules["metrics"].get(device["metric"])
    if spec is None:
        return False, []
    reasons = []
    if value < spec["min"] or value > spec["max"]:
        reasons.append(f"读数{value}超出正常区间[{spec['min']},{spec['max']}]")
    last = db.execute(
        "SELECT value, observed_at FROM readings WHERE device_id=? "
        "AND quarantined=0 AND observed_at<? ORDER BY observed_at DESC, id DESC LIMIT 1",
        (device["id"], observed_at),
    ).fetchone()
    if last is not None and abs(value - last["value"]) > spec["max_delta"]:
        reasons.append(
            f"与上次有效读数{last['value']}的突变{abs(value - last['value']):.2f}超过max_delta={spec['max_delta']}"
        )
    return (len(reasons) > 0), reasons


def _compute_credibility(db, device, metric, value, observed_at, is_anomaly, rules):
    """异常读数的可信度：结合设备状态、校准时效、邻近设备与人工巡查证据。"""
    c = rules["credibility"]
    score = 1.0
    reasons = []
    if device["status"] != "online":
        score -= c["offline_penalty"]
        reasons.append("设备离线，读数可能来自缓存或漂移")
    cal = db.execute(
        "SELECT calibrated_at FROM device_calibrations WHERE device_id=? AND calibrated_at<=? "
        "ORDER BY calibrated_at DESC LIMIT 1",
        (device["id"], observed_at),
    ).fetchone()
    if cal is None:
        score -= c["stale_calibration_penalty"]
        reasons.append("设备无校准记录")
    else:
        age_days = (parse_ts(observed_at) - parse_ts(cal["calibrated_at"])).days
        if age_days > c["calibration_max_age_days"]:
            score -= c["stale_calibration_penalty"]
            reasons.append(f"校准已过期（{age_days}天前）")
    if is_anomaly:
        win = c["neighbor_window_hours"]
        lo, hi = shift(observed_at, hours=-win), shift(observed_at, hours=win)
        neighbors = db.execute(
            "SELECT r.value FROM readings r JOIN devices d ON d.id=r.device_id "
            "WHERE r.tree_id=? AND r.metric=? AND r.device_id!=? AND r.quarantined=0 "
            "AND r.observed_at BETWEEN ? AND ?",
            (device["tree_id"], metric, device["id"], lo, hi),
        ).fetchall()
        if neighbors:
            max_delta = rules["metrics"][metric]["max_delta"]
            if any(abs(n["value"] - value) <= max_delta for n in neighbors):
                score += c["neighbor_corroboration_bonus"]
                reasons.append("邻近设备读数一致，异常可信")
            else:
                score -= c["neighbor_conflict_penalty"]
                reasons.append("邻近设备读数正常，疑似传感器漂移")
        iwin = c["inspection_window_days"]
        ilo, ihi = shift(observed_at, days=-iwin), shift(observed_at, days=iwin)
        insp = db.execute(
            "SELECT id FROM inspections WHERE tree_id=? AND finding!='normal' "
            "AND inspected_at BETWEEN ? AND ?",
            (device["tree_id"], ilo, ihi),
        ).fetchall()
        if insp:
            score += c["inspection_corroboration_bonus"]
            reasons.append("人工巡查证据支持该异常")
    return max(0.0, min(1.0, score)), reasons


def ingest_reading(db, body, now=None):
    device = get_device(db, body.get("device_id"))
    if body.get("value") is None or body.get("observed_at") is None:
        raise DomainError("读数需要 value 与 observed_at")
    value = float(body["value"])
    observed_at = norm_ts(body["observed_at"])
    reported_at = norm_ts(body.get("reported_at") or now or now_utc())
    # 规则按入库时刻生效：升级只影响生效后的研判
    rule_version, rules = active_rules(db, reported_at)
    metric, tree_id = device["metric"], device["tree_id"]

    is_anomaly, anomaly_reasons = _detect_anomaly(db, device, value, observed_at, rules)
    credibility, cred_reasons = _compute_credibility(db, device, metric, value, observed_at, is_anomaly, rules)
    quarantined = 1 if (is_anomaly and credibility < rules["credibility"]["quarantine_threshold"]) else 0

    last_committed = db.execute(
        "SELECT assessed_at FROM assessments WHERE tree_id=? AND committed=1 "
        "ORDER BY assessed_at DESC, id DESC LIMIT 1",
        (tree_id,),
    ).fetchone()
    late = 1 if (last_committed and observed_at < last_committed["assessed_at"]) else 0

    cur = db.execute(
        "INSERT INTO readings(device_id, tree_id, metric, value, observed_at, reported_at, "
        "is_anomaly, credibility, quarantined, late, credibility_reasons) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            device["id"], tree_id, metric, value, observed_at, reported_at,
            int(is_anomaly), credibility, quarantined, late,
            json.dumps(anomaly_reasons + cred_reasons, ensure_ascii=False),
        ),
    )
    return {
        "id": cur.lastrowid,
        "tree_id": tree_id,
        "metric": metric,
        "value": value,
        "observed_at": observed_at,
        "reported_at": reported_at,
        "is_anomaly": bool(is_anomaly),
        "credibility": credibility,
        "quarantined": bool(quarantined),
        "late": bool(late),
        "rule_version": rule_version,
        "reasons": anomaly_reasons + cred_reasons,
    }


# ---------------------------------------------------------------- 研判

def run_assessment(db, tree_id, now=None):
    """提交一次阶段结论。只消费上次结论之后入库的读数（含乱序补报）。"""
    get_tree(db, tree_id)
    now = norm_ts(now or now_utc())
    rule_version, rules = active_rules(db, now)

    prev = db.execute(
        "SELECT * FROM assessments WHERE tree_id=? AND committed=1 "
        "ORDER BY assessed_at DESC, id DESC LIMIT 1",
        (tree_id,),
    ).fetchone()
    since = prev["assessed_at"] if prev else "0000-01-01T00:00:00Z"

    readings = db.execute(
        "SELECT * FROM readings WHERE tree_id=? AND reported_at>? AND reported_at<=? "
        "ORDER BY observed_at, id",
        (tree_id, since, now),
    ).fetchall()
    accepted = [r for r in readings if not r["quarantined"]]
    anomalies = [r for r in accepted if r["is_anomaly"]]
    late_ids = [r["id"] for r in readings if r["late"]]
    quarantined_ids = [r["id"] for r in readings if r["quarantined"]]

    inspections = db.execute(
        "SELECT * FROM inspections WHERE tree_id=? AND inspected_at>? AND inspected_at<=? "
        "ORDER BY inspected_at, id",
        (tree_id, since, now),
    ).fetchall()

    signals = []
    for r in anomalies:
        sev = rules["risk"]["metric_severity"].get(r["metric"], "medium")
        signals.append({"severity": sev, "source": r["metric"], "reading_id": r["id"]})
    for i in inspections:
        if i["finding"] != "normal":
            sev = rules["risk"]["inspection_severity"].get(i["finding"], "medium")
            signals.append({"severity": sev, "source": "inspection", "inspection_id": i["id"]})

    risk = "low"
    if signals:
        risk = max(signals, key=lambda s: SEVERITY_ORDER[s["severity"]])["severity"]
        high_sources = {s["source"] for s in signals if SEVERITY_ORDER[s["severity"]] >= SEVERITY_ORDER["high"]}
        if rules["risk"].get("multi_high_to_critical") and len(high_sources) >= 2:
            risk = "critical"

    prev_risk = prev["risk_level"] if prev else None
    if SEVERITY_ORDER[risk] >= SEVERITY_ORDER["high"]:
        phase = "declining"
    elif prev_risk and SEVERITY_ORDER[prev_risk] >= SEVERITY_ORDER["high"] and SEVERITY_ORDER[risk] < SEVERITY_ORDER["high"]:
        phase = "improving"
    else:
        phase = "stable"

    evidence = {
        "reading_ids": [r["id"] for r in accepted],
        "late_reading_ids": late_ids,
        "quarantined_reading_ids": quarantined_ids,
        "inspection_ids": [i["id"] for i in inspections],
        "signals": signals,
    }
    cur = db.execute(
        "INSERT INTO assessments(tree_id, assessed_at, rule_version, risk_level, phase, committed, evidence, supersedes) "
        "VALUES (?,?,?,?,?,1,?,?)",
        (tree_id, now, rule_version, risk, phase, json.dumps(evidence, ensure_ascii=False),
         prev["id"] if prev else None),
    )
    assessment_id = cur.lastrowid
    alert_outcome = _update_alerts(db, tree_id, risk, signals, rules, now, assessment_id)
    return {
        "id": assessment_id,
        "tree_id": tree_id,
        "assessed_at": now,
        "rule_version": rule_version,
        "risk_level": risk,
        "phase": phase,
        "committed": True,
        "supersedes": prev["id"] if prev else None,
        "evidence": evidence,
        "alert": alert_outcome,
    }


def _add_event(db, alert_id, event_type, reason, at, actor_id=None, detail=None):
    db.execute(
        "INSERT INTO alert_events(alert_id, event_type, actor_id, reason, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (alert_id, event_type, actor_id, reason, json.dumps(detail or {}, ensure_ascii=False), at),
    )


def _update_alerts(db, tree_id, risk, signals, rules, now, assessment_id):
    """高风险生成或合并告警；风险回落则解除未决告警。全部留痕。"""
    if SEVERITY_ORDER[risk] >= SEVERITY_ORDER["high"]:
        dominant = max(signals, key=lambda s: SEVERITY_ORDER[s["severity"]])
        risk_type = dominant["source"]
        open_alert = db.execute(
            "SELECT * FROM alerts WHERE tree_id=? AND risk_type=? AND status IN ('open','claimed') "
            "ORDER BY id DESC LIMIT 1",
            (tree_id, risk_type),
        ).fetchone()
        if open_alert is not None:
            _add_event(
                db, open_alert["id"], "merged",
                f"研判#{assessment_id}再次确认{risk_type}风险，并入既有告警而非新建",
                now, detail={"assessment_id": assessment_id},
            )
            if SEVERITY_ORDER[risk] > SEVERITY_ORDER[open_alert["severity"]]:
                due = shift(now, hours=rules["escalation_hours"][risk])
                db.execute("UPDATE alerts SET severity=?, due_at=? WHERE id=?", (risk, due, open_alert["id"]))
                _add_event(
                    db, open_alert["id"], "escalated",
                    f"研判#{assessment_id}显示风险由{open_alert['severity']}上升至{risk}，升级期限重置",
                    now, detail={"assessment_id": assessment_id},
                )
            return {"action": "merged", "alert_id": open_alert["id"]}
        due = shift(now, hours=rules["escalation_hours"][risk])
        cur = db.execute(
            "INSERT INTO alerts(tree_id, risk_type, severity, status, created_at, due_at) "
            "VALUES (?,?,?,'open',?,?)",
            (tree_id, risk_type, risk, now, due),
        )
        _add_event(
            db, cur.lastrowid, "created",
            f"研判#{assessment_id}判定{risk}级风险（{risk_type}），生成处置告警，升级期限{due}",
            now, detail={"assessment_id": assessment_id},
        )
        return {"action": "created", "alert_id": cur.lastrowid, "due_at": due}

    resolved = []
    for a in db.execute(
        "SELECT * FROM alerts WHERE tree_id=? AND status IN ('open','claimed')", (tree_id,)
    ).fetchall():
        db.execute("UPDATE alerts SET status='resolved', resolved_at=? WHERE id=?", (now, a["id"]))
        _add_event(
            db, a["id"], "resolved",
            f"研判#{assessment_id}显示风险回落至{risk}，证据充分，自动解除",
            now, detail={"assessment_id": assessment_id},
        )
        resolved.append(a["id"])
    return {"action": "resolved", "alert_ids": resolved} if resolved else {"action": "none"}


def list_assessments(db, tree_id):
    get_tree(db, tree_id)
    rows = db.execute(
        "SELECT * FROM assessments WHERE tree_id=? ORDER BY assessed_at, id", (tree_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"])
        out.append(d)
    return out


# ---------------------------------------------------------------- 告警处置

def claim_alert(db, alert_id, body, now=None):
    alert = get_alert(db, alert_id)
    if alert["status"] == "resolved":
        raise DomainError("告警已解除，不能认领", 409)
    staff = get_staff(db, body.get("staff_id"))
    if not staff["active"]:
        raise DomainError("该人员已调岗，不能认领", 409)
    now = norm_ts(now or body.get("now") or now_utc())
    db.execute("UPDATE alerts SET owner_id=?, status='claimed' WHERE id=?", (staff["id"], alert_id))
    _add_event(db, alert_id, "claimed", f"{staff['name']}认领处置", now, actor_id=staff["id"])
    return dict(get_alert(db, alert_id))


def resolve_alert(db, alert_id, body, now=None):
    alert = get_alert(db, alert_id)
    if alert["status"] == "resolved":
        raise DomainError("告警已解除", 409)
    note = body.get("note")
    if not note:
        raise DomainError("解除告警必须填写处置结论（note）")
    now = norm_ts(now or body.get("now") or now_utc())
    actor = body.get("staff_id")
    db.execute("UPDATE alerts SET status='resolved', resolved_at=? WHERE id=?", (now, alert_id))
    _add_event(db, alert_id, "resolved", note, now, actor_id=actor)
    return dict(get_alert(db, alert_id))


def sweep(db, now=None):
    """期限扫描：逾期未处置升级；设备全部离线转入降级追踪（告警不自动解除）。"""
    now = norm_ts(now or now_utc())
    _, rules = active_rules(db, now)
    escalated, degraded, restored = [], [], []
    unresolved = db.execute(
        "SELECT * FROM alerts WHERE status IN ('open','claimed') ORDER BY id"
    ).fetchall()
    for a in unresolved:
        if a["due_at"] < now:
            new_sev = "critical"
            due = shift(now, hours=rules["escalation_hours"][new_sev])
            db.execute("UPDATE alerts SET severity=?, due_at=? WHERE id=?", (new_sev, due, a["id"]))
            why = "已认领但未办结" if a["status"] == "claimed" else "无人认领"
            _add_event(
                db, a["id"], "escalated",
                f"超过升级期限{a['due_at']}（{why}），级别提升为{new_sev}，新期限{due}",
                now,
            )
            escalated.append(a["id"])
        devices = db.execute("SELECT status FROM devices WHERE tree_id=?", (a["tree_id"],)).fetchall()
        if devices and all(d["status"] == "offline" for d in devices):
            has_degraded = db.execute(
                "SELECT 1 FROM alert_events WHERE alert_id=? AND event_type='tracking_degraded'", (a["id"],)
            ).fetchone()
            if not has_degraded:
                _add_event(db, a["id"], "tracking_degraded", "设备全部离线，转入降级追踪，告警保持有效", now)
                degraded.append(a["id"])
        else:
            last_deg = db.execute(
                "SELECT id FROM alert_events WHERE alert_id=? AND event_type='tracking_degraded' "
                "ORDER BY id DESC LIMIT 1", (a["id"],)
            ).fetchone()
            if last_deg:
                has_restored = db.execute(
                    "SELECT 1 FROM alert_events WHERE alert_id=? AND event_type='tracking_restored' AND id>?",
                    (a["id"], last_deg["id"]),
                ).fetchone()
                if not has_restored:
                    _add_event(db, a["id"], "tracking_restored", "设备恢复在线，恢复正常追踪", now)
                    restored.append(a["id"])
    return {"now": now, "escalated": escalated, "tracking_degraded": degraded, "tracking_restored": restored}


def deactivate_staff(db, staff_id, body, now=None):
    """调岗：未决告警转交给指定人员或回到班组队列，追踪不中断。"""
    staff = get_staff(db, staff_id)
    now = norm_ts(now or body.get("now") or now_utc())
    reassign_to = body.get("reassign_to")
    if reassign_to:
        target = get_staff(db, reassign_to)
        if not target["active"]:
            raise DomainError("转交对象已调岗", 409)
    db.execute("UPDATE staff SET active=0 WHERE id=?", (staff_id,))
    moved = []
    for a in db.execute(
        "SELECT * FROM alerts WHERE owner_id=? AND status IN ('open','claimed')", (staff_id,)
    ).fetchall():
        new_status = "claimed" if reassign_to else "open"
        db.execute("UPDATE alerts SET owner_id=?, status=? WHERE id=?", (reassign_to, new_status, a["id"]))
        reason = f"负责人{staff['name']}调岗，告警转交{target['name']}" if reassign_to else \
                 f"负责人{staff['name']}调岗，告警回到班组队列待认领"
        _add_event(db, a["id"], "reassigned", reason, now, actor_id=staff_id)
        moved.append(a["id"])
    return {"deactivated": staff_id, "reassigned_alerts": moved}


def list_alerts(db, tree_id=None, status=None):
    sql, args = "SELECT * FROM alerts WHERE 1=1", []
    if tree_id:
        sql += " AND tree_id=?"
        args.append(tree_id)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY id"
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def alert_replay(db, alert_id):
    alert = dict(get_alert(db, alert_id))
    events = db.execute(
        "SELECT * FROM alert_events WHERE alert_id=? ORDER BY id", (alert_id,)
    ).fetchall()
    return {
        "alert": alert,
        "events": [
            {**dict(e), "detail": json.loads(e["detail"])} for e in events
        ],
    }


# ---------------------------------------------------------------- 措施有效窗口

def measure_effectiveness(db, measure_id, metric, now=None):
    """比较措施前后各一个窗口内的有效读数（剔除被隔离与气象窗口内的读数）。"""
    measure = _one(db, "SELECT * FROM measures WHERE id=?", (measure_id,), "养护措施")
    now = norm_ts(now or now_utc())
    _, rules = active_rules(db, now)
    eff = rules["effectiveness"]
    window = eff["window_days"]
    applied = measure["applied_at"]
    before_lo, before_hi = shift(applied, days=-window), applied
    after_lo, after_hi = applied, shift(applied, days=window)

    weather = db.execute(
        "SELECT kind, started_at, ended_at FROM weather_windows WHERE tree_id=?", (measure["tree_id"],)
    ).fetchall()
    exclude_kinds = set(eff["exclude_weather_kinds"])

    def in_excluded_weather(ts):
        for w in weather:
            if w["kind"] in exclude_kinds and w["started_at"] <= ts <= (w["ended_at"] or "9999"):
                return True
        return False

    def window_stats(lo, hi):
        rows = db.execute(
            "SELECT value, observed_at FROM readings WHERE tree_id=? AND metric=? AND quarantined=0 "
            "AND observed_at>=? AND observed_at<? ORDER BY observed_at",
            (measure["tree_id"], metric, lo, hi),
        ).fetchall()
        kept = [r for r in rows if not in_excluded_weather(r["observed_at"])]
        excluded = len(rows) - len(kept)
        values = [r["value"] for r in kept]
        return {
            "from": lo, "to": hi,
            "count": len(values),
            "excluded_weather": excluded,
            "mean": (sum(values) / len(values)) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
        }

    before = window_stats(before_lo, before_hi)
    after = window_stats(after_lo, after_hi)

    spec = rules["metrics"].get(metric, {"min": 0.0, "max": 0.0})
    mid = (spec["min"] + spec["max"]) / 2
    if before["count"] < 2 or after["count"] < 2:
        verdict = "insufficient_data"
    elif abs(after["mean"] - mid) < abs(before["mean"] - mid):
        verdict = "effective"
    else:
        verdict = "ineffective"

    return {
        "measure_id": measure["id"],
        "tree_id": measure["tree_id"],
        "measure_type": measure["measure_type"],
        "applied_at": applied,
        "metric": metric,
        "window_days": window,
        "before": before,
        "after": after,
        "verdict": verdict,
    }


# ---------------------------------------------------------------- 统一时间线

def timeline(db, tree_id):
    get_tree(db, tree_id)
    items = []

    for c in db.execute(
        "SELECT dc.*, d.metric FROM device_calibrations dc JOIN devices d ON d.id=dc.device_id "
        "WHERE d.tree_id=?", (tree_id,)
    ).fetchall():
        items.append({
            "time": c["calibrated_at"], "kind": "calibration",
            "summary": f"设备{c['device_id']}（{c['metric']}）完成校准",
            "ref_id": c["id"],
        })
    for r in db.execute(
        "SELECT * FROM readings WHERE tree_id=? AND (is_anomaly=1 OR late=1 OR quarantined=1)", (tree_id,)
    ).fetchall():
        if r["quarantined"]:
            kind, note = "reading_quarantined", "异常读数可信度不足被隔离"
        elif r["late"]:
            kind, note = "late_report", "乱序补报，仅进入后续研判，不改写已确认结论"
        else:
            kind, note = "reading_anomaly", "异常读数"
        items.append({
            "time": r["observed_at"], "kind": kind,
            "summary": f"{note}：{r['metric']}={r['value']}（可信度{r['credibility']:.2f}）",
            "ref_id": r["id"],
        })
    for i in db.execute("SELECT * FROM inspections WHERE tree_id=?", (tree_id,)).fetchall():
        items.append({
            "time": i["inspected_at"], "kind": "inspection",
            "summary": f"人工巡查结论：{i['finding']}", "ref_id": i["id"],
        })
    for m in db.execute("SELECT * FROM measures WHERE tree_id=?", (tree_id,)).fetchall():
        items.append({
            "time": m["applied_at"], "kind": "measure",
            "summary": f"实施养护措施：{m['measure_type']}", "ref_id": m["id"],
        })
    for w in db.execute("SELECT * FROM weather_windows WHERE tree_id=?", (tree_id,)).fetchall():
        items.append({
            "time": w["started_at"], "kind": "weather_window",
            "summary": f"气象窗口：{w['kind']}（{w['severity']}）", "ref_id": w["id"],
        })
    for a in db.execute("SELECT * FROM assessments WHERE tree_id=?", (tree_id,)).fetchall():
        items.append({
            "time": a["assessed_at"], "kind": "assessment",
            "summary": f"阶段结论：{a['phase']}，风险{a['risk_level']}（规则{a['rule_version']}）",
            "ref_id": a["id"],
        })
    for e in db.execute(
        "SELECT e.* FROM alert_events e JOIN alerts a ON a.id=e.alert_id WHERE a.tree_id=?",
        (tree_id,),
    ).fetchall():
        items.append({
            "time": e["created_at"], "kind": "alert_event",
            "summary": f"告警#{e['alert_id']} {e['event_type']}：{e['reason']}",
            "ref_id": e["id"],
        })

    items.sort(key=lambda x: (x["time"], x["kind"], x["ref_id"]))
    return items


def tree_risk(db, tree_id):
    get_tree(db, tree_id)
    latest = db.execute(
        "SELECT * FROM assessments WHERE tree_id=? AND committed=1 "
        "ORDER BY assessed_at DESC, id DESC LIMIT 1", (tree_id,)
    ).fetchone()
    open_alerts = db.execute(
        "SELECT * FROM alerts WHERE tree_id=? AND status IN ('open','claimed') ORDER BY id", (tree_id,)
    ).fetchall()
    return {
        "tree_id": tree_id,
        "latest_assessment": dict(latest) if latest else None,
        "open_alerts": [dict(a) for a in open_alerts],
    }
