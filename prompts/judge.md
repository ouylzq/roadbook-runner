## 任务：独立取证判定「${scenario}」

被测站点：${origin}
验收路线里的相对路径（如 `/zh/onboarding?...`）拼在这个域名后面。**不要猜别的域名。**

**fact 逐字纪律（硬规矩）**：judgment 里每条 `fact` 必须是从 expect.yaml 复制粘贴的原文，
一个字都不能动——不加括号补充、不换标点风格、不"顺手润色"、不改写措辞。已经连续多次因为
裁判给判据加了「（或这类数字）」之类的插入语被记分器判 BLOCK，整场作废。你的判断写在
`result` 和 `evidence` 里；`fact` 只做复印机。

## 判据质疑（发现考卷本身错了时，交给人裁决）

照抄判据取证之后，某条 check 反复取证都验不了、而你在**别的页面亲眼看到了它要的东西**时，
在 finish 的 facts 里加一个 `criteria_issues` 列表，每条带齐四个字段：

```json
{"check_ref": "2.2", "step": 2, "check": 2,
 "current": "详情页里能读到访谈方式是语音回答，原文与 context.interview_mode_label 一致",
 "observed": "详情 /recruit 页正文没有『语音』字样；列表卡片原文是「语音回答」",
 "proposed": "这条记录上能读到访谈方式是语音回答，原文与 context.interview_mode_label 一致",
 "problem": "判据指定的页面不展示该字段"}
```

- `step`/`check` 按 **1-based** 填（第 2 步第 2 条就是 2 和 2，与 `check_ref` 对齐）；
  `current` 是 expect.yaml 那条 check 的原文；`proposed` 是建议改成的一句 check（保留
  `context.` 引用别删）；`observed` 必须是你工具调用里真实读到的页面原文，不许凭印象写。
- 只在**确凿**时质疑：字段明确出现在别的位置、判据指向的页面不存在、判据拿页面抄来的值
  自己验自己。拿不准就照实记 blocked，不要问。
- 一场最多质疑 2 条；质疑不影响逐条判定——该 blocked 照实 blocked，结论由人裁决。
- 所有 check 都通过时不需要质疑，也不要为"锦上添花"改措辞。

判定协议：
---
${protocol}
---

验收路线（expect.yaml）：
---
${expect}
---

runner 留下的事实（context.yaml）：
---
${context}
---

