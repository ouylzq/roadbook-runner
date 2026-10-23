## 任务：独立取证判定「${scenario}」

被测站点：${origin}
验收路线里的相对路径（如 `/zh/onboarding?...`）拼在这个域名后面。**不要猜别的域名。**

**fact 逐字纪律（硬规矩）**：judgment 里每条 `fact` 必须是从 expect.yaml 复制粘贴的原文，
一个字都不能动——不加括号补充、不换标点风格、不"顺手润色"、不改写措辞。已经连续多次因为
裁判给判据加了「（或这类数字）」之类的插入语被记分器判 BLOCK，整场作废。你的判断写在
`result` 和 `evidence` 里；`fact` 只做复印机。

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
