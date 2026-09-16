# NichyMochi · 奶茄团子 🍡

把文件交给 Mochi，让它在 GPU 机器上帮你运行。

老师设置好后，你只需要：

```bash
nichy run hello.py
```

```text
Mochi 记下了：hello.py · 版本 a31c29e4
Mochi 收到了：hello.py · 版本 a31c29e4
```

**“收到了”表示 GPU 已经读取并校验了这一版文件。** 如果机器暂时没连接，只会显示“等待机器读取”。终端关闭后，已提交的任务仍会保留。

## 平时怎么用

| 命令 | Mochi 会做什么 |
|---|---|
| `nichy run hello.py` | 运行这个文件 |
| `nichy` | 看当前任务、最新提交和内容版本 |
| `nichy log` | 看最新提交的运行输出和文件位置 |
| `nichy stop` | 停下当前任务，机器继续待命 |

修改文件后，再输入一次 `nichy run hello.py` 就好。Mochi 会保存这次提交的文件快照，给它一个版本号。正在运行的旧版本不会被修改；新版本会排队。

```text
Mochi 正在运行：hello.py · 版本 a31c29e4
最新提交 · Mochi 收到了：hello.py · 版本 b72d580f · 排队中
```

想一直看到结束，可以输入：

```bash
nichy run --wait hello.py
```

```text
Mochi 记下了：hello.py · 版本 a31c29e4
Mochi 收到了：hello.py · 版本 a31c29e4
Mochi 正在运行：hello.py · 版本 a31c29e4
Mochi 完成了 ✓：hello.py · 版本 a31c29e4
```

很快的任务可能直接显示完成。程序自己的输出用 `nichy log` 查看。按 Ctrl+C 只结束等待显示；要取消任务，使用 `nichy stop`。

也支持 shell 文件和程序参数：

```bash
nichy run start.sh
nichy run train.py --epochs 3
```

`nichy` / `nichy status` 查看当前任务和最新提交；`nichy log` 默认查看最新提交；`nichy stop` 优先停止正在运行的任务，没有运行任务时处理最新提交。需要指定某次任务时，老师可以使用任务 ID：`nichy log ID`、`nichy stop ID`。

## 老师的一次性设置

需要 Python 3.9+。GPU 执行端需要 Linux、bash；CUDA 心跳另外使用 GPU 环境中已有的 PyTorch 和 `nvidia-smi`。提交端不需要 PyTorch，也不需要 GPU。

**提交端和 GPU 必须能读写同一份 POSIX 共享目录，并使用相同的绝对路径。** 路径由你选择，`/mydir` 只是示例；Nichy 不会创建网络挂载、自动上传文件或替你申请 GPU。普通云盘同步目录不等同于支持原子操作的共享文件系统。

在两端各自的 Python 环境中安装命令：

```bash
git clone https://github.com/yang3121099/nichy-mochi.git
cd nichy-mochi
python3 -m pip install -e .
```

两端设置相同的队列位置。把下面的路径替换为自己实际可访问的共享目录，并保存到提交端的终端环境和 GPU 作业/服务环境中：

```bash
export NICHY_HOME="/你选择的共享目录/nichy-data"
mkdir -p "$NICHY_HOME"
```

未设置 `NICHY_HOME` 时，默认使用安装代码目录下的 `.nichy`。也可以临时指定：`nichy --home /共享目录/nichy-data run hello.py`。优先级是 `--home`、`NICHY_HOME`、默认目录。

把项目自带的 **`start.sh` 作为平台 GPU 作业的启动入口**，在 GPU 的 Python 环境中运行一次：

```bash
bash start.sh "$NICHY_HOME"
```

`start.sh` 在第一次启动时创建默认配置；已有配置会保留。也可以直接修改脚本开头的共享位置和首次生成配置的参数。它启动后持续等待，内部相当于 `nichy serve`。日常用户只需要前面的四个命令。服务部署、迁移和故障恢复见 [管理员说明](docs/ADMIN.md)。

如果已有平台启动脚本，在完成环境激活后，将下面一行作为最后一行（替换项目实际位置）：

```bash
exec bash /项目安装位置/nichy-mochi/start.sh "$NICHY_HOME"
```

如果 `start.sh` 是你某个训练项目的业务脚本，则在提交端用 `nichy run 训练项目/start.sh` 交给 Mochi。项目自带的启动脚本负责等待任务，训练项目的脚本负责执行一次任务。

## 心跳放在哪里

**放在共享目录的 `$NICHY_HOME/config.json`，GPU 只在启动时读取一次。** 不需要单独上传 keep_alive 脚本；它已经包含在安装包里。管理员在任一能访问共享目录的端修改配置，下次启动 Mochi 时生效；运行期间不会反复读取配置。

可选配置如下；不创建文件时也使用这些默认值：

```json
{
  "enabled": true,
  "interval": 1800,
  "seconds": 10,
  "idle_for": 30,
  "duty": 0.25
}
```

含义：先空闲 30 秒，随后最多每 30 分钟做一次、每次最多 10 秒的 CUDA 计算校验，目标计算占空比不超过 25%。关闭时把文件内容改成 `{"enabled": false}`，并在任务结束后重启 Mochi。启动时配置写错会暂停心跳，正式任务仍可运行；修正后重启生效。

配置只读一次；等待新任务的队列检查，以及心跳计算期间用于及时让出 GPU 的负载检查，会正常保留。

Mochi 只选择当前 GPU 分配中可见、无计算进程、利用率低于 20%、空闲显存超过 60% 的设备，连续两次检查通过后才启动。一次只使用其中一张卡，不扩大 `CUDA_VISIBLE_DEVICES`。心跳期间出现其他 GPU 计算进程或更高负载时会让出；队列中有正式任务时，先结束心跳并清理其子进程，再运行正式任务。

这是周期性的实际 CUDA 校验，**不保证固定利用率，也不保证平台不会回收实例**。队列内任务有清理完成后再启动的顺序保证；外部任务采用采样检测，退出会有检测和清理延迟。无 CUDA、设备映射不明或指标不可用时跳过心跳。

## 文件和版本

每次 `run` 会复制程序所在文件夹，作为本次代码快照，默认最多 100 MiB。请给程序一个小文件夹；模型、大数据和环境放在外部共享位置，通过路径引用。快照跳过队列自身以及 `.git`、`.venv`、`venv`、`__pycache__`、`.nichy`、`.mochi`、`outputs` 目录，不接受符号链接。

运行时的当前目录是快照目录，相对路径的输出也保存在那里。`nichy log` 会显示任务文件位置；可以通过环境变量 `NICHY_OUTPUT_DIR` 把结果统一保存到该任务的 `results` 文件夹。

版本号来自执行参数、运行脚本和整个快照的 SHA-256，显示前 8 位；完整值见 `nichy status --json`。修改外部数据文件不改变快照版本。每次提交都是一个独立任务，相同内容版本也会再次运行。`nichy --version` 查看的是工具版本。

## 测试

```bash
python3 tests/run.py
python3 tests/run.py --gpu
```

第一条在 macOS 测试提交和对话，在 Linux 额外测试进程清理、恢复和完整交互。第二条需在已分配且空闲的 Linux CUDA GPU 上运行，增加实际计算、心跳让出和显存归还测试。测试使用独立临时队列，不提交到正式队列。实测记录见 [测试报告](docs/TESTING.md)。

MIT License.
