# Changelog

> 按天归纳（2026-08-24 → 2026-11-07）。

## 202611 w1

- **Remove**: **「我的团队」整体移除**（用户拍板「都删」）。删除规模：后端 `app/api/teams.py` 1396 行 + `app/teams/{schema,service,sweeper,wakeup}.py` 4028 行（8 张表 / 30 端点）；前端 `TeamsPage` 623 + `TeamDetailPage` 1809 + `TeamChatPage` 41 + `TeamCard` 81 + `TeamCollaborationPanel` 521 + `BiddingArena` 521。**入口**：侧栏「我的团队」、切换器团队分组、广场「团队对话」tab、对话页团队协作面板、`ChatHeader` 的「返回团队」、会话筛选团队分组、`App.tsx` 三条路由与 `scopeTeams/loadTeams/selectTeamScope`。**后端核心链路**：`main.py` 的 router/sweeper、`chat.py` 的 TL 会话注入与 team 权限过滤、`models.py` 8 张表类、`session_schema` 的 `TeamPlannerContext` / `interaction_mode=team_tl|team_task` / `execution_target=team_member`、`turn_run` 的 team planner frames、`planner`/`planner_fastpath` 的 `team_context` 参数、`stage_protocol` 与 planner prompt 的 team 指令。**刻意保留（不可误删）**：① `ChatSession.team_id` 列 + `ChatSessionRead.team_id/team_name`（历史会话仍可读，team_name 恒 None，不再查团队表）—— 原「团队会话对本租户成员/管理员开放」的放行分支随 Team 表一起被删后，**实测管理员打开历史团队会话 404**，已在 `_get_readable_chat_session` 补回最小放行（`row.team_id and role == 'admin'` 只读可读，不扩大到普通成员）；② `message_visibility.py` 对 `interaction_mode=="team_tl"` 旧消息的过滤与「从旧 prompt 恢复原始用户文本」；③ `agent-scope-storage` 的 `team:` 前缀识别（老 localStorage 残留折成「未选员工」，否则会被兜底改写成某位员工）；④ `MessageBubble` 的 `team_progress`（历史消息进度）；⑤ 侧栏历史群聊行（改用 `session.team_name` + 「历史群聊」副标题）。**未执行任何 DROP TABLE**。前端随之把「会话行头像叠放」改成普通头像（原实现依赖 `TeamRead.members`）；i18n 删掉 98 条团队专属词条、保留 5 条泛用员工描述。测试：删 `test_teams_*.py` 6 个 + `EmployeeGalleryPage.test.tsx`（整文件只测团队 tab）+ 前端团队用例若干；改 5 个受签名影响的测试（`test_planner_fast_path` 9 处、`test_planner_followup_shortcircuit` 8 处末尾 `None`、`test_harness_v2` 3 个团队规划用例、`test_harness_session_cleanup` 的 `delete_team` 用例、`test_agent_delete_cascade` 的 TeamMember 断言）；新增 2 条防回归（侧栏无「我的团队」、全站不再请求 `/api/enterprise/teams`）。验证：前端 tsc 0 错、**全量 vitest 85 文件 / 620 用例全绿**、build 11.29s；后端 **17 failed / 2293 passed**（17 条全在既有基线名单）；浏览器实测侧栏无团队入口、切换器无团队分组、广场 tab 4→3、旧路由 `/enterprise/teams` 落到 `/enterprise/dashboard`、43 个 API 请求中 **0 个** teams 接口
- **Feat**: **对话耗时拆成「首 token」+「总耗时」**（用户拍板口径）。后端 `observability/session_timings.py` 的 trace 投影新增 `first_token_ms`：取 `response.generate_stream` span 的 `started_ms + ttft_ms` 减去 trace 起点，**只认流式作答调用** —— 非流式的 `ttft_ms` 等于整个生成时长，用它会把首 token 标成接近总耗时，所以不参与投影；兜底回复 / 无模型调用的轮次字段直接缺省。前端 `TurnTrace.firstTokenMs` → `traceTiming` → `TurnDurationNote` 渲染「首 token 1.2s · 总耗时 8.9s」，无首 token 数据时只显示总耗时（流式期间仍只有本地实时总耗时）；`title` 说明两项口径（首 token = 消息发出到第一个字出现；总耗时 = 消息发出到答案落库）。测试：后端 `test_session_trace_timings.py` 补 2 条（首 token 计算 + 非流式不产出首 token 的反证），前端 `MessageList.test.tsx` 补 1 条
- **UI**: **「我的静态资源」用法提示醒目化**

- **UI**: **「我的静态资源」用法提示醒目化**
- **Fix**: **对话气泡「总耗时」不再出现「答案都出来了，计时还在涨」**
- **Fix**: **员工小队档案「配置成员 / 去配置 / 调整」改为就地打开编辑弹窗，不再跳转**
- **Remove**: **员工小队档案「对话日志」去掉点踩归因摆设**
- **Fix（已被上一条取代，保留演进记录）**: **员工小队档案页「去配置 / 调整 / 配置成员」跳转页面错误**
- **Fix**: **技能管理 / 工具管理 tab → 新建/编辑 → 点「返回」落点不对**
- **New**: **员工小队卡片新增「员工小队档案」入口**
- **Improve**: **员工小队档案页 hero 按钮布局改回左侧头像列下方**
- **Improve**: **员工小队档案总览从 1 张卡扩到 4 块**
- **Fix**: **作用域被兜底改写 / 侧栏切换器下拉被截断**
- **Fix**: **员工小队作用域下点任何「新建」都被送回档案页** 
- **Fix**: **员工小队作用域下的冷启动竞态**
- **Fix**: **员工小队转接后的两处渲染缺陷**
- **Improve**: **「多智能体」统一改称「员工小队」**
- **New**: **员工小队（Dispatcher）** 
- **New**: 员工小队成员需各自填写路由规则（什么情况转给它），无法路由时按「澄清追问 / 默认成员 / 候选列举」兜底；缺规则的成员在卡片上直接标「缺规则」
- **New**: 员工小队可发布到开放广场的同名 tab，广场卡片支持试用与引用；被员工小队引用的员工需先解绑才能删除（员工卡显示「被 N 个员工小队引用」）
- **Improve**: 「员工小队」独立为「我的数字员工」页的一等 tab，与「技能管理」并列并排在其之前（顺序：数字员工 / 员工小队 / 技能管理 / 知识库 / 工具管理 / 静态资源）；创建入口收进该 tab，空状态给出创建引导
- **Improve**: 左下侧栏随作用域切换 —— 选中员工小队时，导航从「员工能力（知识库/技能/SOP/工具）」换成「运营记录 + 成员与路由规则」，头像换成员工小队拓扑图标；作用域下访问知识库/技能/工具路由会自动回到员工小队档案
- **Improve**: 档案页（`/enterprise/dashboard`）适配员工小队：展示基础介绍、成员与路由规则、兜底与模型归属；「档案总览 / 定时任务 / 对话日志」做成**页内 tab**，停在档案页即可直接切换（此前只能从侧栏另点入口进入，档案页本身没有可切的位置）
- **Fix**: 员工小队相关页面文字溢出 —— 卡片底部按钮改为「主行动独占一行 + 管理动作一行」，窄屏（≤1100px）下「去对话」不再被压成竖排、「从广场下架」不再折两行；档案页卡片去掉固定 `pl-[226px]` 缩进（窄屏 hero 竖排后会把卡片推右）；名称/徽标/规则文案补 `min-w-0 truncate` 与 `whitespace-nowrap`
- **Fix**: 定时任务与对话日志表格在 1280px 下首列表头「定时任务」压在「计划」上（`DataTable` 只有全部列都有 `width` 时才把 `minWidth` 接到 table，缺宽度的列会被 `table-fixed` 压成 0 宽）；已给任务表、执行记录表、会话执行记录表补齐列宽
- **Improve**: 对话端适配 —— 员工小队的路由结论以「已转交给 X」的旁白气泡呈现，成员答案紧随其后并持续流式；代码侧已收敛进 `docs/chat-mechanism.md`，涉及对话的改动先读它

## 202610 w4

- **Improve**: 上下文 Token 预算上限 262,144 → **1,048,576**（对齐 1M 上下文模型）；前端提示/校验与后端字段校验、运行时归一化共用同一个常量，不再出现「API 放宽、运行时仍截断」
- **Fix**: 搜索框聚焦时出现「左右两条竖线」——岛焦点环（未分层 CSS）压过了输入框自带的 `outline-none`，又被复合搜索框的 `overflow-hidden` 裁掉了上下两边；现在显式写了 `outline-none` 的输入框不再在岛内画环，焦点提示回到容器的深色边框
- **New**: 个人知识库（不再强绑数字员工）——「我的数字员工」新增「知识库」tab，可独立维护 / 下线（只允许下线，保证外部引用稳定），并支持「从我的知识库导入」到任一数字员工
- **New**: 个人知识库页「API 接入」指引（对齐 OCR 助手样式）：账号级密钥 + `POST /v1/knowledge-bases/{kb_id}:search` 端点 + cURL/Python/Node 示例，接入信息由 `GET /api/enterprise/knowledge-bases/api-access` 下发
- **Improve**: 个人知识库的 `knowledge_base_id` 可在「列表行 → 复制知识库 ID」或「API 接入」弹窗里一键复制（外部系统只需保存这一个 id）
- **New**: 开放 API 新增**集合检索** `POST /v1/knowledge:search`：一次调用检索「我的全部个人知识库」（库内所有文档都在范围内、跨库统一排序），不传 `knowledge_base_ids` 即全量、传了只查指定库；与「仅知识库问答」同一条检索链路（同版本 pin / 同路由缓存），回包带 `knowledge_bases[]` 版本快照；单库端点保留为语法糖
- **New**: 开放 API 新增账号级个人知识库接口（列表 / 详情 / 文档 / `:search`），供 CMLG Office 等外部系统作问答与改文档的依据；非属主 404、下线库 409 `KNOWLEDGE_BASE_ARCHIVED`

## 202610 w2-w3

- **Fix**: 全局时间格式统一，修复时区问题
- **New**: 开放工具广场，并优化工具相关字段
- **New**: AI 单据助手; 增加免登按钮
- **New**: 增加全局模型，用于数字员工的体验; 增加工作台首页
- **Perf**: 优化页面加载速度以及进场效果;优化首页样式
- **Perf**: 优化登录页面;优化全站页面底色


## 202609 w4

- **New**: 引入open-code-review，用于代码审查
- **New**: Agent广场，增加可直接使用的agent
- **Perf**: 调整页面布局


## 2026-09 w3

- **新功能**：决策助手 laya
- **新功能**：可以使用 /或@ 手动选择skill或sop，提升准确率和速度
- **优化**: 统一相同插件在不同位置行为不一致的问题
- **优化**: 针对大附件产物的下载优化
- **新功能**：技能广场的技能支持下载 zip 包; 支持技能分享外链;
- **优化**: 针对大附件上传/下载，增加进度条; 分享链接机器人的答案隐藏来源展示;
- **新功能**：数字员工支持分享链接;技能广场解耦数字员工，成为公司内部 Skill 分享平台
- **修复**：新建/编辑技能点保存不再误报「技能包缺少 SKILL.md 引用文件」
- **调整：技能创建/导入完成后回到技能广场;站点默认页改为开放广场平台**
- **安全：堵住放开成员发布后的两处越权**
- **修复：API Key 管理的两个复制按钮点击无效;管理员无法删除广场技能;广场宿主可被误归档或被重复创建**
- **性能：知识检索结果内联（不再让模型盲读 160KB JSON）** — `knowledge_search` 结果在 `app/knowledge/search_payload.py` 里精简投影后直接进 tool result：去掉桶 metadata 里的整桶原文、evidence 的 `excerpt` 副本、与 `route_trace` 重复的 `trace`、下游无人读的 `expanded_sections`，正文按预算裁剪。实测 90KB → 22.5KB，**引用与回答上下文逐字节不变**（引用由完整 payload 派生）。模型不再需要「读文件→读不全→换词重搜」两轮，预计 35s 问答降到 18–22s。
- **修复：引用编号跳号（正文 `[n]` 与卡片列表对不上）**

## 2026-09 w2

- **新功能：扫描 PDF 云端解析（MinerU）**
- **修复：聊天中「生成文件」出现空文件;打字每击键触发 4 个请求**
- **性能：聊天页轮询治理;送瞬间重复请求合并;Planner 快速路径**
- **修复：剩余 3 处 `window.confirm` 全部统一到 `ConfirmDialog`;表单提交的取值未落库**
- **新功能：A2UI 缺槽表单（MVP）** — SOP 缺槽反问从「让用户写一段自然语言」升级为**下发可渲染表单**
- **修复（根因）：API 密钥弹窗被撑成整屏宽;新建数字员工后列表不刷新（需手动刷页才可见）**
- **架构：知识库文档解析任务迁到 rq（离开 web 进程线程池，可重启恢复）**
- **修复：`output_policy` 的超时上限被 SDK 重试悄悄放大**
- **修复：文档入库的 LLM 阶段补上时间预算 + 入库期「处理中」可见**
- **体验：API 密钥弹窗的「禁用」改用统一二次确认**
- **体验：SOP 缺槽反问里的字段名改中文（零 LLM 模板回复不再抛英文标识符）**
- **性能：SOP 边条件结构化——「复杂条件判断」从每次 LLM 变一次性编译**
- **性能：知识路由缓存重构（两维独立 + 精准失效 + 长 TTL）**

## 2026-09 w1

- **定时任务：调度迁移到 rq**（`c094dc3` `4ca7a7a`）
- **工程化：启动命令收进 `pyproject.toml`**
- **管理端：新增「超级管理员 → 任务监控」**
- **定时任务：修复 rq worker 启动后自动退出**
- **前端：修复所有「二次确认」弹窗被拉成全屏宽**
- **配额管理：用量口径重构;修复未来月可查看**（`6be6660`）
- **性能**：LLM 请求按操作收紧超时（`a1779d0`）
- **性能：知识检索提速**（`a1991b2`）— planner normalize 提升检索效率；新增知识路由缓存 `knowledge/route_cache.py`；对话上下文与 LLM client 配套调整
- **性能：SOP 确定性执行链**（`ed1d2ba` `57eb9c7` `019bd8b` `eb7d595`）— 决策直判 / 工具直组 / handoff 直通 / 槽位 fresh_merge，大幅压缩对话链路耗时
- **配额**：修复云端重置后不更新的问题（`ba91e15`）
- **性能优化：Redis 支持**
- **Harness 流程优化**
- **前端**：JSON 文本输入框体验优化（`6c90fc1`）
- **OA SSO 单点登录**（`6ba6996`）
- **LDAP/域登录相关**：支持更新部门（dep）信息（`e798cfe`）
- **密钥管理**：同步消费者备注（`4fe02cf`）
- **UI**：修复输入框 placeholder 显示问题（`a2fa42c`）、调大 Logo（`31f28da`）
- **配额管理重构**：支持配额反向绑定消费者（`c943783`）；修改配额绑定个人的方式（`4e3de19`）

## 2026-08

- **性能**：意图识别优化合入（PR #1，`5a8c918`）
- **后端**：新增日志配置（`8a27846`）、修复日志配置（`8a58a8f`）；修复 PG 不支持的 SQL 方言（`8c2c3ae`）
- **待回答（handoff）**：卡片显示发起人名字（`ded2664`）
- **模型配置**：优化意图识别专用模型的编辑与显示（`b772244`）
- **聊天界面**：右上角用户信息菜单与管理页统一（`22904f5`）
- **修复**：修复引用片段数量统计（`35594fb`）、修复多条引用无法滑动（`20525f3`）
- **运行时**：配置对话上下文压缩（compaction，`9cc1840`）
- **观测**：增加对话耗时显示（`e5dd5bf`）
- **SOP**：修复 SOP 无法选中处理人的 bug（`26db8ea`）
- **团队（teams）**：成员溢出可发现性修复（`59ed372`）、支持在线预览完整日志（`ff23324`）
- **技能（skills）**：修复蒸馏能力（distilled capabilities）解析（`3e7a0b2`）
- **交接（handoff）**：按渠道路由指派人通知（#200，`008fff3`）
- **API Key 审批模块落地**：新增 API Key 审核相关模块（`3d51641`）、支持自定义模型（`207f4eb`）
- **消费组**：支持修改消费组、增加归属字段（`c4e6a99`）；配额管理 bugfix（`6d8e009`）
- **修复**：harness 恢复已发布交付物（`ad5f6d9`）、技能私有包副本保护（`e01703b`）、聊天中只显示已授权团队（`b06b06b`）

---
