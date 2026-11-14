# 数仓治理与运维平台

面向数据仓库团队的治理与运维一体化平台。覆盖表生命周期管理、存储成本优化、数据热度分析、任务依赖编排、SLA告警、资源弹性调度与数仓健康度评估的完整治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 表生命周期管理 | `lifecycle` | 生命周期策略 | 策略编号、表名称、创建日期 |
| 存储成本优化 | `cost` | 成本分析报告 | 报告编号、分析范围、存储总量 |
| 数据热度分析 | `heatmap` | 热度记录 | 记录编号、表名称、查询次数 |
| 任务依赖编排 | `dag` | 依赖图节点 | 节点编号、节点名称、上游节点 |
| SLA告警管理 | `sla` | SLA规则 | 规则编号、监控对象、产出时间 |
| 资源弹性调度 | `resource` | 资源池配置 | 配置编号、资源类型、最小配额 |
| 数仓健康度评估 | `score` | 健康度报告 | 报告编号、评估范围、存储健康分 |
| 数据漂移检测 | `drift` | 漂移记录 | 记录编号、表名称、漂移字段 |
| 数据资产盘点 | `catalog` | 资产记录 | 资产编号、表名称、资产分类 |
| 数据保留策略 | `retention` | 保留规则 | 规则编号、数据集范围、保留期限 |
| 数据去重管理 | `dedup` | 去重任务 | 任务编号、目标表名、去重字段 |
| 数据分层规划 | `tier` | 分层定义 | 分层编号、分层名称、层级别名 |
| 基线监控管理 | `baseline` | 基线配置 | 基线编号、基线名称、承诺时间 |
| 数据回收站 | `recycle` | 回收记录 | 记录编号、表名称、删除时间 |
| 变更审计追踪 | `audit` | 变更记录 | 记录编号、变更对象、变更类型 |
| 查询性能分析 | `benchmark` | 性能报告 | 报告编号、查询语句、执行耗时 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 漂移归并规则（drift）

同一漂移字段在连续采样窗口里不再反复生成新记录，统一在 `services/drift.py` 归并：

- **聚合粒度**：按「表名称 + 漂移字段 + 采样窗口」聚合重复采样；窗口长度 24 小时
  （`WINDOW_HOURS`），归并顺序为采样窗口升序（`MERGE_ORDER`），均可调整。
- **保留口径**：归并记录保留首次偏差、最近偏差、样本数与结论版本；原始样本（含噪声
  样本与原基线）写入采样历史，`GET /api/drift/history` 始终可查。
- **噪声阈值**：`|偏差比例| < 20%`（`NOISE_THRESHOLD`）视为采样噪声，只计入样本数，
  不推进结论、不覆盖最近偏差；已确认的历史偏差保留检测时间和原基线，噪声过滤
  （`hide_noise=true`）不会将其隐藏。
- **幂等与续跑**：同一窗口相同数值的重复采样幂等忽略；`POST /api/drift/merge`
  中断后从失败窗口继续，已收口窗口的证据不重复累计。
- **冻结基线**：记录确认后冻结基线，两个窗口同时写同一字段时以冻结基线为准。
- **下游同步**：归并结论同步到健康度报告（SCOR-0002 只保留最终字段结论）和查询
  性能报告（BENC-0002 同步显示受影响表）。
- **定位**：`GET /api/drift/locate` 按字段、状态、时间窗口筛选后定位最新未收口
  记录，带 `after` 参数时定位到下一组记录。

领域：数据工程
子方向：数仓与数据湖
