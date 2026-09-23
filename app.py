"""古树恢复趋势研判闭环服务。

启动时按序应用 migrations/ 下的迁移脚本；SQLite 是唯一持久化介质。
"""
import glob
import os
import sqlite3

from flask import Flask, g, jsonify, request

import core

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def init_db(db_path):
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        applied = {r[0] for r in db.execute("SELECT version FROM schema_version")}
        for path in sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))):
            version = int(os.path.basename(path).split("_")[0])
            if version not in applied:
                db.executescript(open(path, encoding="utf-8").read())


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("DB_PATH", "data/app.db")
    init_db(app.config["DB_PATH"])

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.errorhandler(core.DomainError)
    def handle_domain_error(exc):
        return jsonify(error=str(exc)), exc.status

    def body():
        return request.get_json(force=True, silent=True) or {}

    def commit(result, status=200):
        get_db().commit()
        return jsonify(result), status

    # ------------------------------------------------ 基础
    @app.get("/health")
    def health():
        get_db().execute("SELECT 1")
        return jsonify(status="ok")

    # ------------------------------------------------ 档案登记
    @app.post("/trees")
    def create_tree():
        return commit(core.create_tree(get_db(), body()), 201)

    @app.post("/staff")
    def create_staff():
        return commit(core.create_staff(get_db(), body()), 201)

    @app.post("/staff/<staff_id>/deactivate")
    def deactivate_staff(staff_id):
        return commit(core.deactivate_staff(get_db(), staff_id, body()))

    @app.post("/devices")
    def create_device():
        return commit(core.create_device(get_db(), body()), 201)

    @app.post("/devices/<device_id>/status")
    def set_device_status(device_id):
        return commit(core.set_device_status(get_db(), device_id, body()))

    @app.post("/devices/<device_id>/calibrations")
    def add_calibration(device_id):
        return commit(core.add_calibration(get_db(), device_id, body()), 201)

    # ------------------------------------------------ 时间线事件录入
    @app.post("/readings")
    def ingest_reading():
        return commit(core.ingest_reading(get_db(), body()), 201)

    @app.post("/inspections")
    def add_inspection():
        return commit(core.add_inspection(get_db(), body()), 201)

    @app.post("/measures")
    def add_measure():
        return commit(core.add_measure(get_db(), body()), 201)

    @app.post("/weather-windows")
    def add_weather_window():
        return commit(core.add_weather_window(get_db(), body()), 201)

    @app.post("/rule-sets")
    def create_rule_set():
        return commit(core.create_rule_set(get_db(), body()), 201)

    # ------------------------------------------------ 研判
    @app.post("/trees/<tree_id>/assessments")
    def run_assessment(tree_id):
        return commit(core.run_assessment(get_db(), tree_id, body().get("now")), 201)

    @app.get("/trees/<tree_id>/assessments")
    def list_assessments(tree_id):
        return jsonify(core.list_assessments(get_db(), tree_id))

    @app.get("/trees/<tree_id>/timeline")
    def get_timeline(tree_id):
        return jsonify(core.timeline(get_db(), tree_id))

    @app.get("/trees/<tree_id>/risk")
    def get_tree_risk(tree_id):
        return jsonify(core.tree_risk(get_db(), tree_id))

    # ------------------------------------------------ 告警闭环
    @app.get("/alerts")
    def list_alerts():
        return jsonify(core.list_alerts(get_db(), request.args.get("tree_id"), request.args.get("status")))

    @app.post("/alerts/<int:alert_id>/claim")
    def claim_alert(alert_id):
        return commit(core.claim_alert(get_db(), alert_id, body()))

    @app.post("/alerts/<int:alert_id>/resolve")
    def resolve_alert(alert_id):
        return commit(core.resolve_alert(get_db(), alert_id, body()))

    @app.get("/alerts/<int:alert_id>/replay")
    def replay_alert(alert_id):
        return jsonify(core.alert_replay(get_db(), alert_id))

    @app.post("/ops/sweep")
    def sweep():
        return commit(core.sweep(get_db(), body().get("now")))

    # ------------------------------------------------ 措施有效窗口
    @app.get("/measures/<int:measure_id>/effectiveness")
    def get_effectiveness(measure_id):
        metric = request.args.get("metric")
        if not metric:
            return jsonify(error="缺少 metric 参数"), 400
        return jsonify(core.measure_effectiveness(get_db(), measure_id, metric, request.args.get("now")))

    return app


app = create_app()
