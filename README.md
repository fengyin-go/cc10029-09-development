# 智能爬虫采集平台

面向网页爬取、API接入、增量抓取、反爬对抗与采集质量校验的数据采集平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集目标 | `targetsite` | 目标站点 | 站点编号、站点名称、入口URL |
| 抽取规则 | `selector` | 选择器规则 | 规则编号、归属站点、列表选择器 |
| 爬虫引擎 | `crawler` | 爬虫任务 | 任务编号、任务名称、关联站点 |
| 代理池 | `proxy` | 代理节点 | 节点编号、IP地址、端口号 |
| 反爬对抗 | `antiblock` | 反爬策略 | 策略编号、策略名称、触发条件 |
| 定时调度 | `schedule` | 调度计划 | 计划编号、关联任务、Cron表达式 |
| 数据提取 | `extractor` | 提取模板 | 模板编号、模板名称、输入URL |
| 采集校验 | `validate` | 校验规则 | 规则编号、校验字段、期望格式 |
| 失败重试 | `retry` | 重试策略 | 策略编号、适用任务、最大重试 |
| 存储落地 | `storage` | 存储配置 | 配置编号、存储类型、连接信息 |
| 重复去重 | `dedup` | 去重策略 | 策略编号、业务键列、去重算法 |
| 质量报告 | `quality` | 质量指标 | 指标编号、指标名称、统计口径 |
| 运行监控 | `monitor` | 监控大盘 | 大盘编号、关联任务、采集速率 |
| 告警中心 | `alert` | 告警规则 | 规则编号、监控对象、告警条件 |
| 采集API | `apigate` | API配置 | 配置编号、端点路径、请求方法 |
| 历史归档 | `archive` | 归档策略 | 策略编号、数据来源、归档周期 |
| 模板市场 | `template` | 采集模板 | 模板编号、模板名称、适用站点 |
| 采集流程 | `flowline` | 流程编排 | 流程编号、流程名称、步骤序列 |

## 调度批次清单与待下发任务清单

定时调度按「调度计划 + 执行窗口」展开出调度批次清单，爬虫引擎再消费批次生成待下发
任务清单。两份清单是派生台账，放在 `backend/app/store.py` 的 ledger 里，不计入模块统计。

| 台账 | 生产方 | 消费方 | 关键字段 |
| --- | --- | --- | --- |
| 调度批次清单 | 定时调度 | 爬虫引擎 | 批次编号、执行窗口、触发来源、状态、校验结论 |
| 待下发任务清单 | 爬虫引擎 | 下发执行 | 任务清单编号、批次编号、下发时间、状态 |

业务规则（实现在 `backend/app/services/batches.py`、`dispatches.py`、`windows.py`）：

- 窗口边界由 `windows.parse_window` 统一判定；缺少窗口或边界不清晰的计划**不会**产生
  「待执行」批次，Cron 无法解析时同样直接拒绝。
- 同一计划在同一执行窗口只保留一个批次：手动触发命中已存在的窗口时把来源并入
  `合并触发来源`，调用方拿到的仍是同一个批次。
- 状态只能 待执行 → 执行中 → 已完成 / 失败 依次推进；重试只能 失败 → 执行中，
  已完成批次不能回退。
- 批次与任务清单在同一事务里写入（`store.transaction()`），落库失败整体回退，
  任务清单不会读到半份批次；重复送交只读取已存在结果，不重复写入。
- 下发时间必须落在批次窗口内，窗口未到或已过都会被拒绝。
- 批次校验结论同步到任务清单，两份台账的结论保持一致。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/schedule/plans` | 批次面板的计划下拉 |
| GET/POST | `/api/schedule/batches?plan_id=` | 查询 / 生成调度批次清单 |
| GET | `/api/schedule/batches/export` | 导出批次清单 CSV |
| GET | `/api/schedule/batches/{id}` | 单条批次明细 |
| POST | `/api/schedule/batches/{id}/actions` | 开始执行 / 标记完成 / 标记失败 / 重试执行 |
| POST | `/api/schedule/batches/sync` | 按当前时间推进批次状态 |
| GET | `/api/schedule/{plan_id}/batches` | 按计划查看批次序列、窗口与合并情况 |
| GET/POST | `/api/crawler/dispatches` | 查询 / 生成待下发任务清单 |
| GET | `/api/crawler/dispatches/export` | 导出任务清单 CSV |
| POST | `/api/crawler/dispatches/sync` | 同步批次校验结论 |
| POST | `/api/crawler/dispatches/{id}/actions` | 下发任务（动作名：`下发任务`） |

原有的时间线、导出、登记与动作接口返回结构保持不变。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
