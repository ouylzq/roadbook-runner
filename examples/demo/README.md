# Roadbook Demo：用 Markdown 路书验证任务保存

[主 README 的演示流程](../../README.md#demo用一份-markdown-路书验证任务保存) 展示了场景文件的具体内容，以及从读路书到裁判取证的一次完整运行。

从仓库根目录启动演示，打开 [http://127.0.0.1:4173/](http://127.0.0.1:4173/)。

```sh
python examples/demo/demo.py
```

手动体验只需要 Python 3.12+ 和浏览器。导览页可以打开三份测试资产。按路书输入「核对发布清单」，提交后刷新页面，对照验收文件核对任务标题和数量。演示数据保存在本地服务的内存中，随服务重启清空。

| 版本 | 提交后 | 重新打开列表后 |
| --- | --- | --- |
| 正常保存 | 显示「已添加任务」 | 标题还在，已保存任务为 1 |
| 按钮改名 | 从「保存任务」入口完成同一动作 | 标题还在，已保存任务为 1 |
| 保存丢失 | 同样显示「已添加任务」 | 「还没有任务。」，已保存任务为 0 |

## 让 Runner 实际读取这份路书

在 Python 3.12+ 环境中安装项目依赖和 Chromium。

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
```

在仓库根目录的 `.env` 中配置支持工具调用的 OpenAI 兼容模型。把三项示例值换成你的服务配置。

```dotenv
QA_AGENT_BASE_URL=https://your-provider.example/v1
QA_AGENT_API_KEY=your-api-key
QA_AGENT_MODEL=your-model-id
```

先用 Ctrl+C 停止手动预览，释放端口。以下命令每次都会启动本地演示应用，通过 Runner 的正常入口完成执行、独立验收和判分。

```sh
python examples/demo/demo.py run
python examples/demo/demo.py run --variant renamed
python examples/demo/demo.py run --variant lost-write
```

- **正常保存**的预期是 `PASS`。裁判重新打开页面后仍能读到任务。
- **按钮改名**的预期也是 `PASS`。第 3 步找不到「添加任务」时，Agent 可以从表单原子查到改名后的按钮。成功提交后，查看 [playbook.md](skills/roadbook-demo/scenarios/save-task/playbook.md) 的 Git diff，看第 3 步的锚点和动作怎样变化。如需从原始文案重新演示，请将该步的锚点和动作改回「添加任务」。
- **保存丢失**的预期是 `FAIL`，命令退出码为 1。执行 Agent 看到了成功提示，裁判重新打开列表后会读到空列表。这时到 `storage/<run_id>/judgment.yaml` 看裁判引用的证据。

也可以运行 `python examples/demo/demo.py dashboard`，然后打开 [Runner 看板](http://127.0.0.1:8900/) 并运行「保存一条任务」。在该次运行详情中可以查看录屏和判定结果。加上 `--variant renamed` 或 `--variant lost-write` 可以在相应版本上跑看板。

## 从演示迁移到自己的场景

| 文件 | 换成你的场景时写什么 |
| --- | --- |
| [intent.md](skills/roadbook-demo/scenarios/save-task/intent.md) | 测什么、可操作的环境、停止点、需要留下的本次事实 |
| [playbook.md](skills/roadbook-demo/scenarios/save-task/playbook.md) | 操作目的、页面锚点、动作，以及怎么确认当前步骤已走到 |
| [任务列表页原子](skills/roadbook-demo/atoms/task-list-page.md) | 页面如何判断已加载、如何识别空状态和读取错误 |
| [新增任务表单原子](skills/roadbook-demo/atoms/task-create-form.md) | 控件长什么样、常见变体、已知问题及处理方法 |
| [expect.yaml](skills/roadbook-demo/scenarios/save-task/expect.yaml) | 裁判去哪儿重新读页面、哪些业务事实必须成立 |

演示自带裁判协议和计分脚本，源自 Roadbook 资产仓库的 [MIT 许可版本](https://github.com/ouylzq/roadbook/tree/f947da51b320119169bc9ba2257bf6e6d56fc0fd)。
