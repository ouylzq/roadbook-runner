## 任务：把这段描述写成新场景资产

有人想加一个新场景：
---
${ask}
---
${url_hint}现有场景有：${existing}

只写 intent.md 和 expect.yaml —— 不要写 playbook.md，那是探索模式跑出来的产物，
不是猜出来的。

${intent_contract}

expect.yaml 的写法照现有场景来：用 read_resource 读一个现有场景的 expect.yaml 参考。

expect.yaml 每条 check 必须「验的是业务结果，不是点击动作」，而且「能从页面文本上
读出来」——不确定能不能读到，就换一个能读到的等价说法。

两份文件都写完后，调用 `finish(outcome=done, summary=一句话说你建了什么场景、
覆盖了哪些验收点)` 收口——不收口的话任务会被判为未完成。
