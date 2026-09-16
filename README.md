# NichyMochi · 奶茄团子 🍡

Nichy 交来文件，Mochi 接着运行。

## 启动监听

将 `nichy-mochi` 放在 GPU 能访问的代码目录，在项目内运行：

```bash
bash start.sh
```

`start.sh` 是服务启动入口。它启动常驻监听进程，任务结束后继续等待，直到服务停止或 GPU 容器结束。无需安装 Nichy；GPU 环境需要 Python 3.9+ 和 bash，任务依赖使用 GPU 已有环境。

默认接头点为 `/users/nichy/code/start`。路径不存在时会提示，并使用 `nichy-mochi/.nichy`。实际位置会在启动输出和 `log` 首行显示。CPU 提交端只需把文件传到 GPU 的这个位置；也可以通过已有共享目录写入。

## 交给 Mochi

先上传程序及其依赖文件，再在接头点的 `command.sh` 中写启动指令：

```bash
python -u train.py
```

`command.sh` 告诉 GPU 如何启动本次任务；相对路径从接头点的上一级开始。因此默认目录对应 `/users/nichy/code/train.py`，回退目录对应 `nichy-mochi/train.py`。业务启动脚本也可以写成 `bash train.sh`。

**最后上传或保存 `command.sh`，就是提交。** GPU 等待文件连续 2 秒不再变化，校验指令后自动排队执行，不需要 CPU 端运行提交命令。上传工具应先传临时文件，完成后改名为 `command.sh`，避免执行传到一半的内容。

```text
[2026-09-17 01:23:00+08:00] Nichy 提交了：command.sh · 第 12 次提交
[2026-09-17 01:23:00+08:00] Mochi 收到了：command.sh · 第 12 次提交
[2026-09-17 01:23:01+08:00] Mochi 输出：command.sh · 第 12 次提交
训练输出……
[2026-09-17 01:23:03+08:00] Mochi 完成了 ✓：command.sh · 第 12 次提交
```

新任务排队，当前任务继续；执行结束后 Mochi 继续监听。首次启动只初始化文件，不执行已有模板；启动就绪后再提交 `command.sh`。重启不会重跑已接收的文件。每次上传后等到回执再发下一次；需要同时保留多份代码时，分别放进版本目录，由 `command.sh` 指定路径。

“第 N 次提交”是 Mochi 登记的运行编号，重启后保留；同一份指令再次提交也会获得新编号。编号与完整校验值、任务目录的对应关系见 `submissions.tsv`，最新编号也会写入 `receipt.json`。校验值对应保存下来的启动指令，引用的外部代码和数据在执行时读取。

## 查看日志

在 GPU 上，将下面路径换成启动时显示的接头点：

```bash
tail -F /users/nichy/code/start/log
```

`tail -F` 持续显示新输出；`cat -n` 只查看当前内容并显示行号。事件时间包含日期、时间和时区偏移，采用 GPU 的本地时区；程序原始输出保持原样。搜索“第 12 次提交”即可找到同一次运行的记录。

| 文件 | 内容 |
|---|---|
| `log` | 接收回执、运行输出、完成或失败状态 |
| `status.txt` | 当前任务、提交编号和更新时间 |
| `receipt.json` | 最新提交编号、完整校验值与 GPU 读取回执 |
| `submissions.tsv` | 提交编号、指令、任务目录和完整校验值的索引 |
| `keep_alive.log` | 内置 GPU 心跳的状态变化 |
| `jobs/<ID>/run.log` | 每次任务的完整原始输出 |

心跳由同一个监听服务管理，空闲时运行，有任务时让出 GPU，无需另行启动。

高级配置与兼容命令见 [运行说明](docs/ADMIN.md)，验证范围见 [测试记录](docs/TESTING.md)。

MIT License.

贡献署名：Codex（OpenAI）协助完成交互设计、代码实现、GPU 隔离测试与文档；NichyMochi 由使用者持续反馈和完善。
