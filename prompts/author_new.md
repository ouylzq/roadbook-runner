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
好样板是 `scenarios/gov-policy-doc/`——它同时示范了「写死的值直接写进路线」和
「跑的时候才知道的值才开 context 变量」这两种情形，照它写。

**你要负责把 intent.md 和 expect.yaml 拧在一起**：expect.yaml 里每出现一个 `context.<键名>`，
intent.md 的「留给裁判的事实」里就必须有同名的一行，两边逐字一致。用户只会说大白话、
不会知道有 context 这回事——这根线是你的活，不是留给用户补的。

expect.yaml 每条 check 必须「验的是业务结果，不是点击动作」，而且「能从页面文本上
读出来」——不确定能不能读到，就换一个能读到的等价说法。
