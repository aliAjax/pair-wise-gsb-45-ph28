# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、潮位窗口适配、航道批次编排和引航对账规则。
- `src/repository.py`：SQLite建表、跨表事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发、批次重算、冲突草稿和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、批次重算、并发草稿和引航对账测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建计划，请求体为`{"reference":"...","tide_window_id":1,"data":{...}}`，带窗口时同事务编排首批次。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（confirm/berth/depart/cancel/reschedule），请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/tide-windows`：创建潮位窗口（日期+进/出港唯一），`data`含open_hour/close_hour/vessel_quota/tide_level_m。
- `GET /api/tide-windows`：窗口列表。
- `PUT /api/tide-windows/{id}`：修改窗口（乐观锁`expected_version`），成功后同窗口批次自动重算；版本冲突时自动保留冲突草稿。
- `GET /api/tide-windows/{id}/batches`：窗口的批次视图，含全部代次、最新重算结果和未恢复的失败重算`pending_failure`。
- `POST /api/tide-windows/{id}/recompute`：值班员手动重试重算。
- `GET /api/tide-windows/{id}/recompute-runs`：重算历史（成功/失败及原因）。
- `POST /api/records/{id}/actions/gate_release`：闸口放行，`data.now_hour`为当前小时；仅有效批次、窗口开放且无未恢复失败重算时放行。
- `GET /api/conflict-drafts`：打开的冲突草稿。
- `POST /api/conflict-drafts/{id}/apply`：后到方在最新版本上重放草稿。
- `POST /api/conflict-drafts/{id}/discard`：放弃草稿。
- `POST /api/pilot-reports`：引航员回传单，`data`为`{"notice_id":"N-1","pilot_id":"...","items":[{"vessel":"船A","direction":"in","actual_hour":6}]}`。
- `GET /api/pilot-reports` / `GET /api/pilot-reports/{id}`：回传单列表/明细（含逐船对账结果）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 调度业务规则

- **计划与批次分离**：靠泊计划挂到潮位窗口，航道批次按窗口独立编排（普通船按`vessel_quota`分组，危险品单船单批次）。批次带代次，改期/窗口变化触发同窗口整体重算，旧批次先置`invalid`；已放行/落地的船在新批次中锚定（`pinned`）并保留原状态，不重复占额度。
- **重算失败可重试**：重算前先预演（泊位冲突、窗口时段、潮位吃水、配额）。任一船不满足则整窗失败：计划改动随事务回滚、旧批次保留、`recompute_runs`落失败原因；未恢复前闸口对该窗口暂停放行，值班员修正后调用重算接口或再次改期即可恢复。
- **并发编辑**：计划改期与窗口调整均使用乐观锁，先到版本生效；后到方收到409且其改动自动保存为冲突草稿（响应中带`draft_id`和`current_version`），可在最新版本上apply重放或discard放弃。
- **引航对账**：回传单按`notice_id`幂等。逐船比对计划进/出港时刻（默认容差1小时，可传`tolerance_hours`），不一致（时刻超差、流向不符、无计划、无有效批次）挂起，一致则批次条目落地。同一通知单重传时已落地船跳过，挂起船按最新批次状态重新判定补落地，不重复占额度。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、潮位窗口与批次编排、改期重算失败与重试、闸口拦截、并发冲突草稿以及引航回传幂等对账。
