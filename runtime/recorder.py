"""留档：录屏、截图、封面。

刻意跟判定分开——这里产出的东西**只给人看**，不进证据链。
roadbook judge-protocol 的规矩：截图不可复核，不算证据。
"""
from __future__ import annotations

from pathlib import Path


def video_of(run_dir: Path, which: str = "runner") -> Path | None:
    v = sorted((run_dir / which / "video").glob("*.webm"))
    return v[0] if v else None


def poster_of(run_dir: Path, which: str = "runner") -> Path | None:
    """最后一张截图当录屏封面——静止时也能一眼看出跑到哪了。"""
    s = sorted((run_dir / which / "shots").glob("*.png"))
    return s[-1] if s else None


def shots_of(run_dir: Path, which: str = "runner") -> list[Path]:
    return sorted((run_dir / which / "shots").glob("*.png"))
