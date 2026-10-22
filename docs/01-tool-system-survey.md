# Tool System 设计调研

调研对象：Codex CLI（openai/codex，公开仓库）、OpenCode（sst/opencode，公开仓库）、
Goose（block/goose，公开仓库）、Aider（Aider-AI/aider，公开仓库，作反例）、
Claude Agent SDK（anthropics/claude-agent-sdk-python，官方公开 SDK）。

**说明**：Claude Code CLI 本身闭源，2026-03 那次源码泄露不在调研范围内，本文不引用、
不分析该泄露内容。Claude 相关的结论全部来自 Anthropic 官方公开发布的 Agent SDK 源码
（`@tool` 装饰器、`create_sdk_mcp_server`）和 MCP 协议规范，这些是公开 API，跟泄露代码
无关。

结论先说：**没有一家用"每个工具一个类"的继承体系。** 全部收敛到同一个形状——
`schema（数据）+ handler（函数）`，注册进一张按名字查的表。这跟当前项目 `tools/base/`
的设计是同一条路，不是巧合，下面逐节展开证据。

---

## 1. Tool 的抽象方式

| 项目 | 语言 | Tool 是什么 | 怎么构造 |
|---|---|---|---|
| Codex | Rust | struct（spec）+ 独立 handler 函数 | 手写 `ToolSpec` + 在 `handlers/*.rs` 写一个函数 |
| OpenCode | TypeScript | `interface Def { id, description, parameters, execute }` | 工厂函数 `Tool.define(id, init)` |
| Claude Agent SDK | Python | `@dataclass SdkMcpTool { name, description, input_schema, handler, annotations }` | 装饰器 `@tool(name, desc, schema)` |
| Goose | Rust | 大部分不是"内部对象"，是外部 MCP server 上报的工具 | 连接 MCP extension，协议自报 |
| Aider（反例） | Python | **没有 Tool 抽象** | 见下 |

**Claude Agent SDK 的真实代码**（官方公开仓库，非泄露）：

```python
@dataclass
class SdkMcpTool(Generic[T]):
    """Definition for an SDK MCP tool."""
    name: str
    description: str
    input_schema: type[T] | dict[str, Any]
    handler: Callable[[T], Awaitable[dict[str, Any]]]
    annotations: _McpToolAnnotations | None = None

def tool(name, description, input_schema, annotations=None):
    """Decorator for defining MCP tools with type safety."""
    ...
```

跟当前项目 `tools/base/registry.py` 的 `Tool` 逐字段对比：

| Claude SDK `SdkMcpTool` | 本项目 `Tool` |
|---|---|
| `name` | `name` |
| `description` | `description` |
| `input_schema` | `parameters` + `required` |
| `handler` | `handler` |
| `annotations`（可选） | `terminal` / `mutates`（本项目只需要的两个位） |

**OpenCode 的 `Tool.Def`**（`packages/opencode/src/tool/tool.ts`）：同样是接口 + 工厂函数
`Tool.define()`，不是类。**Codex** 的 `handlers/` 目录下，绝大多数工具是一个函数一个文件
（`sleep.rs`、`view_image.rs`、`apply_patch.rs`……），只有当一个"工具"实际是一族相关操作时
才升级成"文件 + 同名文件夹"（`unified_exec.rs` + `unified_exec/{exec_command.rs,write_stdin.rs}`；
`multi_agents.rs` + `multi_agents_v2/{spawn.rs,wait.rs,send_message.rs,...}`）——这跟上次
调研 Codex 得出的"文件夹只在工具变成一族操作时才用"完全一致，这次在 OpenCode 上也验证了同一
规律（`shell.ts` + `shell/` 文件夹）。

**Aider 是唯一的反例，值得说一句为什么**：`aider search/code` 里搜 `tool_call` 命中 0 次——
它压根不用 function-calling 循环。它的 `coders/` 目录是一堆"输出格式解析器"
（`editblock_coder.py`、`architect_coder.py`……），靠提示词让模型输出固定格式的 diff 文本，
再用正则/字符串匹配解析、应用。这证明"tool calling"不是唯一解，但对浏览器场景不适用——
页面上的动作（点击、输入）天然是离散的、有副作用的单次调用，没有"一大段 diff 文本"可解析，
Aider 这条路线对 Browser Agent 没有参考价值，调研到此为止。

**为什么都收敛到"数据 + 函数"而不是继承**：模型调用工具靠的是一个**字符串名字**，不是类型系统。
调用方(harness)要做的事情永远是"拿到一个名字 → 查表 → 找到对应的东西 → 执行"，这是一次
哈希查找，不是"不知道具体类型、指望虚方法多态分发"的场景——继承要解决的问题在这里根本不存在。

### 数据流

```
Tool（name + description + parameters/schema + handler，纯数据 + 一个函数）
   │  启动时，逐个工具收集进一个列表
   ▼
Registry（name → Tool 的一张表；按 agent/权限过滤出"这次能看到哪些工具"）
   │  模型吐出 {tool_name, arguments(JSON 字符串)}；按名字查表
   ▼
Executor（解析参数 → 按 schema 校验 → [权限检查] → 调 handler → 捕获异常 → 截断/格式化结果）
   ▼
Tool Result（字符串或 content blocks，包成 tool-role 消息，塞回对话，喂给下一次模型调用）
```

对应本项目：`Tool` = `tools/base/registry.py` 的 `Tool`；`Registry` = `ToolRegistry`；
`Executor` = `AgentLoop._call_tool` + `_drive` 里那段批处理逻辑；`Tool Result` = `_tool_message(...)`。

---

## 2. Registry 设计

**工具发现**：四个真正做 tool-calling 的系统里，**核心工具全部是显式列表，没有一个是"扫目录自动注册"**：

- Codex：`core/src/tools/registry.rs` 里手工把每个 handler 塞进一张表
- OpenCode：`tool/registry.ts` 开头 12 行 `import { XxxTool } from "./xxx"`，最后收进 `builtin: Tool.Def[]`
- Claude SDK：`create_sdk_mcp_server(name, tools=[add, multiply])`——调用方显式传列表

"动态发现"确实存在，但发生在**另一层**：MCP 协议层面，一个外部 MCP server 进程通过
`tools/list` 协议方法自报自己有哪些工具——这是"发现外部工具提供方"，不是"扫描本地文件自动注册
本地函数"。Goose 把几乎所有工具都放在这一层（`goose-mcp` crate 下每个子目录是一个 MCP
extension，比如 `computercontroller/` 打包了好几个操作 Office 文件的工具），是四个项目里
唯一重度依赖这条路径的。

本项目没有外部工具提供方（不接 MCP server），所以"扫描 + 动态加载"这层用不上，
`tools/__init__.py` 里手工列出 13 个工具，正是这几个系统对"核心工具"的统一做法，
不是偷懒或者过渡状态。

**查表分发**：全部是 `dict[name] -> Tool`。Claude SDK 原文：
`tools_by_name = {tool_def.name: tool_def for tool_def in tools}`——跟本项目
`ToolRegistry._tools: dict[str, Tool]` 是同一行代码换了个语言。

**`registry.get(name)` 是否已经足够**：作为"查找"这一步，足够，四个系统都是这么做的。
但更成熟的两个系统（Codex、OpenCode）在 `get(name)` **周围**多挂了两类东西，值得区分开看：

1. **按上下文过滤的视图**——不是"注册表本身要多什么方法"，而是"谁能看到哪些工具"要按
   调用方(agent/model/权限)过滤。Codex 有 `model_visible_specs`，OpenCode 有
   `tools(model)` 按 agent+permission 过滤。**本项目已经有**：`ToolRegistry.schemas(allowed)`
   按子代理白名单裁剪，裁判拿不到 click 就是这层做的。不是缺口。
2. **挂在查找旁边的横切关注点**——权限确认回调（OpenCode 的 `ctx.ask(...)`）、
   参数变更追踪用于撤销（Codex 的 `ToolArgumentDiffConsumer`）。**本项目没有，且现在也不需要**
   （见第 6 节）。

结论：`get(name)` 对"查找"这个问题是够的；权限、撤销这些不是"注册表该长的能力"，
是另开的协作对象，该来的时候再加，不该现在预埋进 Registry 里。

---

## 3. Executor 设计

把四个系统的调用链摊开对比：

```
模型输出 {tool: name, args: 原始 JSON 字符串}
        │
        ▼
① 参数解析/修复    JSON 字符串 → dict；修复已知的畸形输出
                   Codex: FunctionCallError
                   OpenCode: effect/Schema.decode，失败抛 InvalidArgumentsError，
                             message 是"请重写输入以满足 schema"这类喂给模型的提示
                   Claude SDK: jsonschema.validate 失败 → _tool_error_result
                   本项目: tools/base/args.py 的 parse_tool_args + repair_leaked_args
        │
        ▼
② Schema 校验      拿解析出的 dict 对着这个工具自己声明的 parameters 校验
                   —— 四个系统全部在 handler 执行之前、由 executor/harness 层做，
                      从不依赖 handler 自己校验参数
                   ⚠️ 本项目目前没有这一步（见下方"缺口"）
        │
        ▼
③ 权限检查         视系统而定：OpenCode 在动作型工具跑之前 ctx.ask() 走人工确认；
                   Codex 有独立的 approvals.rs 按沙箱策略拦 shell/exec
                   本项目：没有，见第 6 节评估要不要加
        │
        ▼
④ 工具执行         registry 按名字分发 → tool.handler(ctx, args)；包一层 try/except，
                   handler 崩溃变成一条 tool-result 错误消息，不崩掉整个循环
                   本项目：AgentLoop._call_tool，逐字对应
        │
        ▼
⑤ 结果格式化回填    截断/格式化 → 包成 tool-role 消息，用原始 tool_use id 对齐 → 追加进对话
                   本项目：output[:TOOL_RESULT_MAX_CHARS] + _tool_message(call.id, ...)
```

**具体缺口，不是泛泛而谈**：本项目在 ①→④ 之间跳过了 ②。现在一个必填参数缺失
（比如模型调 `click` 没给 `ref`），是在 handler 内部 `args["ref"]` 直接 `KeyError`，
被 `_call_tool` 的通用 `except Exception` 兜住，回给模型的是
`"工具执行失败: KeyError: 'ref'\n重新 observe 后换个方式。"`——能用，但比"缺少必填参数 ref"
模糊，模型要多猜一步。四个被调研的系统都在这一步用 JSON Schema 校验一次，成本很低
（`jsonschema.validate` 或等价物，一次函数调用），报错也更精确。这是本报告认为值得补的
第一顺位基础设施缺口（不是"工具能力"，是 Executor 该做而没做的一步，跟第 6 节的建议对应）。

---

## 4. Tool Metadata

汇总四个系统 + MCP 协议规范里实际出现过的字段：

| 字段 | 出现在 | 必须/可选 |
|---|---|---|
| `name` | 全部 | **必须** |
| `description` | 全部 | **必须**——这是模型判断"何时用"的唯一信号，见上次讨论 click vs click_xy |
| `parameters`/`input_schema` | 全部 | **必须** |
| `handler`/`execute` | 全部 | **必须** |
| `readOnlyHint` | MCP 协议标准字段，Claude SDK `ToolAnnotations` 原生支持 | 可选 |
| `destructiveHint` | 同上 | 可选 |
| `idempotentHint` | 同上 | 可选 |
| `openWorldHint`（会不会碰到工具作用域之外的东西） | 同上 | 可选 |
| `maxResultSizeChars` | Claude Code 自己加的扩展，不是 MCP 标准 | 可选，特定于 Claude Code |
| 终止型标记（比如本项目的 `terminal`） | **只有本项目有**，四个调研对象都没有专门的"终止工具"概念，
它们靠"模型不再吐 tool_calls"自然结束 | 本项目特有，因为需要给裁判一份结构化产物（facts/notes/questions），
这是我们领域的真实需要，不是画蛇添足 |

**对 Browser Agent 有价值的**：`readOnlyHint`/`mutates` 这一对——本项目的 `mutates` 字段
在语义上就是 MCP `readOnlyHint` 的反面，而且已经真正派上用场（上次修的 ref 重编号 bug，
靠的就是这个字段）。命名上目前是本项目自己的叫法，跟 MCP 标准不一样，本身不影响功能，
但如果以后这个 agent 或者 roadbook 要跟真正的 MCP 生态打交道（roadbook 已经有
`references/runtimes/playwright-mcp.md` 这个运行时），说同一套词汇会省事——这是命名对齐
建议，不是功能缺口。

`destructiveHint`/`openWorldHint` 这两个：本项目的浏览器工具都是通用原语（点击、输入），
不是业务语义上的"删除""转账"，暂时没有能落地这两个标记的具体场景，不建议现在加。

---

## 5. Browser Agent 场景分析

当前 9 个浏览器工具按读写分类：

| 工具 | 分类 | 备注 |
|---|---|---|
| `observe` | 只读 | |
| `find` | 只读 | |
| `eval_js` | 只读 | 文档写明"只读"，不该拿它写值——这是约定不是代码强制的边界，值得记一笔 |
| `navigate` | 动作 | `mutates=True` |
| `click` | 动作 | `mutates=True` |
| `type_text` | 动作 | `mutates=True` |
| `wait` | 动作 | `mutates=True`（等待期间页面可能自己变，比如弹层延迟出现） |
| `scroll` | 动作 | `mutates=True` |
| `click_xy` | 动作 | `mutates=True` |

加上另外 4 个非浏览器工具，有一个值得单独指出的边界情况：

**`run_skill_script` 的分类是模糊的**——它现在没有标 `mutates`，这在"会不会让页面引用重编号"
这个意义上是对的（它不碰 DOM），但它**能真实修改被测系统的数据**（比如清理脚本、造夹具的脚本）。
也就是说"会不会让 DOM 引用失效"和"会不会对被测系统产生副作用"是两根不同的轴，现在这两根轴
在 6 个浏览器动作工具上恰好重合，所以只用一个 `mutates` 字段够用；但 `run_skill_script`
是这两根轴分叉的第一个例子——它在第一根轴上是"否"，第二根轴上是"是"。目前这个工具本身已经
在代码层面做了边界收紧（只能跑 `skill.root/scripts/` 下的文件、走 subprocess 列表不走 shell、
有超时），实际风险可控，**不需要现在为了这一个特例就拆分字段**，但如果以后再出现类似"不碰
DOM 但有副作用"的工具，就是该拆的信号。

**并发/权限标记是否需要**：并发这块，上次的 ref 重编号修复（批内动作型工具跑过之后，
后续用 ref 的调用一律跳过重来）已经是这个领域实际需要的安全网，不需要再加一个独立的
`concurrency_safe` 字段——`mutates` 已经承担了这个作用。权限标记：单一可信操作者、
浏览器本身沙箱隔离、脚本执行有路径和超时边界，没有真实场景需要人工审批介入，不建议加。

---

## 6. 与当前项目对比

```python
Tool            # name/description/parameters/handler/terminal/mutates —— 纯数据 + 一个函数
ToolContext     # browser/skill/scenario —— handler 能摸到的东西
ToolRegistry    # dict[name -> Tool]，按白名单裁剪成 schema 列表，按名字 dispatch
handler(ctx, args)
```

**优点**（都有本次调研的对照支撑，不是自我感觉）：
- `Tool` = 数据 + 函数，跟 Codex/OpenCode/Claude SDK 三家独立收敛到的形状完全一致
- `ToolRegistry.schemas(allowed)` 按 agent 过滤，对应 Codex 的 `model_visible_specs`、
  OpenCode 的 `tools(model)`——裁判拿不到 click 是代码层面的硬约束，这个设计已经在业界主流方案里
- 一个工具一个文件的目录布局，跟 Codex `handlers/`、OpenCode `tool/` 几乎逐文件对应
- `mutates` 字段是真正在用的元数据（ref 重编号 bug 靠它修的），不是摆设

**缺点（具体的）**：
- Executor 链路里缺"参数按 schema 校验"这一步（第 3 节已展开，四家都有，成本低）
- `ToolContext` 现在把 browser/skill/scenario 三种不同性质的东西塞进一个 dataclass——13 个
  工具、1 个技能的规模下没问题，如果工具种类和数量继续长（比如加一个完全不碰浏览器的新领域），
  这个 context 可能要拆
- `mutates`/`terminal` 是本项目自造的词汇，没有对齐 MCP 的 `readOnlyHint` 等标准命名
  （功能上没问题，纯粹是以后要跟 MCP 生态对话时的翻译成本）

**未来扩展风险**：
- 真出现需要人工审批/不可逆动作（比如某个 atom 要真的触发付费）的那天，现在这套
  "白名单即权限"的模型撑不住，需要引入 OpenCode 那种 `ctx.ask()` 式的交互确认——但这是
  "真出现那天再加"的东西，现在加是无的放矢
- 工具数量如果突破二三十个、且开始横跨多个不同运行时（不只是浏览器），单一的扁平
  `REGISTRY` 可能想按运行时拆子表——目前只有一个运行时，不需要

**四个候选要不要引入**：

| 候选 | 结论 | 理由 |
|---|---|---|
| Tool 子类体系 | **不引入** | 四个被调研系统全部没有子类体系，上次已经论证过，这次是交叉验证 |
| Executor（单独抽出一层） | **半引入** | 一旦加上第 3 节说的 schema 校验，"解析→校验→分发→格式化"就是 4 步而不是 2 步，值得抽成一个命名清楚的函数，但只是一个函数，不是一个子系统 |
| Permission Layer | **不引入** | 没有真实场景需要（单操作者、沙箱浏览器、脚本已有路径/超时边界），现在加是预判需求 |
| Tool Capability Layer（标准化 annotations） | **小范围引入** | 把 `mutates` 在文档里标注"即 MCP 的 `!readOnlyHint`"，不用现在改字段名，等真要跟 MCP 生态打交道时再考虑改名，成本极低、收益是未来的 |

---

## 推荐目录结构

现状已经是推荐结构，不需要大改：

```
tools/
  __init__.py              REGISTRY：汇总处
  base/                     地基：Tool / ToolContext / ToolRegistry / 参数解析
    registry.py
    args.py
    validate.py             ← 建议新增：按 tool.parameters 做 JSON Schema 校验（对应第 3 节缺口）
  read_resource.py  run_skill_script.py  write_scenario_file.py  finish.py
  browser/
    __init__.py             BROWSER_TOOLS 汇总
    base/                   地基：Browser 驱动 / 观察结果 / 注入脚本
    observe.py  navigate.py  click.py  type_text.py
    find.py  eval_js.py  wait.py  scroll.py  click_xy.py
    upload_file.py           ← 建议新增，见下方"下一阶段"
```

## 推荐数据结构

`Tool` 不需要结构性改动。若要落地"参数校验"，`ToolRegistry.dispatch` 前置一步：

```python
def dispatch(self, ctx, name, args):
    tool = self._tools.get(name)
    if tool is None:
        return f"未知工具: {name}"
    if tool.terminal:
        return f"{name} 是终止型工具，不应被直接执行"
    error = validate_args(tool.parameters, tool.required, args)   # 新增
    if error:
        return error   # 回给模型的错误信息比裸 KeyError 更精确
    return tool.handler(ctx, args)
```

不需要引入新的类、新的分层，`base/validate.py` 一个十几行的函数就够。

## 推荐演进路线

1. **现在做**：`tools/base/validate.py`——参数按 schema 校验，回填精确错误（第 3 节缺口，成本最低、收益立即）
2. **下一阶段的 Tool 能力**：见下方独立结论
3. **观察但不动**：Permission Layer、annotations 改名、ToolContext 拆分——都是"等信号出现再做"，现在做属于预判

---

## 下一阶段最值得增加的 Tool 能力：`upload_file`

**不是猜的，是这次会话里刚发生过的真实证据**：`attachment-onboarding` 这个场景要往对话里
挂一个附件，现在的做法记在 `atoms/attachment-upload.md` 里，靠 `eval_js` 手写 JS 代码
"构造 File 对象 + dispatch change 事件"去模拟文件选择——这是一个**用只读兜底工具伪造动作型
能力**的 hack，本身就违反了这份报告第 5 节"`eval_js` 只读"的约定，也违反 roadbook 自己定的
"资产层不该编码具体技术手段"（这段 JS 逻辑现在焊死在原子文件里，属于运行时细节，不该在
资产层出现）。

对照组：Playwright MCP（roadbook 自己的 `references/runtimes/playwright-mcp.md` 里列出的
能力表）原生就有 `browser_file_upload`；Goose 的 `computercontroller` extension
把文件相关操作（docx/pdf/xlsx 处理）单独打包成工具而不是让模型拼 JS 糊弄。行业里凡是需要
处理文件的浏览器自动化工具集，都会把"上传文件"做成一等公民的工具，而不是指望模型每次现场
拼 DOM hack。

**推荐**：新增 `upload_file` 工具，`mutates=True`，走 Playwright 原生的
`page.locator(ref).set_input_files(path)`，参数是 `ref` + `path`（相对当前技能目录，
复用 `read_resource` 已经有的路径边界检查逻辑）。做完之后：
- `atoms/attachment-upload.md` 从"焊死一段 JS"降级成"打开附件、调 upload_file"这种
  运行时无关的抽象描述，恢复到 roadbook 自己的分层原则
- 以后任何需要上传文件的场景（简历、图片、任意附件类需求）都不用再让模型现写 JS，
  减少了一类"模型自己糊弄出一个能跑但脆弱的方案"的风险

**为什么不是"加 console/network 只读工具"（次优选项）**：Playwright MCP 能力表里也有
`browser_console_messages`/`browser_network_requests`，对无人值守场景排查失败原因确实有用，
但这次调研和本次会话里**没有一次真实运行因为缺这两个工具而卡住或误判**——它是"锦上添花"，
`upload_file` 是"现在就有一个真实场景在用一个明知有问题的 hack"。按"有实证需求的优先"原则，
`upload_file` 排第一，console/network 工具作为第二候选记录在案，等真的遇到一次
"看不到网络请求/JS 报错、排查卡住"的具体案例再动手，不预先builds。
