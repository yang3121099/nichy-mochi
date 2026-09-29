# NichyMochi · 奶茄团子 🍡

Nichy 交来文件，Mochi 长期运行。

## 启动监听

在 GPU 上的 `nichy-mochi` 项目目录运行：

```bash
bash start.sh
```

Mochi 启动后持续监听，任务结束后继续等待，无需重复启动。

默认接头点为 `/users/nichy/code/start`；不存在时会提示并使用 `nichy-mochi/.nichy`。实际位置显示在启动输出和 `log` 首行。CPU 端把文件上传到这个位置即可，也可以使用已有共享目录。

## 交给 Mochi

先上传程序及其依赖文件，再在接头点的 `command.sh` 中写启动指令：

```bash
python -u train.py
```

指令默认在接头点的上一级目录执行，也可以写成 `bash train.sh`。多卡训练可使用：

```bash
cd /users/nichy/code && torchrun --standalone --nnodes=1 --nproc-per-node=gpu main.py
```

**最后上传或保存 `command.sh`，就是提交。** Mochi 自动读取、排队并运行。上传时建议先传临时文件，完成后改名为 `command.sh`。

```text
[2026-09-18 14:23:00] 📮 Nichy 提交了：command.sh · 2026-09-18 14时 · 第 1 次提交
[2026-09-18 14:23:00] 🍡 Mochi 收到了：command.sh · 2026-09-18 14时 · 第 1 次提交
[2026-09-18 14:23:01] 📝 Mochi 输出：command.sh · 2026-09-18 14时 · 第 1 次提交
训练输出……
[2026-09-18 14:23:03] Mochi 完成了 ✓：command.sh · 2026-09-18 14时 · 第 1 次提交
```

启动就绪后再上传 `command.sh`，每次看到接收回执后再提交下一份。新任务排队，当前任务继续运行。

提交按北京时间的自然小时计数，如 `14时 · 第 1 次提交`、`14时 · 第 2 次提交`，日期一并保留，方便查找。

每次指令自动备份到 `backups/`，例如 `command_2026-09-18_14h_001.sh`，可直接查看或用 `diff` 比较。业务代码和数据请保留到任务结束。

## 查看日志

在 GPU 上，将下面路径换成启动时显示的接头点：

```bash
tail -F /users/nichy/code/start/log
```

`tail -F` 持续显示新输出；`cat -n` 查看已有内容和行号。日志使用北京时间，搜索提交编号即可找到对应记录。

| 文件 | 内容 |
|---|---|
| `log` | 接收回执、运行输出、完成或失败状态 |
| `status.txt` | 当前任务、提交编号和更新时间 |
| `progress.json` | 任务状态与已用时间 |
| `receipt.json` | 最新提交编号与读取回执 |
| `submissions.tsv` | 历史提交与备份索引 |
| `backups/` | 带日期、小时和序号的历史指令 |
| `keep_alive.log` | 内置 GPU 心跳的状态变化 |

内置 GPU 心跳，空闲时自动运行，任务到来时让行；状态查看 `keep_alive.log`。

更多用法见 [运行说明](docs/ADMIN.md)。

MIT License.

贡献署名：Codex协助完成交互设计、代码实现、GPU 隔离测试与文档；NichyMochi 由使用者持续反馈和完善。
