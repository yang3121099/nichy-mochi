# 运行说明

## 共享路径和部署

项目安装位置与接头点可以不同。默认查找 `/users/nichy/code/start`，不存在时提示并回退到代码目录的 `.nichy`。可用 `NICHY_HOME` 指定接头点，或用 `NICHY_CODE_DIR` 指定代码根目录（接头点为其 `start` 子目录）。提交端可上传文件到 GPU，也可写入已有共享目录。启动 GPU worker 的环境应当已分配 GPU、具有所需 Python 依赖；提交端不需要复制 CUDA 环境。

共享目录内容：

```text
nichy-data/
  command.sh / RUN / STOP  # 文件交互入口
  log / status.txt         # 自动汇总的输出与状态
  receipt.json             # 最新提交的读取回执
  keep_alive.py            # GPU 通过绝对路径调用的心跳程序
  config.json              # 启动时读取的心跳配置
  worker.json              # 机器状态、当前内容版本、更新时间
  keep_alive.json          # 已应用配置及心跳状态
  worker.lock/             # 唯一 worker 的锁及停止标志
  staging/                 # 未发布的提交，不会执行
  jobs/<ID>/
    request.json           # 执行参数、完整版本、快照清单
    received.json          # GPU 校验后的读取回执
    code/                  # 本次提交的代码快照及相对路径输出
    results/               # NICHY_OUTPUT_DIR
    status.json            # 当前任务状态
    run.log                # 程序输出
    attempt-*.json          # 每次执行结果
    guard-*.json            # 子进程清理完成证明
  pulses/                  # 内部心跳记录，最多保留约 20 次已结束记录
```

`bash start.sh "$NICHY_HOME"` 首次生成配置并启动 `nichy serve` 持续等待，不自动申请、续费、迁移或销毁 GPU。脚本使用当前 `python3`；如需指定 GPU 环境，可一次设置 `NICHY_PYTHON` 为该环境的 Python。建议交给平台已有的进程管理器。若使用已有的 Linux Supervisor，在项目目录、已配置 `NICHY_HOME` 的 GPU Python 环境中运行一次（可选；平台托管 start.sh 时无需使用 Supervisor）：

```bash
python3 tools/install_service.py
supervisorctl status nichy-mochi
```

该助手生成当前 Python 和所选路径对应的启动脚本，只添加 `nichy-mochi` 服务；已有同名文件时拒绝覆盖。它不配置网页或占用端口。日常提交不需要这些命令。

服务设为开机启动，异常退出后不自动重试。队列保留故障锁，避免不明确的旧作业被自动重跑。正常关机时先通知任务退出，再确认所有子进程清理。删除实例、容器重建或共享存储丢失不是程序能恢复的情况；是否持久化取决于实际存储挂载。

## 心跳

同一个监听服务管理 GPU 心跳；状态变化记录在接头点的 `keep_alive.log`，当前状态见 `keep_alive.json`。程序与配置为 `keep_alive.py` 和 `config.json`，配置只在服务启动时读取。

## 任务语义

提交先写入临时目录，内容和状态完整落盘后再原子发布。每次 `nichy run` 是独立任务；新提交排队，不会替换旧任务。默认运行超时为 24 小时，修改方式为 `nichy run --timeout 3600 hello.py`。`--wait` 等待显示也默认限一天，等待结束不会取消任务。

读取回执与内容版本绑定。即便另一个任务正在运行，worker 仍读取并校验新提交。真正执行前再次校验，防止读取后快照被改变。历史回执表示那次确实读取过；是否仍在线由独立的 worker 心跳判断，超过 30 秒未更新显示无法联系。文件系统延迟会影响回执和在线状态的及时性。

运行环境来自 GPU worker。提交端环境变量不透传到 GPU。任务中可用：

| 环境变量 | 内容 |
|---|---|
| `NICHY_JOB_ID` | 本次任务 ID |
| `NICHY_REVISION` | 完整内容版本 |
| `NICHY_OUTPUT_DIR` | 本次结果目录 |
| `NICHY_PYTHON` | GPU worker 使用的 Python |
| `GPUQ_CODE_DIR` | 本次代码快照目录 |

内部 shell 使用 `bash -e`，进程非零退出即失败。每次任务有独立监督进程；worker 被强制杀死时，监督进程通过管道断开清理任务，包括双重 fork 和 `setsid` 脱离会话的后代。运行时间上限也在监督进程中计时，worker 暂停时仍有效。下一任务只能在清理完成证明写入后启动。

这是同一可信账号下的任务与进程隔离，不是安全沙箱。任务拥有 worker 用户的文件和网络权限；不要把它作为互不信任的多用户执行服务。目录默认权限为 0700；多个 Unix 用户协作时需另行配置共享权限。

## 故障恢复

查看 `nichy status --json` 和服务日志。worker 异常退出会留下锁；**先确认旧 worker、监督进程和任务子进程全部停止**，再在安装目录执行：

```bash
python3 mochi_core.py --root "$NICHY_HOME" recover --confirm-old-worker-stopped
supervisorctl start nichy-mochi
```

遗留 `RUNNING` 会标记为 `UNKNOWN`，不会重跑；检查结果后再显式提交新任务。`PENDING` 继续保留。不要直接删除锁或按一个过期 PID 随意杀进程。跨机器恢复需要确认旧机器上的作业已结束。

正常暂停整个服务使用 `supervisorctl stop nichy-mochi`；这与日常 `nichy stop` 只取消当前任务不同。想让当前任务自然完成后停服务，可以执行 `python3 mochi_core.py --root "$NICHY_HOME" stop`。

## 保存结果

`nichy log` 输出任务目录；其中包含日志、状态、快照和程序生成的文件。任务历史不会自动删除。清理历史请只删除已确认结束且不再需要的任务目录，保留必要结果；不要清理运行中的任务、锁或未确认清理的心跳目录。

## 文件接口与日志

`command.sh` 每约 0.5 秒检查一次文件元数据，连续 2 秒稳定后保存指令快照并发布请求。这是 CPU 文件检查，不使用 GPU，也不重新加载心跳配置。稳定等待不能证明网络上传已完成，上传端应先传临时文件，再原子改名；程序和依赖先传，`command.sh` 最后传。

首次运行将已有 `command.sh` 记录为基线，不自动执行。此后服务停机期间更新的文件会在下次启动后处理。文件身份生成固定任务 ID；发布与记录回执之间退出时，重启能识别已发布请求，避免重跑。不要清理 `.signals.json`、`.views.json` 或修改已发布任务。

一个 `command.sh` 是单个提交槽；上传后等到接收回执再上传下一份，避免连续更新合并。回执和版本针对 shell 指令，所引用的业务文件没有自动冻结。并行准备多个版本时使用不同业务目录，直到任务结束都保留它们。

兼容入口保留：`RUN` 文件更新可立即提交当前指令；`STOP` 文件更新取消当前任务，空闲时取消最新任务。`bash submit.sh train.py` 则会保存小型程序目录快照并跟随输出。自动上传接口无需这些命令；命令接口仍适合批量提交。

`log` 自动追加接收、运行、结束状态与程序输出；每轮限制读取量以免大量输出拖慢进程监督。断电恰逢日志追加与游标保存之间时，汇总日志可能重复部分内容；原始 `jobs/<ID>/run.log` 可用于核对。日志不自动删除，按需归档。
