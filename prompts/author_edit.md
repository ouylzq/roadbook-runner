## 任务：按描述修改场景「${scenario}」

有人想改这个场景：
---
${ask}
---

现在的 intent.md：
---
${intent}
---

现在的 expect.yaml：
---
${expect}
---

在这基础上改，不要重写无关的部分。改完在 finish 的 notes 里说清你动了哪几处、为什么。

改的时候守住这条：**验收路线里的 `context.<键名>` 和 intent.md「留给裁判的事实」里的键
必须一一对应**——你加了一个 `context.x` 就要在 intent 里补一行 `x:`，删了引用就要把
intent 里那行一起删掉。写死的地址、固定文案直接写进路线，不要为它们造变量；
只有「跑的时候才知道」的值才开变量。用户不会知道有 context 这回事，这根线是你的活。

${intent_contract}
