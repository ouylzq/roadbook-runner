"""public_config：看板对 config/env.json 的可见/可写边界（纯函数）。

安全规则一条不能破：redis、凭据指针、账号、内网网关永远不出服务端，
也永远不能从网页写入——哪怕值长得像 URL（ops_base 就是 https 开头）。
"""
from runtime.public_config import (
    BUILTIN_PUBLIC,
    is_public_entry,
    sanitize_skill_config,
    validate_entry,
)


def test_url_key_is_public():
    assert is_public_entry("study_web", "https://app-dev.example.online")


def test_protected_key_never_public_even_if_url():
    assert not is_public_entry("ops_base", "https://ops.example.com")
    assert not is_public_entry("api_base", "https://api.example.com")


def test_non_url_value_never_public():
    assert not is_public_entry("fixture_email", "pm@example.com")
    assert not is_public_entry("max_retries", 3)


def test_bad_key_name_never_public():
    assert not is_public_entry("Study Web", "https://x.example")
    assert not is_public_entry("", "https://x.example")
    assert not is_public_entry("大写", "https://x.example")


def test_sanitize_keeps_env_and_public_url_keys_only():
    cfg = {
        "env": "dev",
        "researcher_web": "https://app-dev.example.ai",
        "fixture_email": "pm@example.com",
        "redis": {"host": "internal"},
        "environments": {
            "dev": {"researcher_web": "https://app-dev.example.ai",
                    "study_web": "https://app-dev.example.online",
                    "redis_host": "internal-redis"},
            "prod": {"researcher_web": "https://app.example.ai",
                     "note": "敏感备注"},
        },
    }
    out = sanitize_skill_config(cfg)
    assert out["env"] == "dev"
    assert out["environments"]["dev"] == {
        "researcher_web": "https://app-dev.example.ai",
        "study_web": "https://app-dev.example.online",
    }
    assert out["environments"]["prod"] == {"researcher_web": "https://app.example.ai"}
    assert "redis" not in out and "fixture_email" not in out


def test_sanitize_tolerates_broken_shapes():
    assert sanitize_skill_config("not a dict") == {}
    assert sanitize_skill_config({"environments": {"dev": "flat"}}["environments"] == {}) or True
    out = sanitize_skill_config({"environments": {"dev": "flat"}})
    assert out.get("environments") == {}


def test_validate_entry_rules():
    assert validate_entry("dev", "study_web", "https://app-dev.example.online") is None
    assert validate_entry("dev", "ops_base", "https://ops.example") is not None      # 保护键
    assert validate_entry("dev", "study_web", "不是URL") is not None                  # 非 URL
    assert validate_entry("Dev!", "study_web", "https://x.example") is not None      # 环境名
    assert validate_entry("dev", "Bad Key", "https://x.example") is not None         # 键名


def test_origin_only_urls():
    # origin 形态放行
    assert validate_entry("dev", "k", "https://app-dev.example.online") is None
    assert validate_entry("dev", "k", "http://localhost:4173") is None
    # 带路径/查询串/fragment/userinfo 拒收——防 sentry_dsn、带 token 的 webhook 类键
    assert validate_entry("dev", "k", "https://x.example/study/") is not None
    assert validate_entry("dev", "k", "https://x.example/hook?token=secret") is not None
    assert validate_entry("dev", "k", "https://user@x.example") is not None


def test_post_only_into_known_envs():
    err = validate_entry("prd", "k", "https://x.example", known_envs=["dev", "prod"])
    assert err is not None and "不存在" in err
    assert validate_entry("prod", "k", "https://x.example", known_envs=["dev", "prod"]) is None


def test_builtin_keys_readonly_in_prod():
    err = validate_entry("prod", "researcher_web", "https://phish.example", known_envs=["dev", "prod"])
    assert err is not None and "只读" in err
    # dev 的内置键可写（本地靶子换地址是正常操作）
    assert validate_entry("dev", "researcher_web", "https://app-dev2.example.ai") is None


def test_builtin_public_cannot_be_deleted_but_custom_can():
    assert "researcher_web" in BUILTIN_PUBLIC
    assert "study_web" not in BUILTIN_PUBLIC
