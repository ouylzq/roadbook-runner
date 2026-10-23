"""config/env.json 的网页可见/可写边界。

诚实版边界说明：这层只防**看板（PM 的浏览器）**。runner 是用 read_resource 读
config/env.json 原文的，受保护键会进 LLM 上下文——别把这层当成对 LLM 的隔离。
redis、凭据指针、账号、内网网关：不对看板暴露，也永远不接受网页写入。
（要对 LLM 隔离，等 env.local.json 覆盖层落地后把秘密键搬过去，见 next 档。）

判定规则集中在这一个模块，GET 的脱敏和 POST/DELETE 的写校验用同一份——两边不会漂。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# 这些键（无论顶层还是环境组里）永远不对看板暴露、也永远不接受网页写入
PROTECTED_KEYS = frozenset({
    "redis", "redis_host", "redis_port", "login_code_key",
    "credentials_files", "credentials_file", "credentials_note",
    "fixture_email", "api_base", "ops_base", "account",
    "protected_user_ids", "disposable_account_email_re",
    "test_account_tag", "test_account_tag_note", "note",
    "interview_web_note", "credentials",
})
# 内置的公开键：可见可写，但不能从看板删除（别的场景可能正引用着）
BUILTIN_PUBLIC = frozenset({"researcher_web", "interview_web"})
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
ENV_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,20}$")


def _is_url(value: object) -> bool:
    """只收 origin 形态：http(s)、无 userinfo/query/fragment、path 为空或根。
    收紧的原因：宽松的"http 开头"会放进 sentry_dsn / webhook（带 token 的查询串）
    这类把秘密带进看板的键。"""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if parts.username or parts.password or parts.query or parts.fragment:
        return False
    return parts.path in ("", "/")


def is_public_entry(key: str, value: object) -> bool:
    """看板可见/可写的判据：键名合法、不在保护名单、值是 origin 形态的地址。"""
    return bool(KEY_RE.match(key or "")) and key not in PROTECTED_KEYS and _is_url(value)


def sanitize_skill_config(cfg: dict) -> dict:
    """env.json 原文 → 看板可见的那一份。合法域名键原样透出（含后来加的），其余一概不留。"""
    if not isinstance(cfg, dict):
        return {}
    out: dict = {}
    if isinstance(cfg.get("env"), str):
        out["env"] = cfg["env"]
    for k, v in cfg.items():
        if k in ("env", "environments") or isinstance(v, (dict, list)):
            continue
        if is_public_entry(k, v):
            out[k] = v
    envs = cfg.get("environments")
    if isinstance(envs, dict):
        out["environments"] = {
            name: {k: v for k, v in (entries or {}).items() if is_public_entry(k, v)}
            for name, entries in envs.items() if isinstance(entries, dict)
        }
    return out


def validate_entry(env: str, key: str, value: object, *, known_envs: list[str] | None = None,
                   builtin_readonly_envs: tuple[str, ...] = ("prod",)) -> str | None:
    """网页写入一条配置前的校验。返回 None = 放行，否则返回给人看的错误。

    known_envs：已存在的环境名清单——不给就只做格式校验；给了则只许写已存在的环境
    （打错 "prd" 不该悄悄多出一组环境）。
    builtin_readonly_envs：这些环境组的内置键（researcher_web 等）只读——prod 的
    researcher_web 同时是 runner 的目标和裁判的 origin 基准，网页覆写等于开放钓鱼面；
    真要改走服务端文件编辑。
    """
    if not ENV_RE.match(env or ""):
        return f"环境名不合法: {env!r}（小写字母/数字/连字符，≤21 位）"
    if known_envs is not None and env not in known_envs:
        return f"环境 {env!r} 不存在（已有：{'、'.join(known_envs)}）；新建环境走服务端文件编辑"
    if key in PROTECTED_KEYS:
        return f"{key!r} 是受保护的内置键，不能从看板写"
    if key in BUILTIN_PUBLIC and env in builtin_readonly_envs:
        return f"{env} 环境的 {key!r} 是内置键，只读（它同时是 runner 目标和裁判基准）；要改走服务端文件编辑"
    if not KEY_RE.match(key or ""):
        return f"键名不合法: {key!r}（小写字母开头，只含小写字母/数字/下划线，≤41 位）"
    if not _is_url(value):
        return ("值必须是 origin 形态的地址（http(s)://host，不带路径/查询串/fragment）；"
                "密钥类走服务端 .env，带 token 的回调地址别放这里")
    return None


def extract_origins(text: str) -> list[str]:
    """从自由文本（PM 的场景描述、被测地址栏）里抽 http(s) 地址，归一成 origin。
    确定性正则，不经过模型——配置的发现永远可复现。"""
    out: list[str] = []
    for m in re.finditer(r"""https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s"'<>)]*)?""", text or ""):
        parts = urlsplit(m.group(0))
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in out:
            out.append(origin)
    return out


def reverse_lookup(cfg: dict, origin: str) -> tuple[str, str] | None:
    """在现有配置里反查哪个环境组的哪个键等于这个 origin（尾斜杠不敏感）。返回 (环境, 键)。"""
    for env, entries in (cfg.get("environments") or {}).items():
        if not isinstance(entries, dict):
            continue
        for k, v in entries.items():
            if isinstance(v, str) and v.rstrip("/") == origin.rstrip("/"):
                return env, k
    return None


def suggest_key(origin: str) -> str:
    """按确定性规则预填键名：host 首段去掉环境标记，加 _web。人可在确认卡上改。"""
    label = urlsplit(origin).netloc.split(":")[0].split(".")[0]
    for marker in ("-dev", "-staging", "-preview", "-test"):
        if label.endswith(marker):
            label = label[: -len(marker)]
            break
    return f"{label or 'site'}_web"
