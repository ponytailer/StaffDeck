# Changelog

> 按天归纳（2026-08-24 → 2026-09-11）。

## 2026-09-11

- **定时任务：调度迁移到 rq**（`c094dc3` `4ca7a7a`）— PG 为主源、Redis 仅承载触发 job；新增 `SCHEDULER_BACKEND` 开关（`rq` 默认 / `poll` 回退），rq 触点收敛到 `app/scheduled_tasks/rq_dispatch.py` + 独立 worker 进程
- **工程化：启动命令收进 `pyproject.toml`** — 新增 `[project.scripts]` 入口点 `uv run serve`（`app/cli.py:serve`，等价于 `uvicorn single_port_app:app --host 127.0.0.1 --port 5173 --log-config log.json --reload`）与 `uv run rq-worker`（`app/scheduled_tasks/rq_worker:main`）；注意 uv 不支持 `[tool.uv.scripts]` 自定义别名，只能走入口点
- **管理端：新增「超级管理员 → 任务监控」** — 侧边栏「运行设置」重命名为「超级管理员」（该页本就仅管理员可见）；页内拆分为「运行设置 / 任务监控」两个 Tab
  - 后端新增 `/api/enterprise/scheduled-task-monitor/{overview,tasks,runs}`（`app/api/scheduled_task_monitor.py`，统一 `require_tenant_admin` 鉴权），概览聚合任务/执行记录计数与 24h 失败，运行时状态来自 `rq_dispatch.runtime_status()`（队列计数 + worker 心跳，Redis 不可用时优雅降级）；列表用批量 JOIN/IN 查询避免逐行回源
  - 前端 `ScheduledTaskMonitorTab.tsx`：6 张指标卡 + 调度运行时面板（rq/Redis 状态、5 个运行时计数、worker 列表与当前任务）+ 全部定时任务表（筛选/搜索/分页，支持立即执行、暂停启用、删除）+ 执行记录表（状态筛选、按任务筛选、查看会话、重新执行），15s 自动刷新可切换
- **定时任务：修复 rq worker 启动后自动退出** — worker 复用带 `socket_timeout=1.5` 的连接，而 rq 靠 `BLMOVE` 长阻塞等任务（`dequeue_timeout ≈ 405s`），阻塞读被 socket 读超时打断后 `Worker.work()` 捕获 `TimeoutError` 直接退出（日志：`Redis connection timeout, quitting...`）。现按用途拆分连接：`blocking` 档（worker 出队）不设读超时、交给 rq 自己按 `dequeue_timeout + 10` 兜底，并开 `socket_keepalive` 防长阻塞连接被中间设备判死；`inspect` 档隔离监控只读（rq 构造 `Worker` 会改写连接池超时，避免污染控制面连接）
- **前端**：消费组管理表移除「网关」「类型」列（`bc6419f`）

## 2026-09-10

- **配额管理：用量口径重构**（`6be6660`）
  - 修复管理员视角用量陈旧：拆除快照 `max` 防回退护栏——云端成功返回的 `usedAmount`（含重置后的低值/0）一律为准，仅响应缺字段时保持原值
  - 新增月内窗口重置累计语义：快照表加 `archived_used_amount`（重置前累计「快照1」，冻结）与 `reset_count`（重置次数）；云端值 < 本地当前窗口值即判定重置，旧值归档、云端新值起算「快照2」
  - 展示统一为月累计 = 快照1 + 快照2；使用率 = 月累计 ÷ 有效配额（每次窗口总量之和，重置 1 次即配额 ×2）
- **配额管理：修复未来月可查看**（`6be6660`）— 后端 `is_current_month` 改为严格 `==`，未来月直接返回空；前端月份前进按钮到当月即禁用
- **性能**：LLM 请求按操作收紧超时（`a1779d0`）— turn_planner / harness 90s、knowledge 60s、slot_extraction 等 30s；生成类流式保持默认
- **前端**：侧边栏隐藏「渠道接入」入口（`6be6660`）；使用统计表移除「网关」列、已用副文本单行显示、配额列防折行（`a8cb6e5`）

## 2026-09-09

- **性能：知识检索提速**（`a1991b2`）— planner normalize 提升检索效率；新增知识路由缓存 `knowledge/route_cache.py`；对话上下文与 LLM client 配套调整
- **性能：SOP 确定性执行链**（`ed1d2ba` `57eb9c7` `019bd8b` `eb7d595`）— 决策直判 / 工具直组 / handoff 直通 / 槽位 fresh_merge，大幅压缩对话链路耗时
- **配额**：修复云端重置后不更新的问题（`ba91e15`）
- **脚本**：`diagnose_planner_latency.py` 等诊断脚本修复（`6d6d79b`）

## 2026-09-08

- **性能优化：Redis 支持**（#3 #2 两个 PR 合入，`e8d871f`）— 引入 Redis，修复响应慢问题（`c6d1c5c` `498ad37`）；对话性能优化并加入 changelog（`9d507d5`）
- **Harness 流程优化**：编排流程（`50339e9`）与 branch 逻辑优化（`1d45c70`）
- **账号**：移除开放注册入口（`1d45c70`）
- **前端**：JSON 文本输入框体验优化（`6c90fc1`）
- **清理**：删除飞书（Feishu）渠道代码（`081e981`）

## 2026-09-07

- **OA SSO 单点登录**（`6ba6996`）
- **LDAP/域登录相关**：支持更新部门（dep）信息（`e798cfe`）
- **密钥管理**：同步消费者备注（`4fe02cf`）
- **UI**：修复输入框 placeholder 显示问题（`a2fa42c`）、调大 Logo（`31f28da`）

## 2026-09-04

- **配额管理重构**：支持配额反向绑定消费者（`c943783`）；修改配额绑定个人的方式（`4e3de19`）

## 2026-08-27

- **性能**：意图识别优化合入（PR #1，`5a8c918`）
- **后端**：新增日志配置（`8a27846`）、修复日志配置（`8a58a8f`）；修复 PG 不支持的 SQL 方言（`8c2c3ae`）
- **待回答（handoff）**：卡片显示发起人名字（`ded2664`）

## 2026-08-26

- **模型配置**：优化意图识别专用模型的编辑与显示（`b772244`）
- **聊天界面**：右上角用户信息菜单与管理页统一（`22904f5`）
- **知识引用**：修复引用片段数量统计（`35594fb`）、修复多条引用无法滑动（`20525f3`）
- 合入 Codex 团队 TaskFrame 编排（#221）

## 2026-08-25

- **运行时**：配置对话上下文压缩（compaction，`9cc1840`）
- **观测**：增加对话耗时显示（`e5dd5bf`）
- **注册**：增加用户注册（`df4ec24`）
- **SOP**：修复 SOP 无法选中处理人的 bug（`26db8ea`）
- **团队（teams）**：成员溢出可发现性修复（`59ed372`）、支持在线预览完整日志（`ff23324`）
- **技能（skills）**：修复蒸馏能力（distilled capabilities）解析（`3e7a0b2`）
- **交接（handoff）**：按渠道路由指派人通知（#200，`008fff3`）
- 同步上游 OpenBMB/main、rebase master

## 2026-08-24

- **API Key 审批模块落地**：新增 API Key 审核相关模块（`3d51641`）、支持自定义模型（`207f4eb`）
- **消费组**：支持修改消费组、增加归属字段（`c4e6a99`）；配额管理 bugfix（`6d8e009`）
- **修复**：harness 恢复已发布交付物（`ad5f6d9`）、技能私有包副本保护（`e01703b`）、聊天中只显示已授权团队（`b06b06b`）
- 合入 Codex 团队 TaskFrame 编排（#215）

---

*生成时间：2026-09-11 · 范围：2026-08-24 → 2026-09-11*
