import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app  # noqa: E402
from core import DEFAULT_RULES  # noqa: E402

T = "2026-09-01T00:00:00Z"


def ts(day, hour=0, minute=0):
    return f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture()
def base(client):
    """一棵树、两名技术员、一套 v1 规则。"""
    assert client.post("/trees", json={"id": "T1", "name": "古银杏"}).status_code == 201
    assert client.post("/staff", json={"id": "S1", "name": "张三"}).status_code == 201
    assert client.post("/staff", json={"id": "S2", "name": "李四"}).status_code == 201
    assert client.post("/rule-sets", json={
        "version": "v1", "effective_from": "2026-01-01T00:00:00Z", "rules": {},
    }).status_code == 201
    return client


def add_device(client, device_id, metric, tree="T1", calibrated_at="2026-08-30T00:00:00Z"):
    assert client.post("/devices", json={
        "id": device_id, "tree_id": tree, "metric": metric,
        "installed_at": "2026-01-01T00:00:00Z",
    }).status_code == 201
    if calibrated_at:
        assert client.post(f"/devices/{device_id}/calibrations", json={
            "calibrated_at": calibrated_at, "drift": 0.1,
        }).status_code == 201


def post_reading(client, device_id, value, day, hour=0, reported=None):
    observed = ts(day, hour)
    payload = {"device_id": device_id, "value": value, "observed_at": observed,
               "reported_at": reported or observed}
    resp = client.post("/readings", json=payload)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def assess(client, day, hour=0, tree="T1"):
    resp = client.post(f"/trees/{tree}/assessments", json={"now": ts(day, hour)})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


# ---------------------------------------------------------------- 摄入与可信度

def test_normal_reading_is_clean(base):
    add_device(base, "D1", "tilt")
    r = post_reading(base, "D1", 10.0, 1)
    assert r["is_anomaly"] is False
    assert r["credibility"] == 1.0
    assert r["quarantined"] is False
    assert r["late"] is False


def test_anomaly_corroborated_by_neighbor_is_credible(base):
    add_device(base, "D1", "tilt")
    add_device(base, "D2", "tilt")
    post_reading(base, "D1", 10.0, 1)
    post_reading(base, "D2", 10.0, 1)
    post_reading(base, "D1", 14.0, 2)          # 突变，先入库存档
    r2 = post_reading(base, "D2", 14.2, 2, hour=1)  # 邻近设备同向异常
    assert r2["is_anomaly"] is True
    assert r2["quarantined"] is False
    assert any("邻近设备读数一致" in reason for reason in r2["reasons"])


def test_anomaly_offline_uncalibrated_is_quarantined(base):
    add_device(base, "D1", "tilt", calibrated_at=None)
    base.post("/devices/D1/status", json={"status": "offline"})
    post_reading(base, "D1", 10.0, 1)
    r = post_reading(base, "D1", 25.0, 2)
    assert r["is_anomaly"] is True
    assert r["credibility"] < DEFAULT_RULES["credibility"]["quarantine_threshold"]
    assert r["quarantined"] is True
    assert any("设备离线" in reason for reason in r["reasons"])
    # 被隔离的读数不进入研判，不会误报告警
    result = assess(base, 3)
    assert result["risk_level"] == "low"
    assert result["evidence"]["quarantined_reading_ids"] == [r["id"]]
    assert base.get("/alerts?tree_id=T1").get_json() == []


# ---------------------------------------------------------------- 乱序补报

def test_late_report_does_not_rewrite_committed_conclusion(base):
    add_device(base, "D1", "tilt")
    post_reading(base, "D1", 10.0, 1)
    first = assess(base, 2)
    assert first["risk_level"] == "low" and first["phase"] == "stable"

    # 补报一条观测时刻早于已确认结论的异常读数
    late = post_reading(base, "D1", 25.0, 1, reported=ts(2, 12))
    late_id = late["id"]
    assert late["late"] is True
    assert late["quarantined"] is False

    # 已确认结论保持原样
    history = base.get("/trees/T1/assessments").get_json()
    assert len(history) == 1
    assert history[0]["risk_level"] == "low"
    assert history[0]["phase"] == "stable"

    # 补报进入下一次研判的证据并触发告警，历史结论不被改写
    second = assess(base, 3)
    assert second["risk_level"] == "high"
    assert late_id in second["evidence"]["late_reading_ids"]
    assert second["alert"]["action"] == "created"
    history = base.get("/trees/T1/assessments").get_json()
    assert len(history) == 2
    assert history[0]["risk_level"] == "low" and history[0]["phase"] == "stable"
    assert history[1]["supersedes"] == history[0]["id"]


# ---------------------------------------------------------------- 规则版本边界

def test_rule_upgrade_only_affects_later_assessments(base):
    add_device(base, "D1", "tilt")
    post_reading(base, "D1", 25.0, 5)          # v1 下越界（max=20）
    first = assess(base, 6)
    assert first["rule_version"] == "v1"
    assert first["risk_level"] == "high"
    alert_id = first["alert"]["alert_id"]

    # 规则升级：倾斜角容忍度放宽到 30
    assert base.post("/rule-sets", json={
        "version": "v2",
        "effective_from": ts(10),
        "rules": {"metrics": {"tilt": {"min": 0.0, "max": 30.0, "max_delta": 5.0}}},
    }).status_code == 201

    r = post_reading(base, "D1", 25.0, 11)     # v2 下不再是异常
    assert r["is_anomaly"] is False
    assert r["rule_version"] == "v2"
    second = assess(base, 12)
    assert second["rule_version"] == "v2"
    assert second["risk_level"] == "low"
    assert second["alert"]["action"] == "resolved"

    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    assert replay["alert"]["status"] == "resolved"
    assert [e["event_type"] for e in replay["events"]] == ["created", "resolved"]


# ---------------------------------------------------------------- 告警闭环

def _raise_high_alert(base, day=1):
    add_device(base, "D1", "tilt")
    post_reading(base, "D1", 25.0, day)
    return assess(base, day, hour=12)


def test_high_risk_alert_escalates_then_claimed(base):
    result = _raise_high_alert(base)
    alert_id = result["alert"]["alert_id"]
    alert = base.get("/alerts?tree_id=T1").get_json()[0]
    assert alert["severity"] == "high"
    assert alert["due_at"] == ts(4, 12)        # high → 72h

    # 期限内扫描不升级
    assert base.post("/ops/sweep", json={"now": ts(2, 12)}).get_json()["escalated"] == []
    # 逾期无人认领 → 升级为 critical，期限重置
    sweep = base.post("/ops/sweep", json={"now": ts(4, 13)}).get_json()
    assert sweep["escalated"] == [alert_id]
    alert = base.get("/alerts?tree_id=T1").get_json()[0]
    assert alert["severity"] == "critical"
    assert alert["due_at"] == ts(5, 13)        # critical → 24h

    # 认领后仍在追踪；逾期不办结继续升级
    assert base.post(f"/alerts/{alert_id}/claim", json={
        "staff_id": "S1", "now": ts(4, 14)}).status_code == 200
    sweep = base.post("/ops/sweep", json={"now": ts(5, 14)}).get_json()
    assert sweep["escalated"] == [alert_id]

    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    types = [e["event_type"] for e in replay["events"]]
    assert types == ["created", "escalated", "claimed", "escalated"]
    assert "无人认领" in replay["events"][1]["reason"]
    assert "已认领但未办结" in replay["events"][3]["reason"]


def test_repeated_high_risk_merges_into_existing_alert(base):
    first = _raise_high_alert(base, day=1)
    alert_id = first["alert"]["alert_id"]
    post_reading(base, "D1", 26.0, 2)
    second = assess(base, 2, hour=12)
    assert second["alert"]["action"] == "merged"
    assert second["alert"]["alert_id"] == alert_id
    assert len(base.get("/alerts?tree_id=T1").get_json()) == 1
    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    assert [e["event_type"] for e in replay["events"]] == ["created", "merged"]
    assert "并入既有告警" in replay["events"][1]["reason"]


def test_manual_resolve_requires_note_and_is_replayable(base):
    result = _raise_high_alert(base)
    alert_id = result["alert"]["alert_id"]
    assert base.post(f"/alerts/{alert_id}/resolve", json={"staff_id": "S1"}).status_code == 400
    resp = base.post(f"/alerts/{alert_id}/resolve", json={
        "staff_id": "S1", "note": "现场支撑加固完成，复测倾斜角回落", "now": ts(2)})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "resolved"
    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    assert replay["events"][-1]["reason"] == "现场支撑加固完成，复测倾斜角回落"
    # 已解除的告警不能再认领
    assert base.post(f"/alerts/{alert_id}/claim", json={"staff_id": "S2"}).status_code == 409


def test_owner_transfer_keeps_tracking_after_reassignment(base):
    result = _raise_high_alert(base)
    alert_id = result["alert"]["alert_id"]
    base.post(f"/alerts/{alert_id}/claim", json={"staff_id": "S1", "now": ts(2)})

    resp = base.post("/staff/S1/deactivate", json={"now": ts(3), "reassign_to": "S2"})
    assert resp.get_json()["reassigned_alerts"] == [alert_id]
    alert = base.get("/alerts?tree_id=T1").get_json()[0]
    assert alert["owner_id"] == "S2" and alert["status"] == "claimed"

    # 追踪不中断：逾期照样升级；调岗人员不能再认领
    sweep = base.post("/ops/sweep", json={"now": ts(5)}).get_json()
    assert sweep["escalated"] == [alert_id]
    assert base.post(f"/alerts/{alert_id}/claim", json={"staff_id": "S1"}).status_code == 409
    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    assert any(e["event_type"] == "reassigned" and "调岗" in e["reason"] for e in replay["events"])


def test_device_offline_degrades_tracking_but_alert_survives(base):
    result = _raise_high_alert(base)
    alert_id = result["alert"]["alert_id"]
    base.post("/devices/D1/status", json={"status": "offline"})
    sweep = base.post("/ops/sweep", json={"now": ts(2)}).get_json()
    assert sweep["tracking_degraded"] == [alert_id]
    alert = base.get("/alerts?tree_id=T1").get_json()[0]
    assert alert["status"] == "open"           # 离线不自动解除

    base.post("/devices/D1/status", json={"status": "online"})
    sweep = base.post("/ops/sweep", json={"now": ts(3)}).get_json()
    assert sweep["tracking_restored"] == [alert_id]
    replay = base.get(f"/alerts/{alert_id}/replay").get_json()
    types = [e["event_type"] for e in replay["events"]]
    assert types == ["created", "tracking_degraded", "tracking_restored"]


# ---------------------------------------------------------------- 措施有效窗口

def test_measure_effectiveness_compares_windows(base):
    add_device(base, "D1", "tilt")
    # 措施 09-10 实施，前窗口 08-11..09-10，后窗口 09-10..10-10
    post_reading(base, "D1", 18.0, 5)
    post_reading(base, "D1", 19.0, 8)
    resp = base.post("/measures", json={
        "tree_id": "T1", "measure_type": "支撑加固", "applied_at": ts(10), "applied_by": "S1"})
    measure_id = resp.get_json()["id"]
    post_reading(base, "D1", 12.0, 15)
    post_reading(base, "D1", 10.0, 20)
    # 气象窗口内的读数应被剔除
    base.post("/weather-windows", json={
        "tree_id": "T1", "kind": "rain", "started_at": ts(16), "ended_at": ts(17)})
    post_reading(base, "D1", 15.0, 16, hour=12)

    eff = base.get(f"/measures/{measure_id}/effectiveness?metric=tilt&now=2026-10-01T00:00:00Z").get_json()
    assert eff["before"]["count"] == 2
    assert eff["before"]["mean"] == 18.5
    assert eff["after"]["count"] == 2
    assert eff["after"]["mean"] == 11.0
    assert eff["after"]["excluded_weather"] == 1
    assert eff["verdict"] == "effective"


# ---------------------------------------------------------------- 统一时间线

def test_timeline_unifies_all_event_kinds(base):
    add_device(base, "D1", "tilt")
    post_reading(base, "D1", 25.0, 1)          # 异常读数
    base.post("/inspections", json={
        "tree_id": "T1", "inspector_id": "S1", "inspected_at": ts(1, 6), "finding": "concern"})
    base.post("/measures", json={"tree_id": "T1", "measure_type": "支撑加固", "applied_at": ts(1, 8)})
    base.post("/weather-windows", json={
        "tree_id": "T1", "kind": "wind", "started_at": ts(1, 2), "ended_at": ts(1, 4)})
    assess(base, 1, hour=12)

    timeline = base.get("/trees/T1/timeline").get_json()
    kinds = {item["kind"] for item in timeline}
    assert {"calibration", "reading_anomaly", "inspection", "measure",
            "weather_window", "assessment", "alert_event"} <= kinds
    times = [item["time"] for item in timeline]
    assert times == sorted(times)


def test_health(base):
    assert base.get("/health").get_json() == {"status": "ok"}
