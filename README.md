# 古树恢复趋势哨兵

把树径、含水率、倾斜角与病虫害巡查数据放进同一个恢复趋势研判闭环：设备校准、人工巡查、养护措施、气象窗口与树体风险共享一条时间线，异常读数自动给出可信度与分类，高风险变化生成可认领、会升级的处置任务，全程留痕可回放。

Flask + SQLite，迁移脚本在 `migrations/`，运行时不需要外部服务。

启动：`docker build -t tree-sentinel . && docker run --rm -p 8080:8080 tree-sentinel`。本地测试：`pytest`。

## 闭环设计

```
读数摄入 → 异常研判（可信度+分类） → 告警（合并/升级/解除） → 处置任务（认领/超期升级）
                ↓                          ↓ 留痕                     ↓ 留痕
        阶段结论（确认后不可改写） ←—— 规则版本（只影响生效后的研判）
```

核心不变量：

1. **乱序补报不改写已确认结论**：采集时刻落在已确认阶段区间内的读数标记 `is_backfill=1`，照常存档、照常参与新研判，但已确认结论不变；与已确认区间重叠的研判请求返回 409。
2. **异常可信度来自多源证据**：偏离基线（同设备 14 天、3σ）的读数，结合邻近设备旁证（同区域同指标）、人工巡查、校准新鲜度/设备在线状态、气象窗口计算可信度（0~1），分类为 `sensor_drift`（设备漂移）/ `seasonal`（季节气象）/ `measure_failure`（措施失效）/ `unclassified`，证据快照随记录保存。
3. **规则版本化**：`rule_sets` 按 `effective_from` 生效；研判只使用研判时刻已生效的最新版本，历史结论与异常记录保留当时的 `rule_version`。
4. **高风险必须有人接手**：可信度达阈值的异常生成告警与处置任务（处置期限 `due_at` + 升级期限 `escalation_deadline`）。负责人调岗任务退回待认领池、设备离线不影响任务；超期未办结由 `POST /escalations/run` 扫描升级，告警级别随之上调。
5. **一切决策可回放**：告警的创建/合并/升级/解除与任务的认领/退回/升级/办结全部写入事件流，附原因与当时的规则版本。

## 主要 API

| 端点 | 说明 |
| --- | --- |
| `POST /trees` `/staff` `/devices` | 档案登记 |
| `POST /devices/{id}/calibrations` `/status` | 校准记录；上线/离线（留痕） |
| `POST /readings` `/readings/batch` | 读数摄入，返回触发的异常研判 |
| `POST /trees/{id}/inspections` `/measures` | 人工巡查；养护措施 |
| `POST /weather-windows` | 气象窗口（按区域） |
| `POST /rule-sets` | 新规则版本（`effective_from` 起生效） |
| `POST /trees/{id}/assessments` | 阶段研判（默认从上一确认区间末尾开始） |
| `POST /assessments/{id}/confirm` | 确认阶段结论（此后不可改写） |
| `GET /anomalies` `/anomalies/{id}` | 异常及证据快照 |
| `GET /alerts` `POST /alerts/{id}/resolve` `/merge` | 告警查询与处置 |
| `GET /alerts/{id}/replay` | 回放：为何合并、升级、解除 |
| `GET /tasks` `POST /tasks/{id}/claim` `/complete` | 待认领池与任务操作 |
| `POST /staff/{id}/transfer` | 调岗：名下任务退回待认领池 |
| `POST /escalations/run` | 扫描超期任务并升级 |
| `GET /trees/{id}/timeline` | 统一时间线（`include_readings=1` 含原始读数） |
| `GET /measures/{id}/effectiveness` | 措施前后等长有效窗口对比（剔除漂移读数） |

时间敏感的写端点都接受可选 `now`（ISO-8601），便于排班回放与测试。

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py sentinel`
