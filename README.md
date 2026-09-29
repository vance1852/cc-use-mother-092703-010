# 新能源联合消纳调度平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，以及衡阳园区新能源（光伏/风电）、储能、输变电线路与工业用户可调负荷的分时联合消纳计划。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/renewable_dispatch/`：带版本的发电预测、储能可用窗口、线路能力与用户保供承诺，分时方案生成、确认锁定、预测修订重算、调度指令事件核对与重启恢复；
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

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入流程，以及联合消纳方案的生成、确认锁定、指令结算、预测修订与重启恢复，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m renewable_dispatch.api --database dispatch.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 联合消纳计划服务（`renewable_dispatch`）

时间轴按 15 分钟对齐，功率单位 kW、电量单位 kWh，内部统一使用 `Decimal` 落库。

- 带版本的不可变输入：发电预测 `POST /forecasts`、储能可用窗口与检修 `POST /storage/availability`、输变电线路分时能力 `POST /line/capability`、工业用户保供承诺 `POST /commitments`；不指定版本时取最新版本。
- 生成方案 `POST /plans`：按“新能源直供 → 储能放电 → 线路受入 → 未满足需求”补缺口，按“储能充电 → 工业可调负荷上浮 → 线路外送 → 弃电”消纳富余；方案保存所采用输入的完整快照、`input_sha256` 与逐版本 `plan_inputs`。
- 确认方案 `POST /plans/{id}/confirm`：锁定储能充放、线路通道（按方向做能力累加校验）与用户负荷；`GET /plans/{id}` 返回各时段约束余量、未满足需求与调整原因。
- 预测修订 `POST /plans/{id}/revise`：生成新版本方案，已执行时段原样承接，只重算未执行时段，并以最后已执行时段的结束 SOC 作为边界；旧方案未执行时段的锁释放，旧方案置为 `superseded`。
- 调度指令 `POST /instructions`：方案内 `seq_no` 单调递增，指令功率不得超过方案安排；回执 `POST /instructions/{id}/ack`、`.../settle` 按事件序号核对，重复、迟到回执和序号不匹配均不重复结算，且全部幂等。
- 重启恢复：`GET /plans/pending` 返回所有已确认方案的待执行时段（含余量、未满足需求与原因）和未闭环指令，状态全部来自 SQLite。
- 角色：`planner`（输入登记与方案生成/修订）、`operator`（确认、下发指令、回执）、`auditor`（只读与哈希链审计 `GET /audit/chain`），所有接口需携带 `X-Actor-Id` 头。
