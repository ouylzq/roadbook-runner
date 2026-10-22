#!/usr/bin/env python3
"""qa-agent —— 路书独立运行时

  qa check               跑之前自检（环境 / 模型服务 / 靶子 / 浏览器 / 上次产物）
  qa list                列出技能和场景（带最近几次结论）
  qa run <场景>          跑一个场景        [--headed 显示浏览器] [--skill roadbook]
  qa runs                最近的跑批列表
  qa show <run_id>       看某次跑批：结论、产物路径、报告
  qa serve               起看板            [--port 8900]
  qa metrics             跑批度量：轮数 / token / 缓存 / 是否自己收口  [--scenario X]

退出码沿用 roadbook judge.py：0=PASS 1=FAIL 2=BLOCK
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

from runtime.app import QaAgent
from runtime.doctor import Doctor
from runtime.domain import RunStatus

EXIT_CODES = {RunStatus.PASSED: 0, RunStatus.FAILED: 1, RunStatus.BLOCKED: 2, RunStatus.ERROR: 2}
ARGS_PREVIEW = 66


def cmd_check(core: QaAgent, args: argparse.Namespace) -> int:
    return 0 if Doctor(core.settings).run_all(args.target) else 2


def cmd_list(core: QaAgent, _: argparse.Namespace) -> int:
    for skill in core.skills.list_all():
        print(f"\n技能 {skill.name}  ({len(skill.resources())} 份资源)")
        print(f"  {skill.description[:80]}\n")
        for sc in skill.scenarios():
            history = " ".join(x.get("verdict") or x["status"] for x in core.knowledge.trend(sc.name, 5))
            print(f"  {sc.name:<22} [{sc.mode.value:<8}] {history or '还没跑过'}")
    return 0


def cmd_run(core: QaAgent, args: argparse.Namespace) -> int:
    skill = core.skill(args.skill)
    print(f"▶ {args.scenario}（技能 {skill.name}）\n")

    def on_event(kind: str, payload: Any) -> None:
        if kind in ("step", "judge_step"):
            prefix = "裁 " if kind == "judge_step" else ""
            print(f"  {prefix}{payload['n']:>2}. {payload['tool']:<13} {str(payload.get('args', ''))[:ARGS_PREVIEW]}")
        elif kind == "phase":
            print(f"\n  ── {payload}\n")

    record = core.run(skill, args.scenario, headless=not args.headed, emit=on_event)
    print(f"\n{'=' * 66}\n{record.verdict or record.status}  {record.summary}\n")
    if record.error:
        print(f"错误: {record.error}\n")
    _print_artifacts(core, record.run_id)
    return EXIT_CODES.get(record.status, 2)


def cmd_runs(core: QaAgent, args: argparse.Namespace) -> int:
    records = core.records(args.limit)
    if not records:
        print("还没有跑批记录")
        return 0
    for r in records:
        print(f"  {(r.verdict or r.status):<6} {r.run_id:<34} 执行{len(r.steps):>2}步 "
              f"裁判{len(r.judge_steps):>2}步  {r.finished_at[11:19]}")
    return 0


def cmd_metrics(core: QaAgent, args: argparse.Namespace) -> int:
    """把跑批换算成能横向比的几个数。不是对照实验——是把人肉考古变成一条命令。"""
    from runtime.metrics import measure, summarize

    records = [r for r in core.records(args.limit)
               if not args.scenario or r.scenario == args.scenario]
    rows = [measure(r, core.llm_calls(r.run_id)) for r in records]
    rows = [m for m in rows if m.runner_turns or m.runner_steps]      # 没跑起来的空壳不计
    if not rows:
        print("没有可度量的跑批")
        return 0

    print(f"  {'结论':<6} {'场景':<32} {'轮':>3} {'步':>3} {'收口':<14} {'路书':>7} "
          f"{'prompt':>9} {'等效':>9} {'省':>5} {'秒':>5}")
    for m in rows:
        pb = f"{m.playbook_before}→{m.playbook_after}" if (m.playbook_before or m.playbook_after) else "—"
        print(f"  {(m.verdict or m.status):<6} {m.scenario[:30]:<32} {m.runner_turns:>3} {m.runner_steps:>3} "
              f"{(m.runner_outcome or '—'):<14} {pb:>7} {m.prompt_tokens:>9,} {m.effective_tokens:>9,} "
              f"{m.cache_saving:>4.0%} {m.wall_seconds:>5}")

    s = summarize(rows)
    landed = (f"自己收口 {s['landed_rate']:.0%}（{s['landed_known']}/{s['runs']} 次有记录）"
              if s["landed_rate"] is not None else "自己收口：无记录（这个字段 2026-09-23 才加）")
    print(f"\n  {s['runs']} 次跑批 · PASS {s['pass_rate']:.0%} · {landed}")
    print(f"  中位数：{s['median_runner_turns']} 轮 · prompt {s['median_prompt']:,} "
          f"· 等效 {s['median_effective']:,} · {s['median_wall_seconds']} 秒")
    print(f"  这批一共往前勘进了 {s['surveyed_steps']} 步路书"
          "（「路书」列是 开跑时→收尾时 的步数；历史跑批没记这个字段，显示 —）")
    if not any(m.cache_read for m in rows):
        print("  ⚠️ 这批跑批没有任何缓存命中——提示词缓存是 2026-09-23 才打开的，之前的都是原价")
    return 0


def cmd_show(core: QaAgent, args: argparse.Namespace) -> int:
    record = core.record(args.run_id)
    if record is None:
        print(f"没有这次跑批: {args.run_id}")
        return 2
    print(f"\n{record.verdict or record.status}  {record.scenario}  ({record.run_id})")
    print(f"\n结论\n  {record.summary}\n")
    if record.notes:
        print(f"执行备注\n  {record.notes[:600]}\n")
    if record.judge_output:
        print("判分明细")
        for line in record.judge_output.strip().splitlines():
            print(f"  {line}")
        print()
    _print_artifacts(core, args.run_id)
    if args.open:
        videos = sorted((core.run_dir(args.run_id) / "runner" / "video").glob("*.webm"))
        if videos:
            subprocess.run(["open", str(videos[0])], check=False)
            print(f"\n已用系统播放器打开 {videos[0].name}")
    return 0


def cmd_serve(_: QaAgent, args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("runtime.server:app", host="127.0.0.1", port=args.port)
    return 0


def _print_artifacts(core: QaAgent, run_id: str) -> None:
    run_dir = core.run_dir(run_id)
    root = core.settings.root
    print("产物")
    for role in ("runner", "judge"):
        for video in sorted((run_dir / role / "video").glob("*.webm")):
            print(f"  录屏({role})  {video.relative_to(root)}  {video.stat().st_size // 1024} KB")
        shots = len(list((run_dir / role / "shots").glob("*.png")))
        if shots:
            print(f"  截图({role})  {shots} 张  storage/{run_id}/{role}/shots/")
    for name in ("context.yaml", "judgment.yaml", "run.json"):
        if (run_dir / name).exists():
            print(f"  {name:<14} storage/{run_id}/{name}")
    report = core.reports.path_for(run_id)
    if report.exists():
        print(f"  报告           reports/{report.name}")
    print("\n  看板里打开：http://localhost:8900  →  该卡片「详情」")


def build_parser(default_target: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("check", help="跑之前自检")
    p.add_argument("--target", default=default_target)
    p.set_defaults(fn=cmd_check)

    sub.add_parser("list", help="列出技能和场景").set_defaults(fn=cmd_list)

    p = sub.add_parser("run", help="跑一个场景")
    p.add_argument("scenario")
    p.add_argument("--skill", default=None)
    p.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("runs", help="最近的跑批")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("metrics", help="跑批度量")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--scenario", default="")
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("show", help="看某次跑批")
    p.add_argument("run_id")
    p.add_argument("--open", action="store_true", help="顺便用系统播放器打开录屏")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("serve", help="起看板")
    p.add_argument("--port", type=int, default=8900)
    p.set_defaults(fn=cmd_serve)
    return parser


def main() -> int:
    core = QaAgent()
    parser = build_parser(core.settings.default_target_url)
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 0
    return int(args.fn(core, args))


if __name__ == "__main__":
    sys.exit(main())
