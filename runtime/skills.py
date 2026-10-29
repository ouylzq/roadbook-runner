"""技能系统。

一个技能 = 一个目录 + 一份 SKILL.md。SKILL.md 进系统提示词，目录里其它文件
（references/、atoms/、scenarios/）不预加载，由 agent 用 read_resource 按需取——渐进式披露。

roadbook 是一个技能，它内部的场景是这个技能的资源，所以「场景」由 Skill 自己管。
身份用目录名：同一套 SKILL.md 可能被挂在不同目录下（开源版 / 内部版），frontmatter 的 name 会撞。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Settings, get_settings
from .domain import Scenario

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "runs", "node_modules", ".idea", ".github"})
READABLE_SUFFIXES = frozenset({".md", ".yaml", ".yml", ".txt", ".json"})
RESOURCE_MAX_CHARS = 20000
BRIEF_MAX_RESOURCES = 120
# 只认 ASCII 的 URL 字符集：排除 CJK/全角标点（）「」等——否则「（新建研究流程，…）」
# 这类中文说明会被整段粘进地址（2026-10-26 实测把 start_url 污染成半句话，导航必挂）。
URL_RE = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[A-Za-z0-9\-._~!$&'()*+,;=:@%]*)?")
TITLE_RE = re.compile(r"^# (.+?)\s*$", re.M)
TITLE_PREFIX = re.compile(r"^(场景|Scenario)\s*[:：]\s*")
CONFIG_FILE = "config/env.json"     # 场景里 `environments.<env>.xxx` 这类引用指的就是它
INTENT_FILE = "intent.md"           # 有它的目录才算场景；项目分组目录（scenarios/<项目>/）没有它


def parse_frontmatter(text: str) -> dict:
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


@dataclass(frozen=True)
class Skill:
    name: str                    # 目录名 = 身份
    root: Path
    instructions: str            # SKILL.md 全文，进系统提示词
    description: str = ""
    title: str = ""              # frontmatter 里的名字，仅展示用

    # ---- 资源 ----------------------------------------------------------
    def resources(self) -> list[str]:
        out = []
        for path in sorted(self.root.rglob("*")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix in READABLE_SUFFIXES and path.name != "SKILL.md":
                out.append(str(path.relative_to(self.root)))
        return out

    def read(self, relative: str, max_chars: int = RESOURCE_MAX_CHARS) -> str:
        """读技能内的一份资源。路径必须落在技能目录内。"""
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError:
            return f"拒绝读取技能目录之外的路径: {relative}"
        if not target.is_file():
            near = [r for r in self.resources() if relative.lower() in r.lower()][:8]
            hint = ("\n可能想找:\n" + "\n".join(near)) if near else ""
            return f"资源不存在: {relative}{hint}"
        text = target.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= max_chars else text[:max_chars] + "\n…（已截断）"

    def reference(self, name: str) -> str:
        return (self.root / "references" / name).read_text(encoding="utf-8")

    def config(self) -> dict:
        """技能的环境配置（域名、账号、环境分组）。没有或坏了都当空——它只给看板做解释用，
        不进任何提示词；agent 自己是用 read_resource 读原文的。"""
        path = self.config_path
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE

    def brief(self) -> str:
        """给系统提示词用的技能说明：指令全文 + 资源目录。"""
        resources = self.resources()
        listing = "\n".join(f"  {r}" for r in resources[:BRIEF_MAX_RESOURCES])
        more = (f"\n  …另有 {len(resources) - BRIEF_MAX_RESOURCES} 份"
                if len(resources) > BRIEF_MAX_RESOURCES else "")
        return (
            f"# 已加载技能: {self.name}\n\n{self.instructions}\n\n"
            f"---\n## 本技能的可读资源（用 read_resource 按需取，不要凭空猜内容）\n"
            f"{listing}{more}\n"
        )

    # ---- 场景 ----------------------------------------------------------
    @property
    def scenarios_dir(self) -> Path:
        return self.root / "scenarios"

    def scenarios(self) -> list[Scenario]:
        """两层发现：`scenarios/<场景>/`（平铺）和 `scenarios/<项目>/<场景>/`（分组）都认，
        与开源版 roadbook `scripts/scenarios.py` 的 discover() 同一语义。
        只有带 intent.md 的目录才算场景——空壳目录、项目分组目录本身都不生成卡片。
        场景名 = 相对 scenarios/ 的路径（嵌套是 `<项目>/<场景>`），保证全技能唯一。"""
        if not self.scenarios_dir.is_dir():
            return []
        out: list[Scenario] = []
        for d in sorted(self.scenarios_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if (d / INTENT_FILE).is_file():
                out.append(self._scenario(d))
                continue
            for child in sorted(d.iterdir()):
                if child.is_dir() and not child.name.startswith(".") and (child / INTENT_FILE).is_file():
                    out.append(self._scenario(child))
        return out

    def project_groups(self) -> list[str]:
        """scenarios/ 下的项目分组目录名——**含还没有场景的空分组**（如 定量/）。
        自己带 intent.md 的目录是场景不是分组（如 search-first-result/），不算。"""
        if not self.scenarios_dir.is_dir():
            return []
        out = []
        for d in sorted(self.scenarios_dir.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and not (d / INTENT_FILE).is_file():
                out.append(d.name)
        return out

    def scenario(self, name: str) -> Scenario:
        directory = (self.scenarios_dir / name).resolve()
        try:
            directory.relative_to(self.scenarios_dir.resolve())
        except ValueError:
            raise FileNotFoundError(f"场景名越界: {name}") from None
        if not directory.is_dir():
            raise FileNotFoundError(f"技能 {self.name} 里没有场景 {name}")
        return self._scenario(directory)

    def _scenario(self, directory: Path) -> Scenario:
        intent_path = directory / INTENT_FILE
        intent = intent_path.read_text(encoding="utf-8") if intent_path.exists() else ""
        urls = URL_RE.findall(intent)
        meta = parse_frontmatter(intent)
        envs = tuple(str(e).strip() for e in (meta.get("envs") or []) if str(e).strip())
        default_env = str(meta.get("default_env") or "").strip()
        # 只写了 default_env 没写 envs 的，就当只有这一个可选——别让下拉框空着。
        if default_env and default_env not in envs:
            envs = (default_env, *envs)
        title_m = TITLE_RE.search(intent)
        title = TITLE_PREFIX.sub("", title_m.group(1).strip()) if title_m else ""
        rel = directory.resolve().relative_to(self.scenarios_dir.resolve())
        name = "/".join(rel.parts)
        return Scenario(
            name=name,
            title=title,
            description=str(meta.get("description", "")),
            dir=directory,
            has_playbook=(directory / "playbook.md").exists(),
            has_expect=(directory / "expect.yaml").exists(),
            start_url=urls[0] if urls else None,
            envs=envs,
            default_env=default_env or (envs[0] if envs else ""),
            project=rel.parts[0] if len(rel.parts) > 1 else "",
        )


class SkillLoader:
    def __init__(self, settings: Settings | None = None):
        self.directory = (settings or get_settings()).skills_dir

    def load(self, name: str) -> Skill:
        root = self.directory / name
        manifest = root / "SKILL.md"
        if not manifest.exists():
            raise FileNotFoundError(f"技能缺少 SKILL.md: {manifest}")
        text = manifest.read_text(encoding="utf-8")
        meta = parse_frontmatter(text)
        return Skill(name=name, root=root, instructions=text,
                     description=str(meta.get("description", "")),
                     title=str(meta.get("name", name)))

    def list_all(self) -> list[Skill]:
        if not self.directory.is_dir():
            return []
        return [self.load(d.name) for d in sorted(self.directory.iterdir())
                if (d / "SKILL.md").exists()]
