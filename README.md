# 古树恢复趋势哨兵

这是一个记录树体观测与窗口计算输入的 Flask 服务起点。SQLite 是唯一持久化介质，迁移脚本与测试数据均在仓库内，运行时不需要外部服务。

启动：`docker build -t tree-sentinel . && docker run --rm -p 8080:8080 tree-sentinel`。本地测试：`pytest`。

## 开发检查

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest`
- 编译检查：`python3 -m compileall -q app.py`
