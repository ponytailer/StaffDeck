# Changelog

> 按天归纳（2026-08-24 → 2026-09-14）。

## 2026-09-14

- **性能：SOP 边条件结构化——「复杂条件判断」从每次 LLM 变一次性编译**
  - **根因**：前端 `DistillPage` 的结构化条件预设（`tool_success` / `user_confirmed` / `missing_slots([])` …）在写入时被 `conditionFromPreset()` 翻译成中文落库，语义当场丢失；后端只看到自由文本，每次 SOP 推进都得让主模型重新理解一遍，条件边全部回落 LLM
  - **① 契约**：新增 `EdgeConditionSpec`（`always` / `slots_all` / `slots_any` / `slots_missing` / `slot_compare` / `user_confirmed` / `user_rejected` / `result_ok` / `result_failed` / `llm_judge`）+ 纯函数求值器（`app/skills/edge_condition_spec.py`），求值**三态**：命中 / 明确不命中 / **未知（交还 LLM）**
  - **② 离线编译**：`edge_condition_compiler.py` 两级策略——前端预设文本逐字直译（零 LLM）+ LLM 批量编译整图剩余条件边（一次调用）；模型臆造的字段名/非法 kind 一律丢弃，失败保持自由文本
  - **③ 运行时求值（场景 G）**：`sop_step_executor` 用槽位 + 上一步能力调用结果 + 用户消息求值，**唯一命中即直判，零 LLM**；只要有一条边求值未知就整体交还 LLM（未知 ≠ 不命中），有编译产物时不再退回无条件边兜底（否则等于擅自走 else）。未编译的图行为与改造前**完全一致**（场景 D 保留为兜底）
  - **异步编译**：写路径（create / update / publish / 回滚 / 删除清理）只入队不等待；rq 独立 `skill_compile` 队列，Redis 不可用自动降级进程内异步队列；按条件指纹跳过已编译边，重复入队零成本
  - **产物存储**：`skill_edge_conditions` 表 + `(tenant_id, skill_id, condition_fingerprint)` 唯一键——不写回 `content_json`（前端 TS 类型与 SkillEditor 局部改写会把无类型字段静默丢掉）；指纹含「边端点 + 条件原文 + 源节点必填字段」，条件一改旧结果自然失效
  - **人工复核数据面**：`GET /{skill_id}/edge-conditions` 返回每条条件边的 status / kind / 可读描述 / 置信度 / 模型理由；`POST /{skill_id}/edge-conditions/compile` 支持异步或 `sync=true` 排障
  - **历史回填脚本**
  - 实测（真实库 30 个技能/分支、176 条非无条件边）：`--no-llm` 零成本直译 24 条（另有无条件边 13 条本来就可直判）
  - 测试：新增 `test_edge_condition_spec.py`(55) + `test_edge_condition_compiler.py`(20) + `test_edge_condition_jobs.py`(18)，`test_sop_step_executor.py` 扩到 67 例（含场景 G / 抽取后直判 / 未知边否决 / 集成透传），相关回归 155 例全绿
- **性能：知识路由缓存重构（两维独立 + 精准失效 + 长 TTL）**
  - **两维独立**：document 与 bucket 拆成独立 key、独立写回条件，任一路走 LLM 成功即可缓存该路决策；旧实现要求「两路都走 LLM 成功」才写，「doc 词法快速路径 + bucket LLM」这类组合一个字都不缓存
  - **key 补版本维度**：`knowledge_base_version_ids` 进入 key 指纹，换版本不再命中旧决策（旧实现命中后被候选过滤清空）
  - **失效安全**：命中后按当前候选集过滤 + 按 `max_documents`/`max_buckets` 截断；过滤为空视为缓存失效**回退 LLM**（旧实现直接返回「没有足够相关的知识」并静默持续到 TTL 到期）
  - **精准失效**：写回时按 (tenant, kb_id) 登记索引集合，知识库写路径按库删除，不再整租户清空；补齐 `DELETE /knowledge-bases/{id}`（原缺失失效）
  - **TTL 1800s → 12h**（新增配置 `KNOWLEDGE_ROUTE_CACHE_TTL_SECONDS`，默认 43200），仅作兜底
  - 实测（`scripts/probe_knowledge_route_cache.py`）：未命中 11.4s → 命中 **0.4s**；TTL 43199s；按库失效不误伤其它库/租户

## 2026-09-11

- **定时任务：调度迁移到 rq**（`c094dc3` `4ca7a7a`）
- **工程化：启动命令收进 `pyproject.toml`**
- **管理端：新增「超级管理员 → 任务监控」**
- **定时任务：修复 rq worker 启动后自动退出**
- **定时任务：新建保存后回到任务列表**
- **前端：修复所有「二次确认」弹窗被拉成全屏宽**
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
