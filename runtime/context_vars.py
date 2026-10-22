"""验收路线里那些 `context.xxx` 到底是什么。

`expect.yaml` 里满屏 `context.app_base`、`context.account_email`。它们不是配置项，是
**这次跑批跑出来的事实**——runner 在 finish 时交出，落进 `runs/<本次>/context.yaml`，
裁判再照着取证。链路本身没问题，问题是人在看板上只看得到那个名字：不知道它是谁定的、
上次到底是什么值、自己能不能改。2026-09-23 用户的原话是「对用户来说完全盲点」。

这里把三件事凑齐给前端：名字、**上次的真实取值**、谁定的。
上次取值取自 `knowledge/facts/<场景>.yaml`（每次跑完由 Knowledge.absorb 覆盖写入），
不用去翻 runs/ 目录。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .domain import Scenario
from .expect import required_context_keys
from .intent import load_intent

RUN_ID_RE = re.compile(r"跑批\s+(\S+)\s+留下")
MAX_VALUE_CHARS = 200


def _facts(facts_dir: Path, scenario: str) -> tuple[dict[str, Any], str]:
    """上一次跑批留下的事实 + 它是哪一次跑的。文件不存在或坏了都只当「没有」。"""
    path = facts_dir / f"{scenario}.yaml"
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    match = RUN_ID_RE.search(text.split("\n", 1)[0])
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}, ""
    return (data if isinstance(data, dict) else {}), (match.group(1) if match else "")


def _declared_keys(scenario: Scenario) -> set[str]:
    """intent.md「留给裁判的事实」那一节里人已经点过名的键。

    没点名的键不是错，但是个信号：验收路线要它，意图书没交代，runner 只能靠
    运行时补的清单去凑——历史上「context.yaml 里没有 xxx」是最大单一失败原因。
    """
    facts = (load_intent(scenario).get("sections") or {}).get("facts") or ""
    return set(re.findall(r"^\s*-?\s*`?([A-Za-z_][A-Za-z0-9_]*)`?\s*:", facts, re.M))


def _display(value: Any) -> str:
    text = value if isinstance(value, str) else yaml.safe_dump(
        value, allow_unicode=True, default_flow_style=True, width=10**6).strip()
    text = text.strip()
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS] + "…"


def describe(scenario: Scenario, facts_dir: Path) -> dict[str, Any]:
    """验收路线引用到的每个 context 变量：名字、上次取值、意图书里有没有交代。"""
    last, last_run = _facts(facts_dir, scenario.name)
    declared = _declared_keys(scenario)
    keys = required_context_keys(scenario)
    variables = [{
        "key": key,
        "last_value": _display(last[key]) if key in last else "",
        "known": key in last,
        "declared": key in declared,
    } for key in keys]
    # 上次交了、但这次验收路线已经不引用的，单独列出来当「多余的」——人删过一步之后
    # intent.md 常常忘了跟着删，runner 就一直在白白多交几个值。
    extra = sorted(k for k in declared if k not in set(keys))
    return {
        "scenario": scenario.name,
        "variables": variables,
        "unused_declared": extra,
        "last_run": last_run,
        "envs": list(scenario.envs),
        "default_env": scenario.default_env,
    }
