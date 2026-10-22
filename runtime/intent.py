"""场景意图（intent.md）的结构化读写。这份是给产品经理写的。

骨架不是我编的，是把三处已有约束收拢成一份可执行的规范：

1. `SKILL.md` 写着 intent.md 该有什么——「frontmatter 一句话 description +『什么时候跑』
   + runner 侧的人定边界：目标、环境、固定参数、红线、造数、清理——不写步骤」。
2. `scripts/scenarios.py` 在机器层面**已经**硬依赖两样：frontmatter 的 `name`/`description`
   （第 1 层索引），和精确标题 `## 什么时候跑`（第 2 层索引）。缺了不报错，是**静默**
   从索引里消失——这比报错更坏。
3. 现存 12 份 intent.md 里实际用过的小节，取交集与最规整的那几份（search-first-result、
   prod-login-page）为准。

规范写在这里、`SECTIONS` 就是唯一事实源：编辑器照它生成表单，校验照它拦，
两边不会漂。跟路书一样，往返（读出来再写回去）对合规文件必须逐字不变。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from .domain import Scenario

INTENT_FILE = "intent.md"
FM_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
HEADING_RE = re.compile(r"^## (.+?)\s*$", re.M)
TITLE_RE = re.compile(r"^# (.+?)\s*$", re.M)

DESC_MIN, DESC_MAX = 25, 120


@dataclass(frozen=True)
class Section:
    key: str
    heading: str            # 必须逐字一致：scenarios.py 按精确标题抓「什么时候跑」
    required: bool
    hint: str               # 编辑器里显示给产品经理看的「这里写什么」
    example: str


SECTIONS: tuple[Section, ...] = (
    Section("when", "什么时候跑", True,
            "用户说哪些话就该跑这个场景。索引脚本按这一段给人挑场景——写得像人话，别写术语。",
            '用户说"搜索还能用吗""点搜索结果会不会跳错""搜索主流程回归一下"。'),
    Section("goal", "目标", True,
            "这个场景要证明哪个**业务结果**成立。不是「点到某个按钮」，是「登录成功」这种。",
            "搜索结果的第一条自然结果，点进去之后落地页的内容确实跟搜索词相关，\n"
            "不是跳到了无关页面、错误页或者广告落地页。"),
    Section("env", "环境", True,
            "跑在哪个环境、入口在哪。域名和账号一律从 config/env.json 取，别写死在这里。\n"
            "要跑 prod 必须在 frontmatter 的 envs 里显式列出来。",
            "外网直连，不需要起本地服务。入口 `https://duckduckgo.com/`。"),
    Section("params", "固定参数", True,
            "写死的输入：搜索词、文案、选项、数量。**凡是没写死的，agent 就会自由发挥**，\n"
            "两次跑的结果就没法比。附件、长文案这类连内容一起写死。\n"
            "域名、账号这类**跟环境走**的值不写死，写成 `environments.<env>.xxx`——\n"
            "xxx 是 `config/env.json` 里的键（如 researcher_web、fixture_email），看板上悬停能看到实际值。",
            "- 搜索词固定用 `playwright end-to-end testing`。\n"
            "- **第一条自然结果**指跳过带「广告」标记之后的第一条。\n"
            "- 登录账号用 `environments.<env>.fixture_email`。"),
    Section("setup", "前置造数", False,
            "跑之前要先有什么：造数脚本、已存在的项目、登录态。不需要就删掉这一节。",
            "先跑 `scripts/seed_draft.sh`，记住它打印的 draft_id / project_id。"),
    Section("facts", "留给裁判的事实", False,
            "只放「跑的时候才知道」的东西：新建对象的 id、实际点开的那一条。\n"
            "这里每写一行 `<键名>:`，验收路线里就用 `context.<键名>` 引用它，两边名字必须一致。\n"
            "**写死的值不要列在这里**——地址、固定文案直接写进验收路线。\n"
            "**别把「应该是多少」写进来**——那样就成了裁判按 runner 的说法判 runner。",
            "```yaml\nquery: playwright end-to-end testing\nlanding_url: <落地页最终 URL>\n```"),
    Section("cleanup", "清理", True,
            "造的数据怎么清。清不掉就写明把哪个 id 记进报告。全程只读就写「不需要」。",
            "不需要，全程只读外部网站，不产生任何数据。"),
    Section("redlines", "红线", True,
            "**绝对不许做的事**：花钱、发布、删数据、触达真人、解验证码。\n"
            "会花钱的场景必须写死一个上限。",
            "- **只读。** 不要做任何提交、注册、下单。\n"
            "- 遇到验证码一律 `blocked`，不要尝试去解。"),
    Section("todo", "待补", False,
            "这个场景还没覆盖到、下次值得补的部分，以及踩过的坑留档。\n"
            "写在这里是为了不丢；不影响这次能不能跑。没有就留空。",
            "- 多附件、非 .md 类型（.pdf / .docx）都没试过。\n"
            "- 2026-09-08 首跑判 FAIL 是验收标准定得不合理，已改 expect.yaml。"),
)
BY_KEY = {s.key: s for s in SECTIONS}
BY_HEADING = {s.heading: s for s in SECTIONS}


class _FrontDumper(yaml.SafeDumper):
    """frontmatter 的写法要跟现有文件一致：映射块式一行一个键，列表行内写成 `envs: [prod]`。

    单靠 default_flow_style 做不到这种混合——False 会把列表展开成多行，
    None 又会把整个顶层映射压成 `{name: ..., description: ...}`，两种都跟现有文件不一致。
    """


_FrontDumper.add_representer(
    list, lambda d, data: d.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True))


class IntentError(ValueError):
    """存不进去的 intent：缺 frontmatter，或缺必填小节。"""


def _split_sections(body: str) -> tuple[str, list[tuple[str, str]]]:
    """正文 → （标题之前的部分, [(小节标题, 小节正文)]）。"""
    marks = [(m.start(), m.end(), m.group(1)) for m in HEADING_RE.finditer(body)]
    if not marks:
        return body, []
    head = body[:marks[0][0]]
    out = []
    for i, (_, end, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        out.append((name, body[end:stop].strip("\n")))
    return head, out


def load_intent(scenario: Scenario) -> dict[str, Any]:
    """读出结构。`conforms` 说明这份 intent 能不能安全地结构化编辑——

    判据跟路书一样硬：解析完再渲染必须和原文逐字一致。现存 12 份里大部分是
    在规范落地之前手写的，小节名各不相同（`前置造数`/`前置`/`目标状态`…），
    这些只能按纯文本改，并把「差哪些必填小节」明着告诉人。
    """
    path = scenario.dir / INTENT_FILE
    if not path.exists():
        return {"exists": False, "conforms": False, "raw": "", "meta": {}, "title": "",
                "sections": {}, "extra": [], "missing": [s.key for s in SECTIONS if s.required]}
    return parse_intent(path.read_text(encoding="utf-8"))


def parse_intent(text: str) -> dict[str, Any]:
    """纯文本 → 结构。跑批目录里存档的 intent 也走这里，所以解析不依赖文件系统。"""
    m = FM_RE.match(text)
    meta: dict[str, Any] = {}
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
    body = text[m.end():] if m else text

    head, found = _split_sections(body)
    title_m = TITLE_RE.search(head)
    sections = {BY_HEADING[name].key: content for name, content in found if name in BY_HEADING}
    extra = [{"heading": name, "body": content} for name, content in found if name not in BY_HEADING]

    data = {
        "exists": True, "raw": text, "meta": meta,
        "title": title_m.group(1) if title_m else "",
        "sections": sections, "extra": extra,
        "missing": [s.key for s in SECTIONS if s.required and not sections.get(s.key, "").strip()],
    }
    data["conforms"] = (not data["missing"]) and not extra and render_intent(data) == text
    return data


def render_intent(data: dict[str, Any]) -> str:
    meta = dict(data.get("meta") or {})
    # width 放大：description 有近百字，默认 80 列会在空格处折行。中文没空格通常折不了，
    # 一旦句子里混了「dev / prod」这种带空格的片段就会被折断，往返立刻不一致。
    front = yaml.dump(meta, Dumper=_FrontDumper, allow_unicode=True, sort_keys=False,
                      default_flow_style=False, width=10**6).strip()
    title = str(data.get("title") or "").strip()
    sections = data.get("sections") or {}

    out = [f"---\n{front}\n---", ""]
    if title:
        out += [f"# {title}", ""]
    for s in SECTIONS:
        content = str(sections.get(s.key) or "").strip("\n")
        if content:
            out += [f"## {s.heading}", "", content, ""]
    for block in data.get("extra") or []:
        content = str(block.get("body") or "").strip("\n")
        out += [f"## {block['heading']}", "", content, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def save_intent(scenario: Scenario, data: dict[str, Any]) -> dict[str, Any]:
    meta = dict(data.get("meta") or {})
    if not str(meta.get("name") or "").strip():
        raise IntentError("frontmatter 缺 name——scenarios.py 的第 1 层索引靠它")
    desc = str(meta.get("description") or "").strip()
    if not DESC_MIN <= len(desc) <= DESC_MAX:
        raise IntentError(f"description 要一句完整的话（{DESC_MIN}~{DESC_MAX} 字，现在 {len(desc)} 字）"
                          "：被测什么、做什么、验什么——挑场景时人只看得到这一句")
    missing = [s.heading for s in SECTIONS
               if s.required and not str((data.get("sections") or {}).get(s.key) or "").strip()]
    if missing:
        raise IntentError("缺必填小节：" + "、".join(f"「{h}」" for h in missing))

    (scenario.dir / INTENT_FILE).write_text(render_intent(data), encoding="utf-8")
    return {"ok": True, "path": f"scenarios/{scenario.name}/{INTENT_FILE}"}


def spec() -> list[dict[str, Any]]:
    """规范本身，给前端画表单用——字段、是否必填、写什么、示例。"""
    return [{"key": s.key, "heading": s.heading, "required": s.required,
             "hint": s.hint, "example": s.example} for s in SECTIONS]


def contract() -> str:
    """拼进「建/改场景」提示词的那份骨架契约。

    agent 也会写 intent（自然语言建场景走的就是它），所以它必须照同一套规范写。
    以前提示词让它「读一个现有场景照着写」——抄到哪份算哪份，这正是 12 份 intent
    漂成 12 个样子的根源。这里从 SECTIONS 生成，规范改了提示词自动跟着改，不会漂。
    """
    lines = [
        "intent.md 必须照这个骨架写，小节标题逐字一致、顺序照排：",
        "",
        "```markdown",
        "---",
        "name: <场景目录名>",
        f"description: <一句完整的话，{DESC_MIN}~{DESC_MAX} 字：被测什么、做什么、验什么>",
        "envs: [dev]        # 可选；要跑 prod 必须显式写出来",
        "---",
        "",
        "# 场景：<一句话标题>",
        "",
    ]
    width = max(len(s.heading) for s in SECTIONS)
    lines += [f"## {s.heading}" + ("" if s.required
              else " " * (width - len(s.heading) + 4) + "# 可选，不需要就整节删掉")
              for s in SECTIONS]
    lines += ["```", "", "每节写什么："]
    lines += [f"- **{s.heading}**（{'必填' if s.required else '可选'}）："
              f"{s.hint.replace(chr(10), ' ')}" for s in SECTIONS]
    lines += [
        "",
        "三条硬规矩：**不写步骤**（那是 playbook.md，探索跑出来的）、"
        "**不写期望值**（那是 expect.yaml）、**不写工具名**（换个运行时就废了）。",
        "",
        "**「跑的时候才知道」的值怎么接**（最容易写错、也最该由你拧好的一根线）：",
        "- 写死的地址、固定文案、固定选项：验收路线里**直接写那个值**，不要为它造变量。",
        "  例：路线里写「打开 https://www.gov.cn/zhengce/」，而不是先声明 list_url 再引用 context.list_url。",
        "- 只有**跑的时候才定下来**的东西才开变量：新建对象的 id、从列表里挑中的那一条的标题。",
        "  做法是**成对**的，缺一半就会让裁判取不到值、整场判 BLOCK：",
        "  1. 在 intent.md 的「留给裁判的事实」里写一行 `<键名>: <值是什么>`；",
        "  2. 在 expect.yaml 里用 `context.<同一个键名>` 引用它。",
        "- 键名用能自解释的英文小写下划线（`issue_number`、`landing_url`），两边逐字一致。",
        "- 判断口径：**这个值在你写文件的时候就已经知道了，就不要开变量**。",
        "要例子就 read_resource 读 `references/intent-spec.md`（规范全文）。",
    ]
    return "\n".join(lines)
