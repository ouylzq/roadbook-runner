"""配置中心：环境变量和路径的唯一来源。

以前 `Path(__file__).resolve().parent.parent` 在五个文件里各算一遍，
`os.getenv` 散在三处。现在只有这里读环境，其它模块拿 Settings 对象。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_max_tokens: int
    llm_temperature: float | None
    llm_timeout_seconds: float       # 单次请求最多等多久，超了当失败处理（不是总预算，是每次尝试的上限）
    llm_max_retries: int             # 超时/连接错/429/5xx 最多重试几次；请求参数错这类重试没用的不重试
    default_skill: str
    default_target_url: str

    # 跑不动时的闸门。这里是全局默认值，单个子代理可以在 agents/<名字>.md 的
    # frontmatter 里覆盖（runner 和裁判的节奏不一样，不该共用一套数）。
    stall_nudge_after: int          # 环境连续几轮没变化 → 往对话里插一条提醒
    stall_abort_after: int          # 再撑几轮还是没变化 → 直接收口判 blocked
    max_wall_seconds: int           # 单个 agent 的墙上时钟上限，0 = 不限
    max_total_tokens: int           # 单个 agent 累计 prompt token 上限，0 = 不限
    dedupe_observations: bool       # 页面没变时 observe 只回一句「跟上次一样」
    prompt_cache: bool              # 给稳定前缀打缓存断点（读取只按 0.1× 计）
    compact_at_tokens: int          # 上下文涨到这个数就压缩重建一次，0 = 不压缩
    keep_recent_turns: int          # 压缩时保留最近几轮原文（0 = 只留摘要）
    max_concurrent_runs: int        # 同时最多几个跑批在跑，0 = 不限；超过的排队等空位

    # Vercel Deployment Protection。密钥只由浏览器驱动按精确 host 白名单加到请求头，
    # 不进 roadbook 资产、模型提示词、运行日志或录屏。
    vercel_bypass_secret: str
    vercel_bypass_hosts: tuple[str, ...]

    # 原子记忆层（runtime/atoms.py）。模型只能提议和投票，代码按这些阈值数票落盘。
    atom_memory: bool               # 总开关；关掉就退回「原子只读」
    atom_confirms_append: int       # 给已有原子追加一条（变体 / 失败方式）要几票确认
    atom_confirms_canonical: int    # 改「它长什么样」要几票（且须来自不同场景）；这版只收不办
    atom_refutes_drop: int          # 候选被几票否决就丢
    atom_refutes_demote: int        # 已落盘的条目被几个不同跑批否决就划掉（预留，这版不做）
    atom_pending_ttl_days: int      # 候选多少天没人验证就过期

    @property
    def agents_dir(self) -> Path:
        return _env_path("QA_AGENT_AGENTS", self.root / "agents")

    @property
    def skills_dir(self) -> Path:
        explicit = os.getenv("QA_AGENT_SKILLS")
        if explicit:
            return Path(explicit).expanduser()
        # ROADBOOK_REPO 指向 skill 根；SkillLoader 要的是它的父目录。这样独立开发时可指向
        # 任意 checkout，在本仓库 checkout 里则直接用旁边的 ../roadbook。
        roadbook_repo = os.getenv("ROADBOOK_REPO")
        if roadbook_repo:
            return Path(roadbook_repo).expanduser().resolve().parent
        return self.root / "skills"

    @property
    def prompts_dir(self) -> Path:
        # 和 agents/ skills 一样可以整目录换掉：想改任务说明、交接、裁判提示词，
        # 拷一份出去改、指过去就行，不用动仓库里的。
        return _env_path("QA_AGENT_PROMPTS", self.root / "prompts")

    @property
    def storage_dir(self) -> Path:
        return self.root / "storage"

    @property
    def database_path(self) -> Path:
        return self.root / "qa-agent.db"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    @property
    def frontend_dir(self) -> Path:
        return self.root / "frontend"

    @classmethod
    def from_env(cls, root: Path = PROJECT_ROOT) -> Settings:
        load_dotenv(root / ".env")
        temperature = os.getenv("QA_AGENT_TEMPERATURE", "").strip()
        return cls(
            root=root,
            llm_base_url=os.getenv("QA_AGENT_BASE_URL", "https://api.anthropic.com/v1/"),
            llm_api_key=os.getenv("QA_AGENT_API_KEY", "dummy"),
            llm_model=os.getenv("QA_AGENT_MODEL", "claude-sonnet-5"),
            llm_max_tokens=int(os.getenv("QA_AGENT_MAX_TOKENS", "4096")),
            llm_temperature=float(temperature) if temperature else None,
            # 实测撞过 3.6 万 prompt token、250 completion token 的调用花了 125 秒——
            # 跟 token 量对不上，是网关偶发的延迟尖峰，不是模型在"想"。SDK 默认单次请求
            # 能等到 600 秒，一次卡顿就在那干等 10 分钟。90 秒够盖住见过的最长正常调用
            # （4096 completion token 撞满上限那次实测 56 秒），卡得更久的直接判失败换一次连接重试。
            llm_timeout_seconds=float(os.getenv("QA_AGENT_LLM_TIMEOUT_SECONDS", "90")),
            llm_max_retries=int(os.getenv("QA_AGENT_LLM_MAX_RETRIES", "2")),
            default_skill=os.getenv("QA_AGENT_DEFAULT_SKILL", "roadbook"),
            default_target_url=os.getenv("QA_AGENT_TARGET_URL", "http://localhost:4173/"),
            stall_nudge_after=int(os.getenv("QA_AGENT_STALL_NUDGE_AFTER", "6")),
            stall_abort_after=int(os.getenv("QA_AGENT_STALL_ABORT_AFTER", "10")),
            # 墙上时钟默认给得很松：实测跑通的场次模型耗时 262~512 秒，再加等待也远够用。
            # 它防的是「挂住不动」，不是「跑得慢」——真正分辨卡死的是上面两个。
            max_wall_seconds=int(os.getenv("QA_AGENT_MAX_WALL_SECONDS", "1800")),
            # 默认关：实测 PASS 的场次用到过 3.2M prompt token，卡死那次是 4.3M，
            # 两者离得太近，拿它当闸会误伤正常跑批。要硬封顶花费的人自己打开。
            max_total_tokens=int(os.getenv("QA_AGENT_MAX_TOTAL_TOKENS", "0")),
            dedupe_observations=_env_flag("QA_AGENT_DEDUPE_OBSERVATIONS", True),
            prompt_cache=_env_flag("QA_AGENT_PROMPT_CACHE", True),
            # 200k 窗口的七成。实测最高峰值 170,945（85%），撞上限的后果是整趟跑批报错，
            # 没有任何降级。压缩会让缓存作废一次（新前缀要重写），所以阈值要高、别频繁触发。
            compact_at_tokens=int(os.getenv("QA_AGENT_COMPACT_AT_TOKENS", "140000")),
            # 最后几条消息装着当前页面的完整观测（结构树、可见文本、ref 编号），
            # 摘要复述不出来。留 2 轮原文，压缩之后就不用浪费一轮重新 observe。
            keep_recent_turns=int(os.getenv("QA_AGENT_KEEP_RECENT_TURNS", "2")),
            # 默认 10：每个跑批是一个真实 Chromium 进程，同时开太多会把机器压垮，
            # 也容易撞上共用固定账号的场景（login 那类）互相抢发码配额。
            # 超过上限不拒绝——排队等空位，跑批发起方不用重试。
            max_concurrent_runs=int(os.getenv("QA_AGENT_MAX_CONCURRENT_RUNS", "10")),
            vercel_bypass_secret=os.getenv("VERCEL_AUTOMATION_BYPASS_SECRET", "").strip(),
            vercel_bypass_hosts=tuple(
                host.strip().lower()
                for host in os.getenv("QA_AGENT_VERCEL_BYPASS_HOSTS", "").split(",")
                if host.strip()
            ),
            # 阈值按爆炸半径分级：追加不动老做法，最坏多试一步，1 票够；改老做法会让顺利回放
            # 改道，2 票且不同场景，而且只生成建议。丢错一条代价很低（下次踩到会重新提出来），
            # 所以否决 1 票就丢。30 天复用 atoms_status.py 的「陈旧」口径。
            atom_memory=_env_flag("QA_AGENT_ATOM_MEMORY", True),
            atom_confirms_append=int(os.getenv("QA_AGENT_ATOM_CONFIRMS_APPEND", "1")),
            atom_confirms_canonical=int(os.getenv("QA_AGENT_ATOM_CONFIRMS_CANONICAL", "2")),
            atom_refutes_drop=int(os.getenv("QA_AGENT_ATOM_REFUTES_DROP", "1")),
            atom_refutes_demote=int(os.getenv("QA_AGENT_ATOM_REFUTES_DEMOTE", "2")),
            atom_pending_ttl_days=int(os.getenv("QA_AGENT_ATOM_PENDING_TTL_DAYS", "30")),
        )


def _env_path(var: str, default: Path) -> Path:
    raw = os.getenv(var)
    return Path(raw).expanduser() if raw else default


def _env_flag(var: str, default: bool) -> bool:
    raw = os.getenv(var)
    return default if raw is None else raw.strip().lower() not in ("0", "false", "no", "off", "")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
