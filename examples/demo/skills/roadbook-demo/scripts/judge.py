#!/usr/bin/env python3
"""把裁判 agent 交回来的 judgment.yaml 换算成退出码。

用法: judge.py <场景目录> <run目录>

`expect.yaml` 是一条有序路线（steps[].go + steps[].check[]）。`judgment.yaml` 必须
和它镜像：steps 的条数与顺序、每步的 go 文本、每步 check 的条数与 fact 文本。

退出码:
  0  PASS   每条 check 都是 pass，且都带证据
  1  FAIL   至少有一条 check 是 fail
  2  BLOCK  runner 标记了环境阻塞（run目录/blocked.txt），或 judgment.yaml 缺失 /
            与 expect.yaml 不镜像 / 有 check 是 blocked / pass|fail 却没有证据 /
            reached=false 的步里出现了非 blocked 的 check /
            expect.yaml 引用的 context.xxx 在 run目录/context.yaml 里找不到

引用的值也在这里查全不查对：expect.yaml 里每个 context.xxx 必须在 context.yaml 里有键。
缺了，裁判要么瞎猜要么全写 blocked，不如在这里直接 BLOCK 并说清缺什么。

同时把逐条结果写进 run目录/judge-output.txt，把一行结论写进 run目录/verdict.txt。

这个脚本**刻意不比对语义**。判决由裁判 agent 按 references/judge-protocol.md 给出，
这里只保证它没偷懒——每步都走了、每条都答了、每个答案都有原文兜底。
"""
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

PASS, FAIL, BLOCK = 0, 1, 2

REF_RE = re.compile(r"\bcontext\.([A-Za-z_][A-Za-z0-9_]*)")
NOT_A_KEY = {"yaml", "yml", "xxx"}          # 「context.yaml」是文件名，不是引用
PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def load_yaml(p: Path):
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def norm(s) -> str:
    return str(s or "").strip()


def norm_key(s) -> str:
    """给镜像比对用的比较键：连标点空白一起去掉。

    裁判抄 go/fact 时把逗号改写成括号这类无害重排版，字面内容没变，不该算「不镜像」
    （2026-09-17 实测踩过：只因这一个标点差异，就把已经每条都 pass 的场景判成了 BLOCK）。
    真正的防造假底线是文字内容本身，不是标点写法——所以比较键去标点，但 emit 时仍然
    显示 norm() 过的原文，人看报告不受影响。"""
    return PUNCT_RE.sub("", norm(s))


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return BLOCK
    scenario_dir, run_dir = Path(sys.argv[1]), Path(sys.argv[2])
    run_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    verdict = PASS
    n_fail = n_block = 0

    def emit(s: str) -> None:
        lines.append(s)
        print(s)

    def finish(code: int, headline: str) -> int:
        word = {PASS: "PASS", FAIL: "FAIL", BLOCK: "BLOCK"}[code]
        (run_dir / "verdict.txt").write_text(f"{word} — {headline}\n", encoding="utf-8")
        (run_dir / "judge-output.txt").write_text(
            f"# judge.py {datetime.now().isoformat(timespec='seconds')}\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        return code

    def block(msg: str) -> None:
        nonlocal verdict, n_block
        emit(f"[BLOCK] {msg}")
        verdict = BLOCK
        n_block += 1

    # 0) runner 说环境挂了：不看裁判，直接 BLOCK
    marker = run_dir / "blocked.txt"
    if marker.exists():
        reason = marker.read_text(encoding="utf-8").strip() or "runner 标记环境阻塞"
        emit(f"[BLOCK] runner: {reason}")
        return finish(BLOCK, reason)

    # 1) 两份文件都得在
    expect_path = scenario_dir / "expect.yaml"
    judgment_path = run_dir / "judgment.yaml"
    if not expect_path.exists():
        emit(f"[BLOCK] 缺 {expect_path}")
        return finish(BLOCK, "场景没有 expect.yaml")
    if not judgment_path.exists():
        emit(f"[BLOCK] 缺 {judgment_path}（裁判没交作业）")
        return finish(BLOCK, "裁判没有产出 judgment.yaml")

    exp_steps = load_yaml(expect_path).get("steps") or []
    jud_steps = load_yaml(judgment_path).get("steps") or []
    total = sum(len(s.get("check") or []) for s in exp_steps)

    # 1.5) 路线里引用的 context.xxx，context.yaml 里都得有（查全不查对）
    keys: set[str] = set()
    for s in exp_steps:
        for text in [s.get("go", "")] + list(s.get("check") or []):
            keys.update(k for k in REF_RE.findall(str(text)) if k not in NOT_A_KEY)
    missing: list[str] = []
    if keys:
        src = run_dir / "context.yaml"
        if not src.exists():
            missing.append(f"缺 context.yaml（expect.yaml 用到了 {'、'.join(f'context.{k}' for k in sorted(keys))}）")
        else:
            have = load_yaml(src)
            have = have if isinstance(have, dict) else {}
            missing += [f"context.yaml 里没有 {k}（expect.yaml 用到了 context.{k}）" for k in sorted(keys - set(map(str, have)))]
    if missing:
        for m in missing:
            emit(f"[BLOCK] {m}")
        return finish(BLOCK, f"路线引用的值不全（{len(missing)} 处）——context.yaml 由 runner 在交棒前写")

    # 2) 步数对得上
    if len(jud_steps) != len(exp_steps):
        emit(f"[BLOCK] expect.yaml 有 {len(exp_steps)} 步，judgment.yaml 有 {len(jud_steps)} 步")
        return finish(BLOCK, "裁判漏走或多走了步骤")

    # 3) 逐步、逐条镜像校验
    for si, (es, js) in enumerate(zip(exp_steps, jud_steps), 1):
        e_go = norm(es.get("go"))
        e_checks = [norm(c) for c in (es.get("check") or [])]
        j_checks = js.get("check") or []
        reached = js.get("reached")
        # go 一律以 expect.yaml 为准，judgment 里写不写都不影响判定。
        # 以前这里要求逐字镜像 go，两次栽在同一类事上：一次是逗号被抄成括号，
        # 一次是 196 字的 go 被裁判用「……」缩写。而 go 是导航指令、不是断言——
        # 真正的断言是下面每条 check 的 fact，那些仍然逐字镜像。让裁判重抄一遍长导航，
        # 换不来任何防造假的保证（抄对了也不证明它真去过），却实打实制造假阳性。
        emit(f"── 第 {si} 步: {e_go}  (reached={reached})")

        if reached not in (True, False):
            block(f"第 {si} 步的 reached 必须是 true/false，实际是 {reached!r}")
            n_block += len(e_checks) - 1
            continue
        if len(j_checks) != len(e_checks):
            block(f"第 {si} 步 expect 有 {len(e_checks)} 条 check，judgment 有 {len(j_checks)} 条")
            n_block += len(e_checks) - 1
            continue

        for ci, (e_fact, jc) in enumerate(zip(e_checks, j_checks), 1):
            tag = f"{si}.{ci}"
            j_fact = norm(jc.get("fact"))
            result = norm(jc.get("result")).lower()
            evidence = norm(jc.get("evidence"))
            note = norm(jc.get("note"))

            if norm_key(j_fact) != norm_key(e_fact):
                block(f"{tag} fact 文本不一致\n    期望: {e_fact}\n    裁判: {j_fact}")
            elif result not in ("pass", "fail", "blocked"):
                block(f"{tag} result 非法: {result!r}  —— {e_fact}")
            elif reached is False and result != "blocked":
                block(f"{tag} 该步 reached=false，却给了 {result}  —— {e_fact}")
            elif result in ("pass", "fail") and not evidence:
                block(f"{tag} {result} 但没有 evidence  —— {e_fact}")
            elif result == "blocked":
                block(f"{tag} {e_fact}\n    note: {note or '(裁判未说明)'}")
            elif result == "fail":
                n_fail += 1
                if verdict != BLOCK:
                    verdict = FAIL
                emit(f"[FAIL]  {tag} {e_fact}\n    证据: {evidence}" + (f"\n    note: {note}" if note else ""))
            else:
                emit(f"[PASS]  {tag} {e_fact}\n    证据: {evidence}")

    if verdict == PASS:
        return finish(PASS, f"{len(exp_steps)} 步 {total} 条 check 全部通过，均附页面原文证据")
    if verdict == FAIL:
        return finish(FAIL, f"{n_fail}/{total} 条 check 不成立（详见 judge-output.txt）")
    return finish(
        BLOCK,
        f"{n_block}/{total} 条无法判定（缺证据 / 不镜像 / 裁判 blocked），另有 {n_fail} 条 fail",
    )


if __name__ == "__main__":
    sys.exit(main())
