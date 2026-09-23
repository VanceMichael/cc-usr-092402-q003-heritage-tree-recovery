"""研判规则：默认配置与版本合并。

规则以版本形式存储在 rule_sets 表中，只有 effective_from 之后的研判才使用新版本；
历史结论保留其当时的 rule_version，不受升级影响。存储的配置允许只给出差异项，
使用时与 DEFAULT_CONFIG 深合并，保证旧版本规则在新增键后仍可解释。
"""
import copy

DEFAULT_CONFIG = {
    # 基线：以同一设备此前 N 天读数估计正常范围
    "baseline_window_days": 14,
    "min_baseline_samples": 3,
    # 偏离基线多少倍标准差判定为异常
    "anomaly_sigma": 3.0,
    # 证据收集窗口
    "neighbor_window_hours": 24,
    "inspection_window_hours": 72,
    # 校准超过 N 天视为失准
    "calibration_fresh_days": 90,
    # 同树同指标告警在该窗口内合并
    "merge_window_hours": 72,
    # 可信度阈值：达到才生成告警；达到 critical 阈值直接定为 critical
    "alert_credibility_threshold": 0.6,
    "critical_credibility_threshold": 0.85,
    # 处置任务期限（小时）：due_at 处置期限，之后 escalation_hours 内未办结则升级
    "task_due_hours": 48,
    "escalation_hours": 24,
    # 可信度权重
    "weights": {"neighbor": 0.3, "inspection": 0.25, "calibration": 0.25, "weather": 0.2},
    # 气象窗口类型 → 可解释的指标（用于判定季节性变化）
    "weather_metric_map": {
        "drought": ["moisture"],
        "heatwave": ["moisture"],
        "frost": ["moisture", "diameter"],
        "storm": ["tilt"],
    },
    # 阶段趋势阈值：后半窗口均值 - 前半窗口均值 的增量判定
    "trend": {
        "tilt": {"declining": 0.5, "improving": -0.5},
        "moisture": {"declining": -5.0, "improving": 5.0},
        "diameter": {"declining": -0.2, "improving": 0.2},
    },
}


def merged_config(stored):
    """把数据库存储的（可能是部分的）配置深合并到默认配置上。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not stored:
        return cfg

    def _merge(dst, src):
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                _merge(dst[key], value)
            else:
                dst[key] = value

    _merge(cfg, stored)
    return cfg
