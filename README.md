# 古树恢复趋势哨兵

把树径、含水率、倾斜角、病虫害巡查等观测数据扩展为**恢复趋势研判闭环**：设备校准、人工巡查、养护措施、气象窗口与树体风险统一在同一条时间线上，高风险变化自动生成可认领、有升级期限的处置告警，全程留痕可回放。

SQLite 是唯一持久化介质，迁移脚本在 `migrations/` 下，服务启动时按序自动应用，运行时不需要外部服务。

启动：`docker build -t tree-sentinel . && docker run --rm -p 8080:8080 tree-sentinel`。本地测试：`pytest`。

## 闭环规则

- **乱序补报不改写结论**：`assessments` 已确认的阶段结论永不修改。观测时刻早于最近结论的补报读数标记 `late=1`，只进入下一次研判的证据（`evidence.late_reading_ids`）。
- **异常读数可信度**：越界或相对同设备历史突变即为异常；可信度综合设备在线状态、校准时效、邻近设备读数是否一致、人工巡查证据计算，低于阈值即隔离（`quarantined`），不参与研判。
- **规则版本边界**：`rule_sets` 按 `effective_from` 生效，摄入与研判只使用当时生效的版本，升级不回溯。
- **告警闭环**：高风险研判生成告警并给出升级期限；同树同风险再次确认时合并而非新建；逾期无人认领（或认领未办结）自动升级；风险回落凭研判证据解除。设备全部离线时转入降级追踪但告警保持有效；负责人调岗时未决告警转交指定人员或回到班组队列，追踪不中断。
- **每次合并、升级、认领、转派、解除都写入 `alert_events`**，`GET /alerts/<id>/replay` 可回放完整因果链。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/trees` `/staff` `/devices` | 登记古树、人员、设备 |
| POST | `/devices/<id>/calibrations` | 记录校准事件 |
| POST | `/devices/<id>/status` | 设备上线/离线 |
| POST | `/readings` | 读数摄入（`observed_at`/`reported_at` 分离，支持补报） |
| POST | `/inspections` `/measures` `/weather-windows` | 巡查、养护措施、气象窗口 |
| POST | `/rule-sets` | 发布规则版本（`effective_from` 起生效） |
| POST | `/trees/<id>/assessments` | 提交一次阶段结论（可传 `now` 指定研判时刻） |
| GET | `/trees/<id>/assessments` `/timeline` `/risk` | 历史结论、统一时间线、当前风险 |
| GET/POST | `/alerts` `/alerts/<id>/claim` `/resolve` `/replay` | 告警查询、认领、解除、回放 |
| POST | `/staff/<id>/deactivate` | 调岗（`reassign_to` 指定接手人） |
| POST | `/ops/sweep` | 期限扫描：逾期升级、离线降级追踪 |
| GET | `/measures/<id>/effectiveness?metric=...` | 措施前后有效窗口对比（剔除隔离与气象窗口内读数） |

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py core.py`
