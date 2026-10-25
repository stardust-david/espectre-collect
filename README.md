# ESPectre 批量 CSI 采集脚本

在 ESP32（ESP32-C6）+ ESPectre 环境下，批量采集可用于神经网络训练的**原始 CSI 数据集**。

## 仓库结构（先看这个）

**这里只有一份核心代码，不是两套。** 跨平台逻辑全在 `collect_batch.py` 里，
macOS / Linux / Windows 用的是同一个文件；平台差异只体现在启动器上。

```
collect_batch.py    跨平台核心：自检、批量采集、元数据回填、质量校验
run_all.sh          macOS / Linux 一键入口
run_all.ps1         Windows 一键入口（PowerShell 主体）
run_all.bat         Windows 双击入口（调用 run_all.ps1）
```

| 平台 | 推荐入口 | 也可以直接调用 |
|---|---|---|
| macOS / Linux | `./run_all.sh` | `python3 collect_batch.py ...` |
| Windows | 双击 `run_all.bat` 或 `.\run_all.ps1` | `python collect_batch.py ...` |

## 这个脚本解决什么问题

你在 `espectre.dev/tools/monitor` 上看到的曲线是 **movement score（0–1 的检测分数）**，不是 CSI 波形。
原始 CSI 数据从来没有离开设备，它一直躺在设备的一个 HTTP 端点上：

```
GET http://<设备IP>:62587/espectre/v1/csi
```

但直接对接这个二进制端点要自己发外部流量、自己解析记录格式，比较繁琐。
官方 CLI 已经封装好了这条通路：

```
./espectre collect --label <标签> --duration <秒> --target <设备IP>
```

本脚本就是它的自动化外壳：**按标签批量重复采集 → 自动回填数据集元数据 → 自动跑质量校验**。
它不自造数据通路，所以产出的数据与官方的校验器和训练脚本完全兼容。

## 前置条件

1. **clone 官方仓库并建好虚拟环境**

   macOS / Linux：

   ```bash
   git clone https://github.com/francescopace/espectre.git ~/espectre
   cd ~/espectre
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

   Windows（PowerShell）：

   ```powershell
   git clone https://github.com/francescopace/espectre.git $HOME\espectre
   cd $HOME\espectre
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

2. **设备已联网**，与电脑处于同一局域网，能访问设备的 62587 端口。

3. **知道设备 IP**（不知道就先跑 `./espectre devices` 发现）。

## 快速开始

macOS / Linux：

```bash
# 1) 只做自检，不采集（强烈建议先跑这一步）
python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
    --room bedroom --self-check

# 2) 三种标签各采 10 条，每条 60 秒
python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
    --room bedroom --rounds 10 --duration 60

# 3) 先小规模试水：只采 empty，3 条，每条 30 秒
python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
    --room bedroom --labels empty --rounds 3 --duration 30
```

## 在 Windows 上运行

把 `collect_batch.py` 拷到那台 Windows 电脑上（任意目录，例如
`%USERPROFILE%\espectre-collect\`），在 PowerShell 里进入该目录执行。
Windows 上把 `python3` 换成 `python`，仓库路径换成 Windows 形式：

```powershell
# 自检
python collect_batch.py --repo $HOME\espectre --target 192.168.1.50 `
    --room bedroom --self-check

# 正式采集
python collect_batch.py --repo $HOME\espectre --target 192.168.1.50 `
    --room bedroom --rounds 10 --duration 60
```

注意 PowerShell 的续行符是反引号 `` ` ``，不是 `\`。在 cmd.exe 里则是 `^`，
或者干脆写成一行更省事。

### Windows 上脚本是怎么调用官方 CLI 的

官方仓库的 `espectre` 文件本身就是一个 Python 脚本（`#!/usr/bin/env
python3`），`espectre.cmd` 只是它在 Windows 上的包装壳。本脚本**不经过
`espectre.cmd`**，而是直接用虚拟环境的解释器执行 `espectre` 文件：

```
.venv\Scripts\python.exe  <repo>\espectre  collect --target ...
```

这样做的原因：`.cmd` 必须经 `cmd.exe` 启动，一旦仓库路径或参数里含空格，
引号解析极易出错；直接用 `python.exe` 执行则完全没有这个问题，也不依赖
PATH 里是否存在 `python` 或 `py`。脚本还会把输出编码强制为 UTF-8，
避免 Windows 控制台因中文报 `UnicodeEncodeError`。

### Windows 上的三个额外注意点

1. **防火墙**：首次运行时 Windows 可能弹出防火墙提示，需要允许 Python
   访问网络。采集本身是电脑主动出站（TCP 到设备 62587、UDP 到设备），
   一般不会被拦；一旦被拦，表现是“探测失败”或“一条都采不到”。

2. **不要用 WSL**：WSL 默认是 NAT 网络，通常发现不了局域网里的 ESP32。
   请用原生 Windows Python，或把 WSL 配成镜像网络模式。

3. **控制台建议用 Windows Terminal 或 PowerShell 7**：老版本 cmd.exe 对
   UTF-8 中文显示不友好，可能出现方块字。这只影响观感，不影响采集。

## 一键流程

`run_all.sh` / `run_all.ps1` 把「自检 → 采集 → 校验 → 训练」串成一条流程。

### macOS / Linux

```bash
./run_all.sh                                          # 全程交互式询问
./run_all.sh --self-check                             # 只自检
./run_all.sh -r ~/espectre -t 192.168.1.50 -m bedroom
```

### Windows

双击 `run_all.bat`，或在 PowerShell 里调用。`.bat` 内部用
`-ExecutionPolicy Bypass` 启动 PowerShell，所以你**不需要**改系统的执行策略：

```powershell
.\run_all.ps1                                          # 全程交互式询问
.\run_all.ps1 -SelfCheckOnly                           # 只自检
.\run_all.ps1 -Repo $HOME\espectre -Target 192.168.1.50 -Room bedroom
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `-Repo` | `$HOME\espectre` | 仓库根目录 |
| `-Target` | 询问 | 设备 IP |
| `-Room` | 询问 | 房间名 |
| `-Rounds` | 10 | 每标签条数 |
| `-Duration` | 60 | 每条秒数 |
| `-Pps` | 100 | 发包率；应与设备 `csi_target_pps` 一致，保持 100 |
| `-Role` | train | 写入 `dataset_role` |
| `-SelfCheckOnly` | - | 只自检，不采集 |
| `-NoTrain` | - | 采集后跳过训练 |
| `-ExportModel` | - | 训练直接走生产流程（通过门禁则导出权重） |

### 关于训练步骤的三个重要事实

**1. 训练需要 Python 3.14，并且要额外装 ML 依赖。**

```bash
pip install -r requirements-ml.txt
```

`collect_batch.py` 本身对 Python 版本要求不高，但训练脚本不行。

**2. 每条录音都必须有角色，否则质量校验直接失败。**

官方的质量校验器要求 `data/dataset_info.json` 里**每个条目都有
`dataset_role`**，缺失一律按 `exclude` 处理并导致校验不通过。
一键流程默认用 `-Role train` 采集，正好满足这个要求。

**3. 门禁大概率不会通过，这是正常的。**

官方晋升门禁要求：正常链路 recall > 95%、误报率 < 5%，低 RSSI 链路
recall > 90%、误报率 < 10%，并且需要 `selection` / `holdout` 角色的
配对录音（每个芯片都要有）。你如果只采了一个房间、只有 `train` 角色，
门禁基本不可能过，权重也就不会导出。

所以一键流程**默认走 `--no-export`**：只训练、只打印交叉验证指标，不碰
`ml_weights.h` / `ml_weights.py`。想真正导出时再选模式 2，或加
`-ExportModel`。这对「复现官方检测、先看指标」的目标是更合适的默认值。

### 两个 Windows 格式细节（已处理）

- `run_all.ps1` 保存为 **UTF-8 with BOM**。PowerShell 5.1 读取无 BOM 的
  UTF-8 文件会把中文显示成乱码，BOM 在这里是必需的。
- 两个文件都是 **CRLF 换行**。`.bat` 用 LF 会导致 `cmd.exe` 解析多行
  `if (...)` 块时行为异常。


## 三个标签的含义

| 标签 | 房间状态 | 模型状态 |
|---|---|---|
| `empty` | 无人，且保持安静 | IDLE |
| `static_presence` | 有人但基本静止 | IDLE |
| `motion` | 有人正常活动 | MOTION |

要点：**一条录音必须标签纯粹**，中途状态变了就作废。官方建议每个标签至少 10 条，每条 30–60 秒。

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--repo` | 必填 | ESPectre 仓库根目录 |
| `--target` | 必填 | 设备 IP / 主机名 / Direct 地址 |
| `--room` | 必填 | 房间名，写入 `dataset_info.json` 的 `environment` |
| `--rounds` | 10 | 每个标签采集条数 |
| `--duration` | 60 | 每条录音秒数 |
| `--start-delay` | 15 | `empty` 标签开始前的留空秒数，便于你离开房间；其他标签自动为 0 |
| `--pps` | 100 | 采集流量发包率；保持 100 以匹配设备时序 |
| `--detector` | lightweight | readiness 检查所用检测器 |
| `--ready-stable-seconds` | 官方默认 | 检测器保持 idle 多久才开录；`motion` 标签自动置 0 |
| `--labels` | 全部三个 | 逗号分隔，例如 `--labels empty,motion` |
| `--role` | exclude | 写入 `dataset_role`，可选 exclude/train/selection/holdout |
| `--frontend` | 无 | 传给官方 CLI，限定设备发现范围 |
| `--self-check` | - | 只自检，不采集 |
| `--skip-probe` | - | 跳过 HTTP 自检 |
| `--dry-run` | - | 只打印将执行的命令 |
| `--no-wait` | - | 标签切换时不等待人工确认 |
| `--no-validate` | - | 采集后不跑质量校验 |
| `--no-log` | - | 不写日志文件 |
| `--stop-on-error` | - | 任一录音失败即中止 |

## ESP32-C6 特别注意

1. **`wifi_raw` 在 C6 上完全不可用**（不是配置问题，是芯片限制）。
   如果你在 Monitor 的 “Wi-Fi traffic source” 里选了 `Wi-Fi raw`，那是无效设置。
   本脚本走的是 `external` 外部流量（UDP marker）通路，**不受这个限制影响**。

2. **C6 的捕获档位默认就是 `ht20`**，正好命中官方训练标准
   （官方模型只认 HT20 + HT-LTF + 64 子载波）。所以 C6 是可用且不妥协的。

3. **包率不要改，保持 `--pps 100`**。

   官方要求发送速率与设备的 `csi_target_pps`（默认 100）匹配，改速率会同时改变
   检测器时序。采集时留意 `collect` 输出的 **occupancy**：
   - 低于 85% 警告，低于 70% 直接 FAIL 并让 `collect` 以非零退出
   - **不要靠降低 `--pps` 来解决占用率**，那会让发送速率与设备期望不匹配，只会更差
   - 正确的排查顺序见「常见问题 → 占用率 FAIL」

## 脚本做了什么

```
[1/5] 检查仓库环境        确认 ./espectre、.venv 存在
[2/5] 设备自检            GET /health /device /capabilities，
                          拦掉 micro 前端和 features.csi=false 的组合
[3/5] 采集前目录快照      记录 data/dataset_info.json 里已有的录音文件名
[4/5] 批量采集            按标签循环调用 ./espectre collect
[5/5] 回填元数据          给新增录音写入 environment 和 dataset_role，
                          并把房间注册进 environments 列表（自动备份原文件）
后置：运行 tools/validate_dataset_quality.py
```

## 关键设计点

### 为什么 `motion` 标签要强制 `--ready-stable-seconds 0`

官方 `collect` 会在录制前等待「检测器就绪且保持 idle 若干秒」。
采集 `motion` 时人在持续走动，检测器**永远不会回到 idle**，录制就永远开始不了。
所以脚本对 `motion` 自动传 `--ready-stable-seconds 0` 禁用该检查。

### 为什么 `--start-delay` 只对 `empty` 生效

`empty` 要求人离开房间，需要一段缓冲时间；`static_presence` 和 `motion` 人本来就在房间里，
加延迟反而会污染录音。

### `dataset_role` 为什么默认 `exclude`

官方建议先以 `exclude` 采集、人工复查后再决定用途。
`exclude` 的录音训练时会被忽略，不会污染训练集。
复查无误后，可以：
- 手工改 `data/dataset_info.json` 里对应条目的 `dataset_role`，或
- 用 `--role train` 重跑（会影响本次新增的录音）

## 采集完成后

```bash
cd ~/espectre
source .venv/bin/activate

./espectre collect --info                    # 按房间列表查看已采录音
python tools/validate_dataset_quality.py     # 质量校验（占用率门）
python tools/train_ml_model.py --info        # 查看训练会用到哪些录音
python tools/train_ml_model.py               # 训练并导出权重
```

训练出的权重会导出到：

- `src/cpp/core/ml_weights.h`（固件用）
- `tools/lib/ml_weights.py`（Python 用）

## 常见问题

**`collect` 报占用率 FAIL，保存失败（退出码非零）**

占用率是「检测器窗口被有效输入填充的比例」，低于 70% 就会 FAIL。
注意：**失败的文件会被保留下来用于诊断，不会自动删除**，所以数据通常还在磁盘上。

按官方顺序排查，**不要改 `--pps`**：

1. **先关蓝牙**（手机、耳机、ESPHome 蓝牙代理）。BLE 和 Wi-Fi 共用射频，
   会把 CSI 包挤成突发，占用率直接掉 —— 这是最常见的原因
2. 检查设备与路由器之间是否有防火墙限速或拦截
3. 如果是 mesh 网络，把设备固定到某一个 AP，避免漫游
4. 重新检查设备摆放位置
5. 换 traffic source 对比（`ping` / `dns` / `dns_tcp`）

**探测返回 403**
固件只放行列出的浏览器 Origin。脚本已经带上官方 CLI 用的
`Origin: https://test.espectre.dev`；如果你的固件改过策略，需要自行调整。

**探测失败但设备能用**
可能是 mDNS/局域网发现受限。加 `--skip-probe` 让官方 CLI 自己去连。

**`alternatives`：如果固件是 Micro-ESPectre**
`micro` 前端不提供 `/csi` 端点，脚本会在自检阶段直接拦下。
需要重刷成 Native 前端固件。

## 许可

本仓库的脚本采用 **MIT**（见 `LICENSE`）。

它**不包含也不修改** ESPectre，只是在独立进程里调用它的命令行，等价于手工敲同样的命令。

但注意：**ESPectre 本身是 GPLv3**（另有商业许可）。日常采集与训练属于正常使用；
若将来要闭源商用量产固件，需要走它的商业许可。
