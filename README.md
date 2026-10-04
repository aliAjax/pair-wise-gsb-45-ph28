# 港口泊位与航道调度

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、潮位窗口通行批次排程和引航回传对账。
- `src/repository.py`：SQLite建表、事务和查询（批次世代、重算留痕、冲突草稿、回传单与额度占用）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景、批次重算/冲突草稿/引航对账测试。

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
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。支持confirm/berth/depart/cancel，以及`reschedule`改期。

### 潮位窗口与通行批次

靠泊计划与航道通行批次分开排：批次是独立实体，由潮位窗口（`tide_windows`，默认有0-24时DEFAULT窗口）按规则自动派生。顺位先到先排（eta升序，同时刻高风险优先），每批`batch_quota`艘、窗口最多`max_batches`批。批次按世代（generation）管理：

- 计划改期（`reschedule`）或窗口重算时，同窗口旧批次先置`superseded`，再写新一代`active`批次，闸口只认`active`，不会放行已改期船。
- 改期与重算在单事务内完成：**重算失败整体回滚**，原批次与顺位保留，失败原因写入`recompute_runs`，值班员可重试（手动触发见下）。
- 两个调度员同时修改同一计划时，先到版本生效，后到者收到`409 draft_conflict`，其改动以冲突草稿保存，不覆盖线上版本。

接口：

- `GET /api/windows`：潮位窗口列表。
- `POST /api/windows`：新建窗口`{"data":{"code","start_hour","end_hour","batch_quota","max_batches"}}`，建后自动重排。
- `POST /api/windows/{id}/recompute`：值班员手动重试重排。
- `GET /api/batches?window_id=`：批次列表（默认仅active，含成员快照）。
- `GET /api/gate?window_id=`：闸口放行视图，只返回active批次。
- `GET /api/recompute-runs?window_id=`：重算记录（含失败原因）。
- `GET /api/drafts?plan_id=`：冲突草稿列表；`GET /api/drafts/{id}`详情。
- `POST /api/drafts/{id}/apply`：基于最新版本重新应用草稿；`POST /api/drafts/{id}/discard`放弃。

### 引航回传对账

引航员回传进出港时间后逐船对账：找不到计划、不在当前有效批次、时间与计划不一致的船一律**挂起**（不落额度），其余落地并占用通行额度。同一回传单（`ticket`）重传是幂等的：已落地船跳过，只补未落地船；额度按(计划,方向)唯一约束占用，不会重复扣。

- `POST /api/pilot-reports`：`{"ticket":"RT-1","items":[{"vessel","movement":"in|out","actual_hour":6}]}`。
- `GET /api/pilot-reports` / `GET /api/pilot-reports/{ticket}`：回传单及逐船对账结果（landed/suspended/quota_consumed汇总）。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。回传接口要求`pilot`角色，窗口与计划维护要求`port_controller`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
