# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **链核对**（按 UTC 时刻升序，乱序补传自动归位）：
   - 交接的 `giver` 必须等于当前保管方（首笔对照起始保管方，其后对照上一笔的 `receiver`），
     不匹配即为 **GAP 断链**。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
6. **原子性与一致性**：
   - 每个批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链**。
   - 查询只读已提交快照（WAL），交错的扫描提交与查询永远看不到半写状态或混合修订。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。

## 证据包导出 / 导入（离线库并入）

监管部门可把一张运单的扫描证据从离线库导出为证据包，再并入另一库。
两个接口均可在 `/docs` 中直接操作。

### 导出 `GET /api/waybills/{waybill_id}/evidence`

运单行、全部修订行与可信链在**同一个只读事务快照**内读取，包内容永远对应
同一个已提交状态。包结构：

```json
{
  "format": "custody-evidence/v1",
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "revisions": [
    {"scan_id": "S1", "revision": 1, "event_type": "HANDOVER",
     "ts_utc": "2026-11-18T08:00:00Z", "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL",
     "ts_utc": null, "giver": null, "receiver": null}
  ],
  "chain": { "...": "按当前规则重算的可信链（ChainResponse）" },
  "digest": "sha256:<64 hex>"
}
```

* `revisions` 是**全部修订历史**：含被更高修订覆盖的旧版本与撤回墓碑，不止当前生效版本。
* `digest` 为确定性摘要：对 `{format, waybill_id, origin_party, revisions}` 的规范化
  （时刻统一 UTC `Z`、按 `(scan_id, revision)` 排序、键排序的紧凑 JSON）做 SHA-256。
  同一逻辑内容在任何库中重算都得到同一摘要。

### 导入 `POST /api/evidence/import`

请求体即导出的证据包。导入先做任何写入之前完成三道完整性核对，任一不符返回
`422 package_integrity` 且本库不变：

1. **修订唯一性**：包内 `(scan_id, revision)` 不得重复；
2. **摘要**：按包内内容重算的 `digest` 必须与包声明的一致；
3. **可信链**：由包内修订重算的可信链必须与包内嵌入的 `chain` 一致。

核对通过后，合并在**单个 `BEGIN IMMEDIATE` 事务**内完成：

* 目标库无此运单 -> 按包内起始保管方建档；
* 已有同号运单但**起始保管方不同** -> `409` 整包拒绝；
* **共同修订必须完全相同**（同修订异文 -> `409` 整包拒绝）；
* 缺少的修订逐条补入；已存在且同文的修订幂等跳过 —— **同一包重试无任何副作用**；
* 损坏包、并发导入（写锁串行化）或中途失败都整体回滚，**不会留下部分修订**。

成功响应返回 `imported_revisions`（新补入条数）、`already_present`（幂等条数）
与**合并后的可信链**。导入后该运单仍按原规则补报、更正、撤回和查询
（导入修订的 `payload_hash` 与正常提交一致，同修订异文检测照常生效）。


## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| GET | `/api/waybills/{waybill_id}/evidence` | 导出证据包（同一已提交快照：全部修订+撤回、可信链、确定性摘要） |
| POST | `/api/evidence/import` | 导入证据包：核对摘要/修订唯一性/包内重算可信链，原子合并缺失修订 |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "verified_prefix": [ {"scan_id": "S1", "verified": true, "...": "..."} ],
  "unverified_scans": [ {"scan_id": "S3", "verified": false,
                         "unverified_reason": "GAP"} ],
  "chain_intact": false,
  "break_reason": "GAP",
  "break_detail": "giver 'X' does not hold custody; expected 'CX-CARGO' ...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（保管方不承接）/ `SIMULTANEOUS`（同时刻多笔，顺序不可判定）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已证实接收方。

## 测试

```bash
pip install pytest httpx
pytest -q
```

共 46 个用例，覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化，以及证据包导出/导入（快照一致性、摘要与
可信链核对、整包回滚、幂等重试、并发导入串行化、导入后原规则不变）。
