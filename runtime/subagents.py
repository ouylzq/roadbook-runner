"""子代理定义：一份 agents/<名字>.md = frontmatter（工具白名单 / 轮数上限 / 模型）+ 提示词正文。

角色写成文件而不是硬编码在 Python 里：加一个角色 = 加一个 md。
身份用文件名，跟 Skill 一样——frontmatter 里的 name 只当展示标题。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

from .config import Settings, get_settings

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
DEFAULT_MAX_ITERATIONS = 40


@dataclass(frozen=True)
class SubAgent:
    name: str
    description: str
    prompt: str
    tools: tuple[str, ...] = field(default_factory=tuple)
    max_iterations: int = DEFAULT_MAX_ITERATIONS  # 0 = 不限；靠墙钟/停滞/人工停止收口
    model: str | None = None
    # 下面四个是「跑不动时的闸门」，不写就用 Settings 里的全局默认值。
    # 写在这里而不是只放全局：runner 要等页面、裁判只读，两者的节奏本来就不一样。
    stall_nudge_after: int = 0
    stall_abort_after: int = 0
    stall_landing: bool = False       # 停滞后进收口轮（只给 finish），裁判用；runner 不用
    max_wall_seconds: int = 0
    max_total_tokens: int = 0
    keep_recent_turns: int = 2


class SubAgentLoader:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.directory = self.settings.agents_dir

    def load(self, name: str) -> SubAgent:
        path = self.directory / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"没有这个子代理定义: {path}")
        text = path.read_text(encoding="utf-8")
        match = FRONTMATTER.match(text)
        meta = yaml.safe_load(match.group(1)) if match else {}
        body = text[match.end():] if match else text

        def limit(key: str, fallback: int) -> int:
            """frontmatter 写了就用它，没写就落到全局默认。"""
            raw = meta.get(key)
            return int(raw) if raw is not None else fallback

        s = self.settings
        return SubAgent(
            name=name,
            description=str(meta.get("description", "")),
            prompt=body.strip(),
            tools=tuple(meta.get("tools") or ()),
            max_iterations=int(meta.get("max_iterations", DEFAULT_MAX_ITERATIONS)),
            model=meta.get("model"),
            stall_nudge_after=limit("stall_nudge_after", s.stall_nudge_after),
            stall_abort_after=limit("stall_abort_after", s.stall_abort_after),
            stall_landing=bool(meta.get("stall_landing", s.stall_landing)),
            max_wall_seconds=limit("max_wall_seconds", s.max_wall_seconds),
            max_total_tokens=limit("max_total_tokens", s.max_total_tokens),
            keep_recent_turns=limit("keep_recent_turns", s.keep_recent_turns),
        )

    def list_all(self) -> list[SubAgent]:
        if not self.directory.is_dir():
            return []
        return [self.load(p.stem) for p in sorted(self.directory.glob("*.md"))]
