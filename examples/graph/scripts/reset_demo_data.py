"""配套 examples/graph/recipes/reset_demo_data.yaml 的清理脚本样例。

真实项目里这里应该是「重置隔离测试库」或「调删除接口」的确定性动作，
并且只针对隔离测试环境，绝不碰生产数据。示例只留一条可审计的痕迹。

约定（跟 runner 的调度器对齐）：
    退出码 0   → 清干净了，任务记 done
    退出码 75  → 暂时清不了（资源忙 / 冷却期），稍后自动重试
    其他非 0   → 真失败，进 leftover 报告交人处理（不影响 verdict）

实体 id 从命令行参数拿（配方里写 args: [context.project_id] 之类），
不要在脚本里自己猜——删错对象的代价比删不掉更大。
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "cleanup-audit.log"


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["(未指定实体)"]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp}\treset_demo_data\ttargets={','.join(targets)}\n")
    print(f"reset_demo_data: 已重置 {len(targets)} 个对象（{stamp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
