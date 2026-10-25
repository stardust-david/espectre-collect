#!/usr/bin/env bash
#
# ESPectre 一键流程（macOS / Linux）
#
# 与 run_all.ps1 功能对齐：设备自检 -> 批量采集 -> 质量校验 -> 模型训练。
# 真正执行采集的是同目录的 collect_batch.py，本脚本只负责参数收集与流程编排。
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR="$SCRIPT_DIR/collect_batch.py"

REPO=""
TARGET=""
ROOM=""
ROUNDS=10
DURATION=60
PPS=100
ROLE="train"
SELF_CHECK_ONLY=0
NO_TRAIN=0
EXPORT=0

ans=""
TRAIN_RC=0
RC=0

usage() {
    cat <<'EOT'
ESPectre 一键流程（macOS / Linux）

用法:
  ./run_all.sh [选项]

选项:
  -r, --repo <路径>     ESPectre 仓库根目录（含 espectre 文件）
  -t, --target <IP>     设备 IP
  -m, --room <名称>     房间名
  -n, --rounds <N>      每个标签采集条数（默认 10）
  -d, --duration <秒>   每条录音秒数（默认 60）
  -p, --pps <N>         发包率（默认 100，应与设备 csi_target_pps 一致）
      --role <角色>     exclude|train|selection|holdout（默认 train）
      --self-check      只自检，不采集
      --no-train        采集后跳过训练
      --export          训练时走生产流程并导出权重
  -h, --help            显示本帮助

示例:
  ./run_all.sh -r ~/espectre -t 192.168.1.50 -m bedroom
  ./run_all.sh --self-check -r ~/espectre -t 192.168.1.50 -m bedroom
EOT
}

rule()  { printf '%s\n' "------------------------------------------------------------"; }
step()  { printf '\n'; rule; printf '%s\n' "$1"; rule; }
ok()    { printf '  [ok] %s\n' "$1"; }
note()  { printf '  [!]  %s\n' "$1"; }
bad()   { printf '  [x]  %s\n' "$1" >&2; }
head2() { printf '\n%s\n%s\n%s\n' "============================================================" "$1" "============================================================"; }

confirm() {
    local prompt="$1" answer=""
    read -r -p "$prompt " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--repo)     REPO="${2:-}"; shift 2 ;;
        -t|--target)   TARGET="${2:-}"; shift 2 ;;
        -m|--room)     ROOM="${2:-}"; shift 2 ;;
        -n|--rounds)   ROUNDS="${2:-10}"; shift 2 ;;
        -d|--duration) DURATION="${2:-60}"; shift 2 ;;
        -p|--pps)      PPS="${2:-100}"; shift 2 ;;
        --role)        ROLE="${2:-train}"; shift 2 ;;
        --self-check)  SELF_CHECK_ONLY=1; shift ;;
        --no-train)    NO_TRAIN=1; shift ;;
        --export)      EXPORT=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) bad "未知参数: $1"; usage; exit 2 ;;
    esac
done

head2 "ESPectre 一键流程：自检 -> 采集 -> 校验 -> 训练"

if [[ ! -f "$COLLECTOR" ]]; then
    bad "找不到 collect_batch.py: $COLLECTOR"
    bad "请把 run_all.sh 和 collect_batch.py 放在同一目录。"
    exit 2
fi
ok "采集脚本: $COLLECTOR"

# ---- 仓库 ----
if [[ -z "$REPO" ]]; then
    default_repo="$HOME/espectre"
    read -r -p "ESPectre 仓库路径 [$default_repo]: " ans
    REPO="${ans:-$default_repo}"
fi
REPO="${REPO%/}"
if [[ ! -d "$REPO" ]]; then
    bad "仓库路径不存在: $REPO"
    exit 2
fi

# ---- 设备 / 房间 ----
if [[ -z "$TARGET" ]]; then
    read -r -p "ESP32 的 IP 地址: " TARGET
fi
if [[ -z "$TARGET" ]]; then
    bad "必须提供设备 IP。"
    exit 2
fi

if [[ -z "$ROOM" ]]; then
    read -r -p "房间名（例如 bedroom）: " ROOM
fi
[[ -z "$ROOM" ]] && ROOM="room"

# ---- Python ----
if [[ -x "$REPO/.venv/bin/python" ]]; then
    PY="$REPO/.venv/bin/python"
elif [[ -x "$REPO/.venv/Scripts/python.exe" ]]; then
    PY="$REPO/.venv/Scripts/python.exe"
else
    PY="python3"
fi

if ! "$PY" -c 'import sys' >/dev/null 2>&1; then
    bad "无法运行 Python: $PY"
    bad "请先安装 Python，或在仓库下建好 .venv。"
    exit 2
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python: $PY ($PYVER)"

EST_MIN=$(( (ROUNDS * 3 * (DURATION + 25) + 59) / 60 ))

# ---- 摘要 ----
step "运行参数"
printf '  仓库       : %s\n' "$REPO"
printf '  Python     : %s  (%s)\n' "$PY" "$PYVER"
printf '  设备       : %s\n' "$TARGET"
printf '  房间       : %s\n' "$ROOM"
printf '  每条时长   : %s 秒\n' "$DURATION"
printf '  每标签条数 : %s\n' "$ROUNDS"
printf '  发包率     : %s pps\n' "$PPS"
printf '  元数据角色 : %s\n' "$ROLE"
printf '  预计耗时   : 约 %s 分钟\n' "$EST_MIN"

confirm "确认开始自检吗？(Y/n)" || { echo "已取消。"; exit 0; }

# ---- 1. 自检 ----
step "[1/3] 设备自检"
"$PY" "$COLLECTOR" --repo "$REPO" --target "$TARGET" --room "$ROOM" --self-check
RC=$?
if [[ $RC -ne 0 ]]; then
    bad "自检未通过（退出码 $RC）。"
    if [[ $RC -eq 3 ]]; then
        bad "设备不可达或不满足采集条件，请检查："
        bad "  - IP 是否写错，电脑与设备是否同一局域网（AP 隔离）"
        bad "  - 防火墙是否拦截"
        bad "  - 固件是否为 micro 前端（它不提供 /csi）"
    fi
    exit $RC
fi
ok "自检通过。"

if [[ $SELF_CHECK_ONLY -eq 1 ]]; then
    ok "自检模式结束（--self-check）。"
    exit 0
fi

# ---- 2. 采集 ----
step "[2/3] 批量采集"
note "接下来按标签依次采集，每条之间需要你按回车确认。"
note "empty 需要人离开房间；motion 需要人在房间里走动。"
note "采集期间 Monitor 页面的运动曲线会暂停，这是官方设计。"
confirm "确认开始采集吗？(Y/n)" || { echo "已取消。"; exit 0; }

"$PY" "$COLLECTOR" --repo "$REPO" --target "$TARGET" --room "$ROOM" \
    --rounds "$ROUNDS" --duration "$DURATION" --pps "$PPS" --role "$ROLE"
RC=$?

case $RC in
    0) ok "采集与质量校验全部通过。" ;;
    4) note "有录音未通过质量校验（占用率低于 70%）。失败文件已保留用于诊断。"
       note "不要靠降低 --pps 解决；优先检查蓝牙干扰、设备位置、mesh 漫游。" ;;
    1) note "采集过程中有部分录音失败，请查看上面的日志。" ;;
    *) note "采集流程返回退出码 $RC。" ;;
esac

if [[ $NO_TRAIN -eq 1 ]]; then
    ok "已跳过训练（--no-train）。"
    exit $RC
fi

# ---- 3. 训练 ----
step "[3/3] 训练模型"

if ( cd "$REPO" && "$PY" -c 'import sklearn, numpy, scipy' >/dev/null 2>&1 ); then
    ok "训练依赖已就绪。"
else
    note "训练依赖尚未安装。请先执行："
    printf '      %s -m pip install -r requirements-ml.txt\n' "$PY"
    note "训练需要 Python 3.14。"
    confirm "仍然尝试运行训练吗？(y/N)" || { echo "已跳过训练。"; exit $RC; }
fi

printf '\n  $ %s tools/train_ml_model.py --info\n' "$PY"
( cd "$REPO" && "$PY" tools/train_ml_model.py --info )
note "只有 dataset_role = train 的录音会参与拟合。本次新增录音角色：$ROLE"

if [[ $EXPORT -eq 1 ]]; then
    MODE=2
else
    printf '\n  [1] 只训练看指标，不覆盖固件权重（推荐先跑这个）\n'
    printf '  [2] 生产训练：通过门禁则导出权重\n'
    read -r -p "选择 (回车默认 1): " MODE
    MODE="${MODE:-1}"
fi

if [[ "$MODE" == "2" ]]; then
    note "生产训练。门禁不通过时不会导出权重。"
    ( cd "$REPO" && "$PY" tools/train_ml_model.py --augment )
    TRAIN_RC=$?
else
    note "只训练不导出，不会改动固件权重文件。"
    ( cd "$REPO" && "$PY" tools/train_ml_model.py --augment --no-export )
    TRAIN_RC=$?
fi

head2 "流程结束"
printf '  采集退出码 : %s\n' "$RC"
printf '  训练退出码 : %s\n' "$TRAIN_RC"
printf '  数据位置   : %s/data/<标签>/\n' "$REPO"
printf '  数据目录   : %s/data/dataset_info.json\n' "$REPO"
printf '  采集日志   : %s/logs/\n' "$SCRIPT_DIR"
printf '\n  后续常用命令（在仓库目录下执行）：\n'
printf '    python3 tools/validate_dataset_quality.py\n'
printf '    python3 tools/train_ml_model.py --info\n'
printf '    python3 tools/train_ml_model.py --augment\n'
exit 0
