# NichyMochi · 奶茄团子 🍡

共享目录，就是和 Mochi 的秘密接头点。

GPU 上启动一个常驻监听进程；提交端只写入请求，不需要额外申请 CPU 作业，也不需要反复使用 `nichy` 查询。回执、状态和运行输出自动写回共享目录。

## 启动一次

将仓库放在两端可访问的共享目录。推荐位置是 `/users/nichy/code/start`；GPU 的平台启动命令为：

```bash
bash /users/nichy/code/start/start.sh
```

**无需 `pip install` Nichy。** 监听进程使用 Python 3.9+ 标准库，需要 Linux 和 bash。执行任务使用 GPU 现有的 Python 环境；CUDA 心跳另外使用已有的 PyTorch 和 `nvidia-smi`。没有可用 CUDA 环境时跳过心跳，队列仍可工作。无需配置端口、数据库或 Web 服务。

代码放在其他位置时，在代码目录运行 `bash start.sh` 即可。默认查找 `/users/nichy/code/start`；若不存在，Mochi 会提示并回退到**代码目录下的 `.nichy`**，不会擅自创建 `/users/nichy`。

可选覆盖：

```bash
bash start.sh /另一个共享目录
# 或在两端设置：
export NICHY_HOME=/另一个共享目录
# 或设置代码根目录，接头点为其 start 子目录：
export NICHY_CODE_DIR=/另一份代码目录
```

优先级：显式路径、`NICHY_HOME`、`NICHY_CODE_DIR/start`、默认路径、`.nichy` 回退。`python3 nichy.py home` 显示实际位置。两端需要访问**同一份、同一绝对路径**的 POSIX 共享文件系统；程序不负责挂载或同步。分别落到两台机器各自的 `.nichy` 不会互通。

## 交给 Mochi

在 `/users/nichy/code` 下：

```bash
bash start/submit.sh train.py --epochs 3
```

提交后直接显示回执和运行输出，直到任务结束：

```text
Mochi 记下了：train.py · 版本 a31c29e4
Mochi 收到了：train.py · 版本 a31c29e4
Mochi 正在运行：train.py · 版本 a31c29e4
Epoch 1/3 ...
Mochi 完成了 ✓：train.py · 版本 a31c29e4
```

“收到了”表示 GPU 已校验该版本。新任务排队，旧任务继续运行。Ctrl+C 只退出本地显示，已提交任务继续保留。支持 `.py` 和业务 `.sh` 文件。

`submit.sh` 只需要提交端的 Python 3，不依赖安装好的 `nichy` 命令。现有 `nichy run` 仍可使用；`nichy run --follow train.py` 同样显示实时输出。

## 只用文件也可以

在接头点的 `command.sh` 中写启动指令，例如：

```bash
python train.py --epochs 3
```

执行目录默认为接头点的上一级；可通过 `NICHY_CODE_DIR` 指定。`python` 使用监听进程的 Python 环境。

保存完成后，发一个信号：

```bash
touch /users/nichy/code/start/RUN
```

GPU 会校验 shell 语法，保存本次指令快照，排队执行。只编辑文件不会误触发运行。相同指令再次 `touch RUN` 也会创建新任务；已处理的信号在正常重启后不会重放。也可以直接 `bash start/submit.sh`，提交当前 `command.sh` 并显示输出。

停止当前任务：

```bash
touch /users/nichy/code/start/STOP
```

没有运行任务时，STOP 处理最新提交。监听进程继续待命。`RUN` 是单个信号槽，短时间内连续触发可能合并；需要连续或并发提交时使用 `submit.sh`。

## 看接头点就好

```bash
tail -F /users/nichy/code/start/log
cat /users/nichy/code/start/status.txt
```

| 文件 | 内容 |
|---|---|
| `log` | 持续追加的 Mochi 回执、任务输出和完成状态 |
| `status.txt` | 当前运行版本、最新提交、更新时间 |
| `receipt.json` | 最新任务的完整版本、读取回执和状态 |
| `command.sh`、`RUN`、`STOP` | 文件交互入口 |
| `keep_alive.py`、`config.json` | 心跳程序与启动配置 |
| `jobs/<ID>/` | 每次任务的原始日志、快照、状态和结果 |

这些文件自动维护，不需要手动找任务 ID。既有 `log` 会保留并追加。`log` 是便于阅读的汇总，可能稍滞后；原始 `jobs/<ID>/run.log` 保留完整输出。查看静态状态时请同时看更新时间。

## 心跳留在这里

首次启动将 `keep_alive.py` 和默认 `config.json` 放在实际接头点，不覆盖已有文件。GPU 通过**解析后的绝对路径**调用此心跳文件，使用自己已分配且可见的 GPU。

```json
{"enabled": true, "interval": 1800, "seconds": 10, "idle_for": 30, "duty": 0.25}
```

配置仅在启动时读取一次；修改后下次启动生效。默认空闲 30 秒后允许启动心跳，每次最多计算 10 秒，两次开始至少间隔 30 分钟。目标计算占空比为 25%，不是保证的 GPU 利用率。

GPU 有其他计算需求时让出；队列任务开始前，先清理心跳进程。心跳计算期间仍保留必要的负载检查。无 CUDA、指标不明或设备映射不明确时跳过；不保证平台不会回收实例。

## 快照与运行环境

`submit.sh train.py` 会快照程序所在文件夹，默认不超过 100 MiB；修改源文件不会改变已提交版本。模型和数据放在快照外，通过路径引用。`command.sh + RUN` 只快照 shell 指令，所引用的外部代码和数据以执行时内容为准；需要冻结代码版本时使用文件提交。

工具版本见 `python3 nichy.py --version`；内容版本显示 SHA-256 前 8 位。Python 任务的相对输出保存在任务快照目录，也可通过 `NICHY_OUTPUT_DIR` 写入独立结果目录。

服务托管、恢复和测试范围见 [运行说明](docs/ADMIN.md) 与 [测试记录](docs/TESTING.md)。这是可信账号内的执行工具，任务使用监听进程的权限，不是多租户安全沙箱。

MIT License.
