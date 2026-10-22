"""领域类型：状态枚举和场景描述。

以前这些都是裸字符串，合法值靠注释维护；拼错一个字母静默通过。
StrEnum 保证 JSON 序列化仍是普通字符串，前端和历史 run.json 不受影响。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class Outcome(StrEnum):
    """agent 循环怎么收的口。"""

    DONE = "done"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    MAX_ITERATIONS = "max_iterations"


class RunStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    ERROR = "error"
    STOPPED = "stopped"          # 人手动叫停，不是模型或系统失败


class Verdict(StrEnum):
    """判分结论。EXPLORED = 没有验收路线、只做了探索；ASK = 卡住等人回答。"""

    NONE = ""
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCK = "BLOCK"
    EXPLORED = "EXPLORED"
    ASK = "ASK"
    STOPPED = "STOPPED"


class ScenarioMode(StrEnum):
    """展示用标签：有路书可照走，还是需要探索。不再用它选 agent。"""

    PLAYBOOK = "playbook"
    EXPLORE = "explore"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    dir: Path
    has_playbook: bool
    has_expect: bool
    start_url: str | None
    # intent.md frontmatter 里人已经声明过的环境清单。资产层早就写着 `envs: [dev, prod]`，
    # 但运行时一直没读它——看板因此只能跑 config/env.json 当前指着的那个环境。
    envs: tuple[str, ...] = ()
    default_env: str = ""
    title: str = ""          # intent.md 的一级标题（去掉「场景：」前缀），给人看的名字；目录名是给机器的
    project: str = ""        # roadbook 的项目层：scenarios/<项目>/<场景>/；平铺布局为空串，看板据此分组

    @property
    def mode(self) -> ScenarioMode:
        return ScenarioMode.PLAYBOOK if self.has_playbook else ScenarioMode.EXPLORE

    def read(self, filename: str) -> str:
        return (self.dir / filename).read_text(encoding="utf-8")

    def read_or_empty(self, filename: str) -> str:
        path = self.dir / filename
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def to_dict(self) -> dict[str, Any]:
        """API 契约：前端依赖 name/description/has_playbook/has_expect/mode/start_url。"""
        data = asdict(self)
        data.pop("dir")
        data["mode"] = self.mode.value
        data["envs"] = list(self.envs)
        return data
