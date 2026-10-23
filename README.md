# Roadbook Runner

**Roadbook Runner** —— 让 [roadbook](https://github.com/ouylzq/roadbook) 路书资产脱离 Claude Code / Codex，
作为独立程序无人值守跑，并带一个看板：每个路书任务一个触发按钮，每次跑批一段录屏和一个结论。

## 它是什么

一个 AI Native QA Agent：不生成 Playwright 脚本、不依赖固定 Selector，而是

* 理解业务目标（读路书的 intent）
* 在真实浏览器里按语义定位元素（可访问树 + 角色 + 文案，不是坐标）
* Observe → Act → Observe 短反馈循环推进
* 由**独立裁判**按验收路线取证判定，证据必须是页面原文
* 输出结构化报告 + 录屏
* 路书按单步结构化事件异步沉淀，业务执行不等待整份 Markdown 重写
* runner 不按固定轮数误杀，使用墙上时钟、页面停滞和人工停止三道安全闸

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
cp .env.example .env          # 填 LLM 地址/密钥/模型；按需配置 Vercel bypass

# 起 demo 靶子
python3 -m http.server 4173 --directory "${ROADBOOK_REPO:-../roadbook}/demo-app" &

.venv/bin/python run.py --list
.venv/bin/python run.py todo-complete
.venv/bin/python run.py serve                   # 打开 http://localhost:8900
```

## LLM

走 OpenAI 兼容协议，改 `.env` 三行即可换供应商（Claude 官方、任意 OpenAI 兼容网关都行）：

```
QA_AGENT_BASE_URL=https://api.anthropic.com/v1/
QA_AGENT_API_KEY=sk-ant-xxx
QA_AGENT_MODEL=claude-sonnet-5
```

## 目录

```
agents/     agent.md（唯一主 agent）+ judge.md（独立验证，需要干净上下文）
skills/     技能（roadbook 挂在这里）
frontend/   看板前端
runtime/    引擎：loop / session / context / events / traces
tools/      browser · recorder · validator
knowledge/  facts · experiences · reflections
storage/    跑批产物（录屏、截图、判定）
reports/    markdown 报告
```

路书资产来自开源的 [roadbook](https://github.com/ouylzq/roadbook) 仓库，默认通过 `ROADBOOK_REPO=../roadbook` 读取。

## 一个主 agent + 一个独立验证

只有 `agents/agent.md` 一个执行体，拿到全部工具（浏览器 + 读资源 + 跑脚本 + 写资产）。
"照路书走""自己探索""新建场景"这几种情形不是不同的角色，是代码在调用前拼好的
不同任务说明（`runtime/prompts.py`），模型不需要自己从提示词里判断"我现在该干嘛"。

`agents/judge.md` 是唯一保留独立的第二个 agent——它必须看不到执行过程、只信自己
读到的页面，这是上下文隔离的硬需求，不是角色分工，所以工具白名单里**没有**
click / type_text。

## 许可

MIT
