# qa-agent × pi 范式对齐简报（给专家 agent）

> 背景：qa-agent（本仓库）是 roadbook 路书资产的无人值守 UI 回归 runner（看板 + FastAPI + CLI）。
> 参照系是 pi（github.com/earendil-works/pi，本地克隆（service_code/pi））的
> agent 运行时范式，完整架构图见飞书文档
> https://wcnrixwb6ud5.feishu.cn/docx/Bzt2dN2NioW6fpxXOQacCODcnxU （源码快照
> 桌面快照 `pi-agent-loop.mmd`）。本文由一次逐层对照分析得出，供后续改造决策使用。

## 场景约束（评估一切建议的前提）

- 无人值守批处理：跑批启动后人不一定在场；交互只在看板、只在收口点（needs_input → resume）。
- 单任务生命周期短：30 分钟墙钟 + 页面停滞 + 人工停止三道闸封顶（`runtime/loop.py`）。
- 被测对象是测试靶子（demo/dev 环境），非真实用户数据。
- 工具集静态且小（< 20 个），单主 agent + 独立裁判，无角色分工。
- 结论要结构化（verdict → 退出码 → 看板），这是产品命脉。

## 已确认对齐、无需动的部分

循环正确性层已与 pi 逐字对齐，不要重构：

| 范式点 | qa-agent 位置 | pi 参照 |
|---|---|---|
| 压缩（阈值触发、摘要+保留尾部、领域化交接说明） | `loop.py` compact_at / `_compact` | `agent-loop.ts` prepareRequest 自动压缩 |
| 截断安全（length 整轮丢弃重试） | CLAUDE.md 已锁 + 单测 | `agent-loop.ts:471` failToolCallsFromTruncatedMessage |
| 转录完整性（finish 后补 tool_result） | loop.py + 单测 | toolCall/toolResult 配对约束 |
| 工具校验前置、错误即 tool_result | `tools/base.py` + `tools/args.py` | `agent-loop.ts:708` prepareToolCall |
| subagent 上下文隔离 + 工具白名单 | judge + `ToolRegistry.schemas()` 裁剪 | pi Agent 工具 / subagent |
| 事件层（emit → EventStream → UI） | `runtime/events.py` SSE + 看板 | EVENTSYS 子图 |
| 会话恢复（transcript 回放 + 注入） | `session.py` execute/resume 共用 `_settle()` | pi session restore |
| 记忆闭环（facts 覆盖 / experiences 追加 / reflections 噪音闸 + brief 注入 + 人工投票） | `knowledge.py` + `/api/memory` | MEMREAD/MEMWRITE |

## 建议对齐（按优先级）

### P1 · mid-run steering（插话队列）—— 唯一高价值缺口

- 现状：人对照板只能「停止」（`cancel.py`）或等 needs_input 收口后 resume。runner 卡在错误流程上时，停掉重来浪费最长 30 分钟。
- pi 的做法：`agent-loop.ts` 的 `getSteeringMessages` 在循环启动、每轮 turn 结束、prepareNextTurn 之后三个时机取队列（:176/:295/:204-206），消息在下一请求前注入上下文，任务不中断。
- 最小实现：看板加插话输入框 → 每 run 一个 steering 队列（threading 结构，风格照 `cancel.py`）→ `loop.py` 主循环每轮 turn 边界取队首，作为 user 消息注入后继续。不需要 pi 的 one-at-a-time 策略（单插话源）。
- 验收：runner 执行中从看板插话「跳过当前步骤，直接验下一个」，下一轮动作即改变。

### P2 · before_request 钩子位（PREPAREREQ 轻量版）—— 为未来买的保险

- 现状：压缩检查长在 loop 体内，没有"每次请求前的标准扩展位"；换模型/网关/按场景路由都要改 loop 本体。
- pi 的做法：`agent-loop.ts:219-239` `config.prepareRequest(request, signal)` 返回 requestUpdate（context/model/thinkingLevel 三合一，未提供的键保留原值）。
- 最小实现：`AgentLoop` 每轮构造请求前调 `hooks.before_request(messages) -> RequestUpdate | None`；压缩检查迁入默认钩子。不做虚拟模型路由（单 provider LiteLLM 够用，钩子位建好后是 20 行的事）。

### P3 · 记忆 briefing 的检索与预算 —— 带触发条件再做

- 现状：`_memory_brief` → `memory.brief_for(scenario)` 按场景整份注入，无相关性排序、无 token 预算。
- 触发条件：brief 超过 ~2K tokens 或场景数 > 20 时实施；此前不动。
- 最小实现：brief_for 内做 top-k（TF-IDF 起步即可，不必上向量库）；给 briefing 设 token 预算，超限截断并注明。pi-mind（github shog-lab/pi-mind，`packages/memory`）的 FTS5+向量+图谱是规模化的完整参照，现阶段过重。

### P4 · token 级流式 —— 可选，最后

- 现状：SSE 是 step/frame 级（`events.py` emit + FrameStore）。
- 只在"调参时想看 runner 正在想什么"的场景有价值；pi 的 partial 消息替换语义实现成本不低。默认不做。

## 明确不对齐（non-goals，别顺手"补齐"）

1. **SP 分段 diff/运行时刷新**：pi 需要它是因为会话跑数小时、工具 loadout 动态变；qa-agent 任务 30 分钟封顶、工具集静态、SP 每次由代码现拼，漂移没有生长土壤。
2. **会话投影层（分支/context_edit/ProjectedSessionEntry）**：那是交互式会话的需求；qa-agent transcript 线性、单次生命周期，run.json 整段回放即全部需求。
3. **人工审批闸（pi 四层权限链）**：测试靶子场景下物理隔离（authoring browser=None、路径代码卡死、judge 白名单）优于审批。**留一个心眼**：runner 未来接触真实用户数据/生产环境时，第一个要补的就是 EXEC 前的 approval 位（可复用白名单机制）。
4. **动态工具装卸（MCP/tool_search/deferred）**：< 20 个静态工具无上下文经济问题；MCP 是生态需求不是架构需求。
5. **虚拟模型路由**：同 P2，钩子位就位后按需 20 行。

## 保持现状（qa-agent 优于 pi 的部分，防被"对齐"掉）

1. **finish 显式协议**：结构化产物 + 代码拒回（缺 facts / needs_input 缺 questions 给精确补全指引）。pi 靠"无工具调用即结束"，无结构化约束——QA 结论进看板/退出码靠这个协议，是命根子。
2. **三道安全闸 + 协作式取消**：墙钟 + 页面停滞 + 人工停止；cancel 走自然边界自查保证录屏落盘无孤儿进程。pi 无环境停滞感知。
3. **压缩提示词的领域化交接**（"引用编号全部作废、页面重新 observe"）：比 pi 的通用压缩说明更强。
4. **playbook 异步结构化 writer 与业务结论解耦**：资产失败只告警，不改判。
