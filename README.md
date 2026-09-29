# 新能源联合消纳调度平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景、硬件稳定性准入，以及衡阳新能源园区的储能、输变电设备与工业可调负荷的联合消纳计划。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/renewable_dispatch/`：带版本的发电预测、储能可用窗口、线路能力、用户保供约束、分时方案、资源锁定、调度指令与回执结算；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m renewable_dispatch.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入，以及联合消纳计划的版本化输入、分时方案、锁定、修订重算、回执结算与重启恢复流程，不访问外部网络。

## 联合消纳计划服务（renewable_dispatch）

面向衡阳新能源园区逐步并网后的协调调度：

- **带版本的输入**：发电预测（`/forecasts`）、储能可用窗口（`/storage/windows`）、输变电线路分时能力（`/lines/capabilities`）、工业用户保供约束（`/loads/commitments`）均按版本登记；新版本登记后旧版本置为 `retired`。
- **分时方案与输入快照**：`POST /plans/generate` 在统一时间网格上逐时段模拟储能充放、叠加线路能力，输出各时段功率平衡、约束余量、未满足需求与调整原因；方案保存所采用输入的规范化快照及 sha256，`GET /plans/{id}/verify` 可用快照重放核对。
- **确认即锁定**：`POST /plans/{id}/confirm` 锁定方案涉及的储能、线路、用户资源，并为未执行时段生成带事件序号（event_seq）的调度指令；资源已被其他确认方案锁定时拒绝确认。
- **预测修订只重算未执行时段**：已执行时段从已确认方案冻结（含期末 SOC，保证时序连续），仅重算剩余时段，生成新 revision，旧确认版本置为 `superseded` 并取消其未执行指令；输入版本未变化时拒绝无谓重算。
- **回执幂等与迟到防护**：`POST /receipts` 按事件序号核对；同一幂等键回放首次结果，换键二次结算、时段结束后的确认、过早结算或针对已作废版本的回执均记为 `late`/不重复结算。
- **运营接口与恢复**：`GET /operations/{horizon_id}` 展示各时段约束余量、未满足需求、调整原因与待执行指令；`GET /plans/recover` 在进程重启后从 SQLite 恢复所有待执行的确认方案。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m renewable_dispatch.api --database dispatch.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON（写接口需 `X-Actor-Id` 头）。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。
