"""跑之前的自检：环境通不通、上次的产物看不看得见。

刻意把「产物可见性」也纳入自检——录屏和日志是这套东西唯一能让人复核的凭据，
文件在不在、能不能打开，必须是可机器验证的，不能靠"应该没问题"。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .traces import Run

WEBM_MAGIC = b"\x1a\x45\xdf\xa3"     # EBML 头，WebM 文件的前四个字节


@dataclass(frozen=True)
class Check:
    ok: bool
    title: str
    detail: str = ""
    warn: bool = False

    @property
    def mark(self) -> str:
        return "!" if self.warn else ("✓" if self.ok else "✗")


class Doctor:
    def __init__(self, settings: Settings, reports_dir: Path | None = None):
        self.settings = settings
        self.reports_dir = reports_dir or settings.reports_dir

    def env(self) -> list[Check]:
        s = self.settings
        return [
            Check(bool(s.llm_base_url), "LLM 地址", s.llm_base_url or "未配置（.env 里 QA_AGENT_BASE_URL）"),
            Check(s.llm_api_key != "dummy", "LLM 密钥", "已配置" if s.llm_api_key != "dummy" else "未配置"),
            Check(bool(s.llm_model), "模型", s.llm_model),
        ]

    def gateway(self) -> list[Check]:
        s = self.settings
        req = urllib.request.Request(s.llm_base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {s.llm_api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ids = [m["id"] for m in json.load(resp).get("data", [])]
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return [Check(False, "模型服务可达", f"{e}（内网网关需要 Tailscale）")]
        hit = s.llm_model in ids
        return [Check(True, "模型服务可达", f"{len(ids)} 个模型"),
                Check(True, "目标模型在列表里", s.llm_model if hit else f"{s.llm_model} 不在列表（仍可能可用）", warn=not hit)]

    def target(self, url: str) -> list[Check]:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return [Check(True, "被测靶子", f"{url} HTTP {resp.status}")]
        except Exception as e:
            return [Check(False, "被测靶子", f"{url} 不可达：{e}")]

    def browser(self) -> list[Check]:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                launched = pw.chromium.launch(headless=True)
                version = launched.version
                launched.close()
            return [Check(True, "浏览器", f"Chromium {version}")]
        except Exception as e:
            return [Check(False, "浏览器", f"{type(e).__name__}: {e}")]

    def artifacts(self, run_id: str | None = None) -> list[Check]:
        """上次跑批的录屏和日志，是不是真的躺在盘上、能打开。"""
        storage = self.settings.storage_dir
        candidates = sorted((d for d in storage.iterdir() if d.is_dir() and (d / "run.json").exists()),
                            reverse=True) if storage.is_dir() else []
        if not candidates:
            return [Check(True, "上次跑批的产物", "还没有跑过", warn=True)]
        run_dir = storage / run_id if run_id else candidates[0]
        record = Run.load(run_dir)
        if record is None:
            return [Check(False, "上次跑批的产物", f"{run_dir.name} 的 run.json 读不出来")]

        out = [Check(True, "上次跑批", f"{run_dir.name} → {record.verdict or record.status}")]
        for role in ("runner", "judge"):
            videos = sorted((run_dir / role / "video").glob("*.webm"))
            if videos:
                valid = videos[0].read_bytes()[:4] == WEBM_MAGIC
                out.append(Check(valid, f"  {role} 录屏",
                                 f"{videos[0].stat().st_size // 1024} KB " + ("可播放" if valid else "文件头不对，可能损坏")))
            else:
                out.append(Check(role == "judge", f"  {role} 录屏", "没有", warn=(role == "judge")))
            shots = list((run_dir / role / "shots").glob("*.png"))
            if shots:
                out.append(Check(True, f"  {role} 截图", f"{len(shots)} 张"))

        for filename, label in (("context.yaml", "  事实(context)"), ("judgment.yaml", "  判定(judgment)")):
            path = run_dir / filename
            present = path.exists() and path.stat().st_size > 0
            out.append(Check(True, label, f"{path.stat().st_size} 字节" if present else "没有", warn=not present))

        total_steps = len(record.steps) + len(record.judge_steps)
        out.append(Check(total_steps > 0, "  步骤日志", f"{total_steps} 步记在 run.json"))
        report = self.reports_dir / f"{run_dir.name}.md"
        out.append(Check(True, "  报告", f"reports/{report.name}" if report.exists() else "没有", warn=not report.exists()))
        return out

    def agents(self) -> list[Check]:
        """每个子代理的工具白名单，这个进程认不认得。

        2026-09-20 实测：往 agents/judge.md 加了个新工具，而跑着的服务进程里的工具表
        还是启动时那份，裁判阶段才炸，一次跑了 48 步的跑批整个报废。现在跑批里遇到
        不认识的名字只警告跳过（见 ToolRegistry.schemas），这里负责**开跑前就报出来**。
        """
        from tools import REGISTRY

        from .subagents import SubAgentLoader

        out: list[Check] = []
        try:
            agents = SubAgentLoader(self.settings).list_all()
        except Exception as e:
            return [Check(False, "  子代理定义", f"读不出来: {type(e).__name__}: {e}")]
        for agent in agents:
            unknown = REGISTRY.unknown_tools(agent.tools)
            limit = ("轮数不限（墙钟/停滞保护）" if agent.max_iterations <= 0
                     else f"上限 {agent.max_iterations} 轮")
            out.append(Check(
                not unknown, f"  {agent.name}",
                f"{len(agent.tools)} 个工具，{limit}" if not unknown
                else f"这些工具这个进程不认识：{'、'.join(unknown)}（会被跳过）",
                warn=bool(unknown),
            ))
        return out or [Check(False, "  子代理定义", "agents/ 下一个都没有")]

    def run_all(self, target_url: str) -> bool:
        groups = [("环境配置", self.env()), ("模型服务", self.gateway()),
                  ("被测目标", self.target(target_url)), ("浏览器", self.browser()),
                  ("子代理与工具", self.agents()),
                  ("产物可见性", self.artifacts())]
        all_ok = True
        for title, checks in groups:
            print(f"\n{title}")
            for c in checks:
                print(f"  {c.mark} {c.title:<26} {c.detail}")
                all_ok = all_ok and (c.ok or c.warn)
        print("\n" + ("全部通过，可以跑。" if all_ok else "有项目没过，先修上面标 ✗ 的。"))
        return all_ok
