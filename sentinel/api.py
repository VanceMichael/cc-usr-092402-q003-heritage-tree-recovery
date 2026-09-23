"""HTTP API：所有写操作在单事务内完成；时间敏感端点接受 now 便于回放与测试。"""
import json

from flask import Blueprint, current_app, g, jsonify, request

from . import core
from .db import connect

bp = Blueprint("api", __name__)


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def body():
    return request.get_json(silent=True) or {}


def ok(data, status=200):
    return jsonify(data), status


@bp.errorhandler(core.DomainError)
def domain_error(err):
    return jsonify({"error": err.message, **err.payload}), err.status


def commit():
    get_db().commit()


# ---------------------------------------------------------------- 档案

@bp.post("/trees")
def create_tree():
    b = body()
    row = core.create_tree(get_db(), b.get("code"), b.get("name"), b.get("species"),
                           b.get("location"), b.get("risk_level", "low"), b.get("now"))
    commit()
    return ok(dict(row), 201)


@bp.get("/trees")
def list_trees():
    rows = get_db().execute("SELECT * FROM trees ORDER BY id").fetchall()
    return ok([dict(r) for r in rows])


@bp.post("/staff")
def create_staff():
    b = body()
    row = core.create_staff(get_db(), b.get("name"), b.get("role", "technician"), b.get("now"))
    commit()
    return ok(dict(row), 201)


@bp.post("/staff/<int:staff_id>/transfer")
def transfer_staff(staff_id):
    b = body()
    result = core.transfer_staff(get_db(), staff_id, b.get("reason"), b.get("now"))
    commit()
    return ok(result)


@bp.post("/devices")
def create_device():
    b = body()
    row = core.create_device(get_db(), b.get("tree_id"), b.get("metric"),
                             b.get("label", ""), b.get("installed_at"), b.get("now"))
    commit()
    return ok(dict(row), 201)


@bp.post("/devices/<int:device_id>/status")
def device_status(device_id):
    b = body()
    row = core.set_device_status(get_db(), device_id, b.get("status"), b.get("reason"), b.get("now"))
    commit()
    return ok(dict(row))


@bp.post("/devices/<int:device_id>/calibrations")
def calibrate(device_id):
    b = body()
    row = core.calibrate_device(get_db(), device_id, b.get("calibrated_at"),
                                b.get("method"), b.get("deviation"), b.get("note"), b.get("now"))
    commit()
    return ok(dict(row), 201)


# ---------------------------------------------------------------- 数据流

@bp.post("/readings")
def ingest_reading():
    b = body()
    result = core.ingest_reading(get_db(), b.get("device_id"), b.get("value"),
                                 b.get("observed_at"), b.get("received_at"),
                                 b.get("source", "device"), b.get("now"))
    commit()
    return ok(result, 201)


@bp.post("/readings/batch")
def ingest_batch():
    items = body().get("readings", [])
    results = [core.ingest_reading(get_db(), r.get("device_id"), r.get("value"),
                                   r.get("observed_at"), r.get("received_at"),
                                   r.get("source", "device"), r.get("now"))
               for r in items]
    commit()
    return ok({"ingested": len(results),
               "anomalies": [r["anomaly"] for r in results if r["anomaly"]]}, 201)


@bp.post("/trees/<int:tree_id>/inspections")
def create_inspection(tree_id):
    b = body()
    row = core.create_inspection(get_db(), tree_id, b.get("inspected_at"), b.get("inspector_id"),
                                 b.get("category", "general"), b.get("severity", "none"),
                                 b.get("finding"), b.get("now"))
    commit()
    return ok(dict(row), 201)


@bp.post("/trees/<int:tree_id>/measures")
def create_measure(tree_id):
    b = body()
    row = core.create_measure(get_db(), tree_id, b.get("measure_type"), b.get("started_at"),
                              b.get("ended_at"), b.get("note"), b.get("now"))
    commit()
    return ok(dict(row), 201)


@bp.get("/measures/<int:measure_id>/effectiveness")
def effectiveness(measure_id):
    metric = request.args.get("metric")
    window_days = int(request.args.get("window_days", 14))
    return ok(core.measure_effectiveness(get_db(), measure_id, window_days, metric))


@bp.post("/weather-windows")
def create_weather_window():
    b = body()
    row = core.create_weather_window(get_db(), b.get("region"), b.get("window_type"),
                                     b.get("started_at"), b.get("ended_at"), b.get("note"))
    commit()
    return ok(dict(row), 201)


# ---------------------------------------------------------------- 规则与研判

@bp.post("/rule-sets")
def create_rule_set():
    b = body()
    result = core.create_rule_set(get_db(), b.get("effective_from"), b.get("config"), b.get("now"))
    commit()
    return ok(result, 201)


@bp.get("/rule-sets")
def list_rule_sets():
    core.ensure_rule_sets(get_db())
    rows = get_db().execute("SELECT * FROM rule_sets ORDER BY version").fetchall()
    return ok([{**dict(r), "config": json.loads(r["config"])} for r in rows])


@bp.post("/trees/<int:tree_id>/assessments")
def run_assessment(tree_id):
    b = body()
    result = core.run_assessment(get_db(), tree_id, b.get("period_end"),
                                 b.get("period_start"), b.get("now"))
    commit()
    return ok(result, 201)


@bp.post("/assessments/<int:assessment_id>/confirm")
def confirm_assessment(assessment_id):
    result = core.confirm_assessment(get_db(), assessment_id, body().get("now"))
    commit()
    return ok(result)


@bp.get("/trees/<int:tree_id>/assessments")
def list_assessments(tree_id):
    rows = get_db().execute(
        "SELECT * FROM assessments WHERE tree_id=? ORDER BY period_start", (tree_id,)).fetchall()
    return ok([{**dict(r), "detail": json.loads(r["detail"])} for r in rows])


@bp.get("/anomalies")
def list_anomalies():
    sql, args = "SELECT * FROM anomalies WHERE 1=1", []
    if request.args.get("tree_id"):
        sql += " AND tree_id=?"
        args.append(request.args["tree_id"])
    if request.args.get("classification"):
        sql += " AND classification=?"
        args.append(request.args["classification"])
    rows = get_db().execute(sql + " ORDER BY id", args).fetchall()
    return ok([{**dict(r), "evidence": json.loads(r["evidence"])} for r in rows])


@bp.get("/anomalies/<int:anomaly_id>")
def get_anomaly(anomaly_id):
    row = get_db().execute("SELECT * FROM anomalies WHERE id=?", (anomaly_id,)).fetchone()
    if row is None:
        return ok({"error": "异常不存在"}, 404)
    return ok({**dict(row), "evidence": json.loads(row["evidence"])})


# ---------------------------------------------------------------- 告警与任务

@bp.get("/alerts")
def list_alerts():
    sql, args = "SELECT * FROM alerts WHERE 1=1", []
    if request.args.get("tree_id"):
        sql += " AND tree_id=?"
        args.append(request.args["tree_id"])
    if request.args.get("status"):
        sql += " AND status=?"
        args.append(request.args["status"])
    rows = get_db().execute(sql + " ORDER BY id", args).fetchall()
    return ok([dict(r) for r in rows])


@bp.get("/alerts/<int:alert_id>/replay")
def replay_alert(alert_id):
    return ok(core.replay_alert(get_db(), alert_id))


@bp.post("/alerts/<int:alert_id>/resolve")
def resolve_alert(alert_id):
    b = body()
    result = core.resolve_alert(get_db(), alert_id, b.get("reason"), b.get("actor"), b.get("now"))
    commit()
    return ok(result)


@bp.post("/alerts/<int:alert_id>/merge")
def merge_alert(alert_id):
    b = body()
    result = core.merge_alert(get_db(), alert_id, b.get("into_id"), b.get("reason"),
                              b.get("actor"), b.get("now"))
    commit()
    return ok(result)


@bp.get("/tasks")
def list_tasks():
    sql, args = "SELECT * FROM tasks WHERE 1=1", []
    if request.args.get("status"):
        sql += " AND status=?"
        args.append(request.args["status"])
    if request.args.get("claimed_by"):
        sql += " AND claimed_by=?"
        args.append(request.args["claimed_by"])
    if request.args.get("overdue") == "1":
        from .util import iso, parse, utcnow
        now = iso(parse(request.args["now"])) if request.args.get("now") else iso(utcnow())
        sql += " AND status != 'done' AND escalation_deadline < ?"
        args.append(now)
    rows = get_db().execute(sql + " ORDER BY id", args).fetchall()
    return ok([dict(r) for r in rows])


@bp.post("/tasks/<int:task_id>/claim")
def claim_task(task_id):
    b = body()
    result = core.claim_task(get_db(), task_id, b.get("staff_id"), b.get("now"))
    commit()
    return ok(result)


@bp.post("/tasks/<int:task_id>/complete")
def complete_task(task_id):
    b = body()
    result = core.complete_task(get_db(), task_id, b.get("note"), b.get("actor"), b.get("now"))
    commit()
    return ok(result)


@bp.post("/escalations/run")
def run_escalations():
    result = core.sweep_escalations(get_db(), body().get("now"))
    commit()
    return ok(result)


# ---------------------------------------------------------------- 时间线

@bp.get("/trees/<int:tree_id>/timeline")
def tree_timeline(tree_id):
    include_readings = request.args.get("include_readings") == "1"
    return ok(core.timeline(get_db(), tree_id, request.args.get("from"),
                            request.args.get("to"), include_readings))
