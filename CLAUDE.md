# qa-agent

给 roadbook 路书资产做的**独立运行时** + 看板。让路书不再依赖 Claude Code / Codex 才能跑。

## 目录结构

```
agents/              agent.md（唯一主 agent）+ judge.md（独立验证）
prompts/             任务模板 *.md（跟 agents/*.md 同一约定：提示词是数据，不是 Python 字符串）
skills/roadbook/     一个技能（软链到资产仓库），场景是它内部的资源
frontend/            看板前端（单文件，零构建）
runtime/             引擎
  app.py             组合根 QaAgent：CLI 和 server 只跟它打交道
  session.py         RunSession：execute / resume 两个入口，共用一条收尾链 → 裁判 → 判分
  loop.py            AgentLoop：Observe→Act→Observe 主循环
  authoring.py       自然语言建/改场景（ToolContext.browser=None，物理上碰不到浏览器）
  config.py          Settings：环境变量和路径的唯一来源
  domain.py          Outcome / RunStatus / Verdict 枚举、Scenario
  skills.py          Skill + SkillLoader（场景归 Skill 管）
  subagents.py       SubAgent + SubAgentLoader
  expect.py          验收路线 expect.yaml 的结构化读写
  knowledge.py       KnowledgeStore   report.py  ReportWriter   doctor.py  Doctor
  server.py          FastAPI，handler 只解析请求 / 调门面 / 整理响应
  context/prompts.py PromptLibrary + TaskPrompts：按情形拼任务说明
  events/            EventStream（SSE 缓冲）+ FrameStore（实时画面帧）
  traces/            Run / Step，落 run.json
tools/               工具层，不依赖 runtime
  base.py            Tool（name + schema + handler）、ToolContext、ToolRegistry
  args.py            工具参数卫生（泄漏修复）
  browser/           driver.py（Playwright）· tools.py（Tool 对象）· observation.py
  resource.py / scripts.py / authoring.py / finish.py   各导出一个 Tool
  recorder/ validator/
tests/               pytest，纯函数级，不碰 LLM 和浏览器
knowledge/ storage/ reports/   运行期产物
```

## 分层与依赖方向

```
tools  ←  runtime  ←  server / run.py
```

`tools` 一个 `runtime` 都不 import（类型只在 TYPE_CHECKING 下引用）。以前 driver 反向 import
`runtime.context` / `runtime.events` 形成循环，靠 import 顺序运气才能跑；现在 Observation 住在
驱动旁边，实时帧用 `on_frame` 回调注入。

## 几条刻意的设计

- **只有一个主 agent，不是"角色分工"。** `agents/agent.md` 拿到全部工具。"有没有路书""是建还是改"
  由代码在调用前判断完，只拼对应的任务块（`prompts/*.md`）；模板里没有"如果……否则……"，
  `tests/test_prompts.py` 机械地卡这一条。
- **judge 是唯一独立的第二个 agent，理由是上下文隔离。** 它看不到执行过程，只信自己读到的页面；
  白名单里没有 click / type_text，`ToolRegistry.schemas()` 按白名单裁剪，越权工具不存在于它的世界里。
- **一个工具 = 一个 `Tool` 对象。** schema 和 handler 在一起，注册表按名字查表分发，
  加工具 = 多放一个对象。动作型工具用 `acting()` 包一层，返回里自动拼最新观察——
  Observe→Act→Observe 是结构性保证。
- **边界是代码卡的，不是提示词说的。** 写文件只能进 `scenarios/<当前场景>/`；脚本只能跑
  `scripts/` 下的、不走 shell；authoring 阶段浏览器是 None，浏览器工具会明确拒绝。
- **playbook 走异步结构化 writer。** runner 只交单步五要素；后台单线程按序原子写入，收尾 flush。
  整文件覆盖不再暴露给 runner，资产失败只记告警，不改变业务结论。
- **所有工具在 handler 前校验。** 坏 JSON、缺 required、类型/enum/未知字段不执行 handler，错误作为
  tool_result 精确反馈；`stop_reason=length` 即使带半截 tool call 也整轮丢弃重试。
- **runner 不设固定轮数上限。** 真 UI 流程的等待和恢复次数不可预估；runner 用 30 分钟墙钟、页面停滞
  检测和人工停止收口。judge 只读取证，仍保留固定轮数上限。
- **场景自己的核心文件直接拼进任务，不让模型猜着调 read_resource。** intent/playbook/expect/
  判定协议都由代码读盘代入；read_resource 留给原子、参考资料、别的场景样板这类"要不要看"
  由模型判断的场合。
- **裁判继承 storage_state。** 全新会话 + 全新对话，但继承被测应用已落盘的状态。
- **状态是枚举。** `Outcome` / `RunStatus` / `Verdict` 用 StrEnum，序列化仍是普通字符串，
  历史 run.json 和前端契约不变；`Run.from_dict` 容忍早期缺字段的文件。

## 已知的模型层坑（都有单测锁住）

- `tools/args.py`：bedrock/claude 经 LiteLLM 会把 `</notes><parameter name="facts">{...}` 泄进字符串参数。
- `tools/validator.flatten_facts`：模型把事实多套一层 `context_yaml:`。
- `runtime/loop.py`：`needs_input` 不带 `questions` 会被拒回；`finish` 后补 `tool_result`，
  否则冻结的对话是残的，续跑时 Bedrock 400。

## 工程

```bash
.venv/bin/pip install -e ".[dev]"      # 可安装包 + ruff/mypy/pytest
.venv/bin/ruff check .                 # 0 错
.venv/bin/mypy runtime tools run.py    # 0 错
.venv/bin/python -m pytest             # 267 passed
```

## 跑

```bash
./qa check                  # 自检
./qa list                   # 场景 + 最近结论
./qa run <场景>             # 0=PASS 1=FAIL 2=BLOCK
./qa serve                  # 看板 http://localhost:8900
```

## 常驻服务启动约束

- 从 agent 的命令执行器启动 `qa serve` 时，不要用普通 `nohup ... &`：命令结束后，执行器可能清理它的后台子进程，造成“刚探活成功，页面随后拒绝连接”。
- 用持久 PTY/终端会话托管服务；启动后必须在**下一次独立调用**里再检查 `lsof -iTCP:8900 -sTCP:LISTEN` 和健康接口，不能只相信启动命令内的探活。
- `storage/` 是历史跑批数据，roadbook 路径是场景来源，两者彼此独立。看板加载哪套场景由 `ROADBOOK_REPO` 决定。
- 启动探活不能只看 HTTP 200：还要读取 `/api/skills`，确认包含你自己的场景。有跑批处于 running 时不要为改配置重启服务。

## 数据持久化（硬规矩）

- **唯一数据源是本目录**：`storage/`（跑批产物+录屏）、`reports/`、`qa-agent.db`（索引）。看板只从这里起（`./serve.sh`）。**绝不能从别的 checkout 起**——db 和 storage 跟着项目目录走，换个目录起服务，历史就"消失"一次（有过排一下午"记录不见了"、最后发现是旧 checkout 分家导致的教训）。
- 外来历史记录带 `external: true`（run.json 字段）：只上历史列表、不上卡片——它们没有封面和录屏。卡片按"全名 → 叶子名"认领历史（`server.py` 的 `_latest_for`），旧平铺命名能挂到对应的分组卡上。
- **每天备份**：`./backup_data.sh`（APFS 克隆 + sqlite `.backup`，保留 14 份），建议挂一个每日定时任务自动跑；跑批大日子可以手动多跑。`backups/` 不进 git。
