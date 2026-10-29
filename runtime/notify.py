"""跑批结束推飞书（自定义机器人 webhook）。

PM 不该守着看板等结果——跑完一条推一条到群里。webhook 没配就什么都不发生；
推送失败只打一行日志，绝不影响跑批结论本身（收尾链里的其它钩子照常执行）。
"""
from __future__ import annotations

import json
import urllib.request

from .config import Settings
from .traces import Run


def notify_finished(settings: Settings, record: Run) -> None:
    if not settings.webhook_url:
        return
    outcome = str(record.verdict or record.status or "")
    duration = ""
    if record.started_at and record.finished_at:
        try:
            from datetime import datetime

            delta = (datetime.fromisoformat(record.finished_at)
                     - datetime.fromisoformat(record.started_at)).total_seconds()
            duration = f"，耗时 {int(delta // 60)} 分 {int(delta % 60)} 秒" if delta > 0 else ""
        except ValueError:
            pass
    tag = f"（仅重判，原跑批 {record.rejudge_of}）" if record.rejudge_of else ""
    text = (f"Roadbook 跑批结束：{record.scenario}{tag}"
            f"（{record.env or '默认环境'}）→ {outcome}{duration}\n"
            f"结论详情：http://127.0.0.1:8900 （看板 · {record.run_id}）")
    body = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(settings.webhook_url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:      # 通知是锦上添花，任何网络问题都不许炸掉收尾链
        print(f"[飞书通知] 发送失败：{type(e).__name__}: {e}")
