# 运行说明

## 共享路径和部署

项目安装位置与接头点可以不同。默认查找 `/users/nichy/code/start`，不存在时提示并回退到代码目录的 `.nichy`。两端设置相同的 `NICHY_HOME`，指向同一份、同一路径的 POSIX 共享存储。启动 GPU worker 的环境应当已分配 GPU、具有所需 Python 依赖；提交端不需要复制 CUDA 环境。

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

## 启动时读取一次配置

GPU 只在启动时读取 `$NICHY_HOME/config.json`，`keep_alive.json` 记录本次启动应用的配置。可以只填写需要修改的字段，其余采用默认值。运行时不轮询配置文件；修改后重启服务或重新启动平台入口生效。

| 字段 | 默认 | 范围 / 含义 |
|---|---:|---|
| `enabled` | `true` | 布尔值；控制本次启动是否使用心跳 |
| `interval` | `1800` | 开始两次心跳的最小间隔，秒，至少 1 |
| `seconds` | `10` | 每次计算时长，秒，大于 0、不超过 120 |
| `idle_for` | `30` | 用户任务结束后至少连续空闲多久，秒 |
| `duty` | `0.25` | 目标计算占空比，0.01–0.25；不是保证的 GPU 利用率 |

推荐保存到临时文件后再原子替换，避免恰好启动的 GPU 读到编辑中的半份 JSON：

```bash
printf '%s\n' '{"enabled": false}' > "$NICHY_HOME/config.json.tmp"
mv "$NICHY_HOME/config.json.tmp" "$NICHY_HOME/config.json"
```

启动时格式不合法或出现未知字段，心跳暂停，状态为 `config-error`；正式任务继续。修正同一个文件后，等待正式任务完成并重启服务，即可恢复。删除配置文件后下次启动会恢复默认设置。`start.sh` 只初始化缺失的文件，从不覆盖已有配置。

已有平台入口可以在环境准备完成后用 `exec bash /项目安装位置/nichy-mochi/start.sh "$NICHY_HOME"` 接入。若使用 Supervisor，先等待当前正式任务结束，再 `supervisorctl restart nichy-mochi` 应用新配置；直接重启会中断正在运行的任务。

没有可见 CUDA 设备、没有 PyTorch、`nvidia-smi` 失败或映射不明确时，心跳跳过。探测失败最多约每分钟重试一次；指标读取失败后退让至少 30 秒。MIG 映射无法明确对应时也跳过。空闲检查的两次采样间隔至少 1 秒。

心跳使用一张已分配且可见的 GPU，约 48 MiB 矩阵张量，另有 PyTorch/CUDA 上下文开销。利用率高于 40% 时降低占空比；达到 80%、空闲显存低于 60% 或出现第二个 GPU 计算进程时退出。外部需求每约 0.5 秒检测一次，每次指标查询有超时；因此实际退出可能有数秒延迟。它不会终止外部进程。

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

`RUN` 和 `STOP` 约每 0.5 秒检查一次，属于 CPU 上的文件元数据检查，不使用 CUDA，也不重读心跳配置。RUN 根据信号文件身份生成固定请求 ID，先原子发布，再记下处理回执；中间退出后可识别已发布请求，避免重复执行。信号文件保留原位，新的 touch 不会被旧请求删除。不要手工清理 `.signals.json`、`.views.json` 或修改已发布任务。

RUN 是一个信号槽，不是事件总线；多个触发在一次轮询前合并时，只处理最终内容。`submit.sh` 为每次调用生成独立任务，适合自动化、连续及并发提交。新建 `command.sh` 的默认内容只输出问候，启动监听不会自动执行它。

`log` 会自动追加任务标识、读取/运行/结束状态及程序输出；每轮限制读取量以避免大量输出拖慢进程监督。断电恰逢日志追加与游标保存之间时，汇总日志可能重复部分内容；任务不会因此重跑，原始日志可用于核对。任务日志、汇总日志不自动删除，需按保留策略归档。

`submit.sh` 自动跟随本次任务输出，不会执行额外 CPU 平台作业。可用 `NICHY_CLIENT_PYTHON` 指定提交端 Python，`NICHY_PYTHON` 指定 GPU 启动 Python。心跳文件只在缺失时复制，已有自定义文件保持不变；更新它需要自行替换，并保持 `--probe`、`--uuid`、`--seconds`、`--duty` 接口。
