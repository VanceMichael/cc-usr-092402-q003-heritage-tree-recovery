import pytest

from sentinel import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=tmp_path / "test.db")
    app.config.update(TESTING=True)
    return app.test_client()


def post(client, url, payload):
    resp = client.post(url, json=payload)
    assert resp.status_code < 500, resp.get_json()
    return resp


def make_tree(client, code, location="东区", now="2026-01-01T00:00:00Z"):
    return post(client, "/trees", {"code": code, "name": code, "location": location,
                                   "now": now}).get_json()["id"]


def make_device(client, tree_id, metric="moisture", calibrated_at=None, now="2026-01-01T00:00:00Z"):
    dev = post(client, "/devices", {"tree_id": tree_id, "metric": metric,
                                    "now": now}).get_json()
    if calibrated_at:
        post(client, f"/devices/{dev['id']}/calibrations",
             {"calibrated_at": calibrated_at, "method": "standard", "deviation": 0.1})
    return dev["id"]


def feed(client, device_id, dates, value):
    for d in dates:
        post(client, "/readings", {"device_id": device_id, "value": value, "observed_at": d})


def day_range(month, days, hour=0):
    return [f"2026-{month:02d}-{d:02d}T{hour:02d}:00:00Z" for d in days]


# ---------------------------------------------------------------- 健康检查

def test_health(client):
    assert client.get("/health").get_json()["status"] == "ok"


# ---------------------------------------------------------------- 1. 乱序补报不改写已确认结论

def test_backfill_does_not_rewrite_confirmed_conclusion(client):
    tree = make_tree(client, "G001")
    dev = make_device(client, tree)
    feed(client, dev, day_range(1, range(2, 11)), 40.0)
    feed(client, dev, day_range(1, range(20, 29)), 40.0)

    resp = post(client, f"/trees/{tree}/assessments",
                {"period_start": "2026-01-01T00:00:00Z", "period_end": "2026-01-31T00:00:00Z",
                 "now": "2026-02-01T00:00:00Z"})
    assert resp.status_code == 201
    assessment = resp.get_json()
    assert assessment["trend"] == "stable"
    post(client, f"/assessments/{assessment['id']}/confirm", {"now": "2026-02-01T01:00:00Z"})

    # 乱序补报：采集时刻落在已确认区间内
    resp = post(client, "/readings",
                {"device_id": dev, "value": 99.0, "observed_at": "2026-01-15T00:00:00Z",
                 "now": "2026-02-02T00:00:00Z"})
    assert resp.status_code == 201
    assert resp.get_json()["reading"]["is_backfill"] == 1

    # 已确认结论保持不变
    assessments = client.get(f"/trees/{tree}/assessments").get_json()
    assert len(assessments) == 1
    assert assessments[0]["trend"] == "stable"
    assert assessments[0]["status"] == "confirmed"

    # 与已确认区间重叠的研判被拒绝
    resp = post(client, f"/trees/{tree}/assessments",
                {"period_start": "2026-01-10T00:00:00Z", "period_end": "2026-02-10T00:00:00Z",
                 "now": "2026-02-15T00:00:00Z"})
    assert resp.status_code == 409
    assert resp.get_json()["conflicts"] == [assessment["id"]]

    # 确认区间之后可以正常研判
    resp = post(client, f"/trees/{tree}/assessments",
                {"period_start": "2026-02-01T00:00:00Z", "period_end": "2026-02-28T00:00:00Z",
                 "now": "2026-03-01T00:00:00Z"})
    assert resp.status_code == 201


# ---------------------------------------------------------------- 2. 异常可信度：邻近设备 + 人工证据

def test_anomaly_credibility_combines_neighbor_and_manual_evidence(client):
    # 失准设备 + 邻近正常 → 传感器漂移，不告警
    t2 = make_tree(client, "G002", location="西区")
    t3 = make_tree(client, "G003", location="西区")
    t4 = make_tree(client, "G004", location="西区")
    d2 = make_device(client, t2, calibrated_at="2025-09-01T00:00:00Z")  # 校准已 200 天，失准
    d3 = make_device(client, t3, calibrated_at="2026-03-01T00:00:00Z")
    d4 = make_device(client, t4, calibrated_at="2026-03-01T00:00:00Z")
    for dev in (d2, d3, d4):
        feed(client, dev, day_range(3, range(1, 11)), 40.0)
    feed(client, d3, ["2026-03-11T06:00:00Z"], 40.0)
    feed(client, d4, ["2026-03-11T06:00:00Z"], 40.0)

    resp = post(client, "/readings",
                {"device_id": d2, "value": 90.0, "observed_at": "2026-03-11T12:00:00Z",
                 "now": "2026-03-11T13:00:00Z"})
    anomaly = resp.get_json()["anomaly"]
    assert anomaly["classification"] == "sensor_drift"
    assert anomaly["credibility"] < 0.6
    assert anomaly["alert_id"] is None
    assert anomaly["evidence"]["calibration"]["fresh"] is False

    # 邻近设备同样异常 + 人工巡查佐证 + 校准正常 + 有在途措施 → 措施失效，高可信，生成告警
    post(client, f"/trees/{t3}/measures",
         {"measure_type": "irrigation", "started_at": "2026-03-01T00:00:00Z"})
    post(client, f"/trees/{t3}/inspections",
         {"inspected_at": "2026-03-12T08:00:00Z", "category": "pest",
          "severity": "severe", "finding": "根腐迹象"})
    post(client, "/readings",
         {"device_id": d4, "value": 90.0, "observed_at": "2026-03-12T06:00:00Z",
          "now": "2026-03-12T07:00:00Z"})
    resp = post(client, "/readings",
                {"device_id": d3, "value": 90.0, "observed_at": "2026-03-12T12:00:00Z",
                 "now": "2026-03-12T13:00:00Z"})
    anomaly = resp.get_json()["anomaly"]
    assert anomaly["classification"] == "measure_failure"
    assert anomaly["credibility"] >= 0.6
    assert anomaly["alert_id"] is not None
    assert anomaly["evidence"]["neighbors"]["corroborated"] >= 1
    assert len(anomaly["evidence"]["inspections"]) == 1


def test_weather_window_marks_anomaly_seasonal(client):
    tree = make_tree(client, "G005", location="北区")
    dev = make_device(client, tree, calibrated_at="2026-03-01T00:00:00Z")
    feed(client, dev, day_range(3, range(1, 11)), 40.0)
    post(client, "/weather-windows",
         {"region": "北区", "window_type": "drought",
          "started_at": "2026-03-01T00:00:00Z", "ended_at": "2026-03-31T00:00:00Z"})

    resp = post(client, "/readings",
                {"device_id": dev, "value": 90.0, "observed_at": "2026-03-15T00:00:00Z",
                 "now": "2026-03-15T01:00:00Z"})
    anomaly = resp.get_json()["anomaly"]
    assert anomaly["classification"] == "seasonal"
    assert anomaly["alert_id"] is None
    assert anomaly["evidence"]["weather"]["window_type"] == "drought"


# ---------------------------------------------------------------- 3. 规则升级只影响生效后的研判

def test_rule_upgrade_only_affects_later_assessments(client):
    tree = make_tree(client, "G006")
    dev = make_device(client, tree)
    feed(client, dev, day_range(1, range(1, 15)), 40.0)
    feed(client, dev, day_range(1, range(16, 31)), 43.0)  # delta +3：v1 阈值 5 → stable

    resp = post(client, f"/trees/{tree}/assessments",
                {"period_start": "2026-01-01T00:00:00Z", "period_end": "2026-01-31T00:00:00Z",
                 "now": "2026-02-01T00:00:00Z"})
    first = resp.get_json()
    assert first["trend"] == "stable"
    assert first["rule_version"] == 1
    post(client, f"/assessments/{first['id']}/confirm", {})

    # 规则升级：2 月起 improving 阈值收紧为 2
    post(client, "/rule-sets",
         {"effective_from": "2026-02-01T00:00:00Z",
          "config": {"trend": {"moisture": {"improving": 2.0, "declining": -2.0}}}})

    feed(client, dev, day_range(2, range(1, 15)), 40.0)
    feed(client, dev, day_range(2, range(15, 28)), 43.0)
    resp = post(client, f"/trees/{tree}/assessments",
                {"period_start": "2026-02-01T00:00:00Z", "period_end": "2026-02-28T00:00:00Z",
                 "now": "2026-03-01T00:00:00Z"})
    second = resp.get_json()
    assert second["rule_version"] == 2
    assert second["trend"] == "improving"

    # 历史结论仍记录为旧规则版本与旧结论
    assessments = client.get(f"/trees/{tree}/assessments").get_json()
    assert assessments[0]["rule_version"] == 1
    assert assessments[0]["trend"] == "stable"


# ---------------------------------------------------------------- 4. 高风险 → 可认领任务 + 升级期限；调岗/离线后持续追踪

def _build_alert(client, code="G007", location="南区"):
    """构建一条高可信告警及其处置任务，返回 (tree_id, device_id, alert_id, task_id)。"""
    tree = make_tree(client, code, location=location)
    neighbor = make_tree(client, code + "N", location=location)
    dev = make_device(client, tree, calibrated_at="2026-03-01T00:00:00Z")
    ndev = make_device(client, neighbor, calibrated_at="2026-03-01T00:00:00Z")
    feed(client, dev, day_range(3, range(1, 13)), 40.0)
    feed(client, ndev, day_range(3, range(1, 13)), 40.0)
    post(client, f"/trees/{tree}/measures",
         {"measure_type": "pest_control", "started_at": "2026-03-01T00:00:00Z"})
    post(client, f"/trees/{tree}/inspections",
         {"inspected_at": "2026-03-12T08:00:00Z", "category": "pest",
          "severity": "severe", "finding": "虫害扩散"})
    # 邻近树先报异常（其自身可信度不足，不告警），为本树提供旁证
    post(client, "/readings",
         {"device_id": ndev, "value": 95.0, "observed_at": "2026-03-12T12:00:00Z",
          "now": "2026-03-12T13:00:00Z"})
    resp = post(client, "/readings",
                {"device_id": dev, "value": 95.0, "observed_at": "2026-03-13T00:00:00Z",
                 "now": "2026-03-13T01:00:00Z"})
    anomaly = resp.get_json()["anomaly"]
    assert anomaly["alert_id"] is not None
    alert_id = anomaly["alert_id"]
    task = client.get("/tasks").get_json()[0]
    assert task["alert_id"] == alert_id
    return tree, dev, alert_id, task["id"]


def test_high_risk_generates_claimable_task_with_escalation(client):
    _, _, alert_id, task_id = _build_alert(client)
    staff_a = post(client, "/staff", {"name": "张三"}).get_json()["id"]
    staff_b = post(client, "/staff", {"name": "李四"}).get_json()["id"]

    task = client.get("/tasks").get_json()[0]
    assert task["due_at"] < task["escalation_deadline"]

    # 认领
    resp = post(client, f"/tasks/{task_id}/claim",
                {"staff_id": staff_a, "now": "2026-03-13T02:00:00Z"})
    assert resp.get_json()["status"] == "claimed"

    # 超期未办结 → 自动升级，告警留痕
    resp = post(client, "/escalations/run", {"now": "2026-03-20T00:00:00Z"})
    assert task_id in resp.get_json()["escalated_tasks"]
    replay = client.get(f"/alerts/{alert_id}/replay").get_json()
    assert replay["alert"]["status"] == "escalated"
    assert any(e["event_type"] == "escalated" and "超期" in e["reason"]
               for e in replay["events"])

    # 负责人调岗：任务退回待认领池，继续被追踪
    resp = post(client, f"/staff/{staff_a}/transfer",
                {"reason": "调往其他班组", "now": "2026-03-21T00:00:00Z"})
    assert resp.get_json()["released_tasks"] == [task_id]
    task = client.get("/tasks?status=open").get_json()[0]
    assert task["id"] == task_id and task["claimed_by"] is None

    # 设备离线不影响任务追踪
    post(client, "/devices/1/status", {"status": "offline", "reason": "电池耗尽"})
    assert any(t["id"] == task_id for t in client.get("/tasks?status=open").get_json())

    # 调岗人员不可再认领，其他人可认领
    resp = post(client, f"/tasks/{task_id}/claim", {"staff_id": staff_a})
    assert resp.status_code == 409
    resp = post(client, f"/tasks/{task_id}/claim", {"staff_id": staff_b})
    assert resp.get_json()["status"] == "claimed"


# ---------------------------------------------------------------- 5. 措施前后有效窗口比较

def test_measure_effectiveness_compares_windows(client):
    tree = make_tree(client, "G008")
    dev = make_device(client, tree, calibrated_at="2026-03-01T00:00:00Z")
    feed(client, dev, day_range(4, range(1, 14), hour=6), 30.0)
    measure = post(client, f"/trees/{tree}/measures",
                   {"measure_type": "irrigation",
                    "started_at": "2026-04-14T00:00:00Z"}).get_json()
    feed(client, dev, day_range(4, range(14, 28), hour=6), 40.0)

    result = client.get(
        f"/measures/{measure['id']}/effectiveness?metric=moisture&window_days=14").get_json()
    m = result["metrics"]["moisture"]
    assert m["before_window"]["mean"] == 30.0
    assert m["after_window"]["mean"] == 40.0
    assert m["delta"] == 10.0
    assert m["verdict"] == "effective"


# ---------------------------------------------------------------- 6. 告警合并与回放

def test_alert_merge_and_replay(client):
    tree = make_tree(client, "G009", location="中心区")
    neighbor = make_tree(client, "G009N", location="中心区")
    dev = make_device(client, tree, calibrated_at="2026-05-01T00:00:00Z")
    ndev = make_device(client, neighbor, calibrated_at="2026-05-01T00:00:00Z")
    feed(client, dev, day_range(5, range(1, 13)), 40.0)
    feed(client, ndev, day_range(5, range(1, 13)), 40.0)
    post(client, f"/trees/{tree}/measures",
         {"measure_type": "bracing", "started_at": "2026-05-01T00:00:00Z"})
    post(client, f"/trees/{tree}/inspections",
         {"inspected_at": "2026-05-12T18:00:00Z", "category": "bark",
          "severity": "severe", "finding": "树干开裂"})

    post(client, "/readings",
         {"device_id": ndev, "value": 95.0, "observed_at": "2026-05-12T12:00:00Z",
          "now": "2026-05-12T13:00:00Z"})
    first = post(client, "/readings",
                 {"device_id": dev, "value": 95.0, "observed_at": "2026-05-13T00:00:00Z",
                  "now": "2026-05-13T01:00:00Z"}).get_json()["anomaly"]
    post(client, "/readings",
         {"device_id": ndev, "value": 95.0, "observed_at": "2026-05-13T12:00:00Z",
          "now": "2026-05-13T13:00:00Z"})
    second = post(client, "/readings",
                  {"device_id": dev, "value": 130.0, "observed_at": "2026-05-14T00:00:00Z",
                   "now": "2026-05-14T01:00:00Z"}).get_json()["anomaly"]

    # 同树同指标且在合并窗口内 → 只保留一条告警
    assert first["alert_id"] == second["alert_id"]
    alert_id = first["alert_id"]
    alerts = client.get(f"/alerts?tree_id={tree}").get_json()
    assert len(alerts) == 1

    post(client, f"/alerts/{alert_id}/resolve",
         {"reason": "复测确认已加固，风险解除", "actor": "王五", "now": "2026-05-15T00:00:00Z"})

    replay = client.get(f"/alerts/{alert_id}/replay").get_json()
    event_types = [e["event_type"] for e in replay["events"]]
    assert event_types == ["created", "merged", "resolved"]
    assert "合并" in replay["events"][1]["reason"] or "并入" in replay["events"][1]["reason"]
    assert replay["events"][2]["reason"] == "复测确认已加固，风险解除"
    assert len(replay["anomalies"]) == 2
    # 解除告警后任务关闭并留痕
    assert replay["tasks"][0]["status"] == "done"
    assert any(e["event_type"] == "done" for e in replay["tasks"][0]["events"])


# ---------------------------------------------------------------- 7. 统一时间线

def test_timeline_unifies_all_streams(client):
    tree, dev, alert_id, task_id = _build_alert(client, code="G010", location="东区")
    post(client, "/weather-windows",
         {"region": "东区", "window_type": "storm",
          "started_at": "2026-03-10T00:00:00Z", "ended_at": "2026-03-11T00:00:00Z"})
    post(client, f"/devices/{dev}/status", {"status": "offline", "reason": "通信故障",
                                            })
    post(client, f"/trees/{tree}/assessments",
         {"period_start": "2026-03-01T00:00:00Z", "period_end": "2026-03-31T00:00:00Z",
          "now": "2026-04-01T00:00:00Z"})

    timeline = client.get(f"/trees/{tree}/timeline").get_json()
    types = {item["type"] for item in timeline["items"]}
    assert {"calibration", "inspection", "measure", "weather_window",
            "assessment", "anomaly", "alert_event", "task_event", "device_event"} <= types
    times = [item["time"] for item in timeline["items"]]
    assert times == sorted(times)
    assert timeline["reading_counts"]["moisture"] > 0
