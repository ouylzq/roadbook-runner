facts 的键就是 context.yaml 的顶层键，不要再往外套一层。intent.md 写
「task_titles: [...]」，你就直接给 {"task_titles": [...]}，不要给
{"context_yaml": {"task_titles": [...]}} —— 判分脚本按顶层键找，套了就判 BLOCK。
