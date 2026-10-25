#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESPectre 批量 CSI 采集自动化脚本（macOS / Linux / Windows）

用途
----
在 ESP32（含 ESP32-C6）+ ESPectre 固件环境下，批量采集可用于神经网络训练的
原始 CSI 数据集。

本脚本是官方 CLI `./espectre collect` 的自动化外壳，不自造数据通路，因此产出
的数据与官方数据集格式、校验器、训练脚本完全兼容。

它解决四件事
------------
1. 采集前自检：确认设备 frontend / chip / features.csi，提前拦掉不可用组合。
2. 批量采集：按标签（empty / static_presence / motion）重复采集 N 条。
3. 回填元数据：自动把 environment 与 dataset_role 写回 data/dataset_info.json。
4. 质量校验：自动调用官方 tools/validate_dataset_quality.py。

前置条件
--------
- 已 clone ESPectre 仓库并建好虚拟环境：

  macOS / Linux：
      cd <repo>
      python3 -m venv .venv
      source .venv/bin/activate
      python -m pip install -r requirements.txt

  Windows（PowerShell）：
      cd <repo>
      python -m venv .venv
      .venv\Scripts\Activate.ps1
      python -m pip install -r requirements.txt

- 设备已联网，且与电脑处于同一局域网（可互访 62587 端口）。
- 知道设备 IP，或先用 `./espectre devices` 发现。

跨平台说明
----------
官方仓库里的 `espectre` 文件本身就是一个 Python 脚本（`#!/usr/bin/env
python3`），而 `espectre.cmd` 只是它在 Windows 上的包装壳。

因此本脚本统一用「虚拟环境里的 Python 解释器直接执行 espectre 文件」来调用
官方 CLI：

    macOS / Linux : .venv/bin/python        espectre collect ...
    Windows       : .venv\\Scripts\\python.exe espectre collect ...

这样在 Windows 上可以完全绕开 .cmd 的引号解析问题，也不依赖 PATH 里的
python / py。没有 .venv 时会退回到当前解释器。

典型用法
--------
    # 1) 只做自检，不采集
    python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
        --room bedroom --self-check

    # 2) 三种标签各采 10 条，每条 60 秒
    python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
        --room bedroom --rounds 10 --duration 60

    # 3) 只采 empty，先跑 3 条试水
    python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 \
        --room bedroom --labels empty --rounds 3 --duration 30
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_LABELS = ("empty", "static_presence", "motion")

LABEL_HINTS = {
    "empty": "房间内无人，且保持绝对安静",
    "static_presence": "房间内有人，但基本静止不动（坐着或站着）",
    "motion": "房间内有人，正常走动活动",
}

# 只有 empty 需要“人离开房间”的缓冲时间，其他标签人本来就在房间里
LABELS_NEEDING_START_DELAY = {"empty"}

ORIGIN = "https://test.espectre.dev"  # 固件默认放行的 Origin，官方 CLI 也用这个
DIRECT_PORT = 62587


# --------------------------------------------------------------------------
# 输出辅助
# --------------------------------------------------------------------------
def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print("  [ok] " + msg, flush=True)


def warn(msg: str) -> None:
    print("  [!]  " + msg, flush=True)


def err(msg: str) -> None:
    print("  [x]  " + msg, file=sys.stderr, flush=True)


def hr(char: str = "-", width: int = 68) -> None:
    print(char * width, flush=True)


class Tee:
    """把标准输出同时写到屏幕与日志文件。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a", encoding="utf-8")
        self._stdout = sys.stdout

    def write(self, data: str) -> int:
        self._stdout.write(data)
        self._file.write(data)
        return len(data)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.flush()
            self._file.close()
        except Exception:
            pass


def setup_console() -> None:
    """把标准输出/错误强制为 UTF-8。

    Windows 控制台默认代码页不是 UTF-8，而本脚本输出大量中文，这里统一编码，
    避免出现乱码或 UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 设备探测（Direct HTTP）
# --------------------------------------------------------------------------
def normalize_base(target: str) -> str:
    """把各种 target 写法统一成 http://host:62587 形式。"""
    t = target.strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0]
    if ":" not in t:
        t = t + ":" + str(DIRECT_PORT)
    return "http://" + t


def http_json(base: str, path: str, method: str = "GET",
              payload=None, timeout: float = 6.0) -> dict:
    url = base.rstrip("/") + "/espectre/v1/" + path.lstrip("/")
    headers = {"Origin": ORIGIN, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def probe_device(target: str) -> tuple[bool, dict]:
    """返回 (是否探测成功, 探测结果)。探测失败不致命，只警告。"""
    base = normalize_base(target)
    info("  探测地址: " + base)
    result: dict = {"base": base}
    try:
        result["health"] = http_json(base, "health")
        result["device"] = http_json(base, "device")
        result["capabilities"] = http_json(base, "capabilities")
    except urllib.error.HTTPError as exc:
        warn("HTTP %s：%s" % (exc.code, exc.reason))
        if exc.code == 403:
            warn("Origin 被拒绝。确认设备固件放行了 " + ORIGIN)
        elif exc.code == 404:
            warn("端点不存在。设备可能不是 ESPectre，或固件过旧。")
        return False, result
    except urllib.error.URLError as exc:
        warn("无法连接：%s" % (exc.reason,))
        warn("确认电脑与设备在同一局域网，且没有客户端隔离。")
        return False, result
    except Exception as exc:  # noqa: BLE001
        warn("探测异常：%r" % (exc,))
        return False, result
    return True, result


def report_device(probe: dict) -> list[str]:
    """打印设备信息，返回致命问题列表（非空则不应继续）。"""
    fatal: list[str] = []
    device = probe.get("device", {}) or {}
    caps = probe.get("capabilities", {}) or {}

    frontend = str(device.get("frontend", "")).strip().lower()
    chip = str(device.get("chip", "")).strip()
    label = str(device.get("label") or device.get("name") or "")
    fw = str(device.get("firmware", ""))
    profile = str(device.get("csi_profile", ""))
    dev_id = str(device.get("device_id", ""))
    features = caps.get("features", {}) or {}
    csi_ok = bool(features.get("csi"))
    proto = str(caps.get("protocol_version", ""))

    info("  设备名称  : " + (label or "(未命名)"))
    info("  device_id : " + (dev_id or "(未知)"))
    info("  frontend  : " + (frontend or "(未知)"))
    info("  芯片      : " + (chip or "(未知)"))
    info("  固件版本  : " + (fw or "(未知)"))
    info("  CSI 端点  : " + ("可用" if csi_ok else "不可用"))
    if proto:
        info("  协议版本  : " + proto)
    if profile:
        info("  捕获档位  : " + profile)

    if frontend == "micro":
        fatal.append(
            "frontend 是 micro（Micro-ESPectre），它不支持 /csi 端点，"
            "原始 CSI 采集无法进行。请重刷为 Native 前端固件。"
        )
    elif frontend and frontend not in ("native", "esphome", "matter"):
        warn("未知 frontend %r，脚本继续尝试。" % frontend)

    if not csi_ok:
        fatal.append(
            "capabilities.features.csi 为 false，该设备当前不支持原始 CSI 采集。"
        )

    if "c6" in chip.lower():
        info("")
        warn("检测到 ESP32-C6：wifi_raw 流量模式在该芯片上不可用。")
        warn("本脚本走的是 external 外部流量（UDP marker）通路，不受影响。")
        warn("但请不要在 Monitor 里选择 Wi-Fi raw，那在 C6 上是无效设置。")

    return fatal


# --------------------------------------------------------------------------
# 运行官方 CLI
# --------------------------------------------------------------------------
def build_cli_env(repo: Path) -> dict:
    env = os.environ.copy()
    venv = repo / ".venv"
    if venv.is_dir():
        env["VIRTUAL_ENV"] = str(venv)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        if bin_dir.is_dir():
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return env


def num(value) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else str(f)


def venv_python(repo: Path) -> Path | None:
    """返回虚拟环境里的 Python 解释器；不存在则返回 None。"""
    candidates = (
        repo / ".venv" / "bin" / "python",
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "Scripts" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def resolve_invocation(repo: Path) -> list[str] | None:
    """构造调用官方 CLI 的命令前缀。

    官方仓库的 `espectre` 本身是 Python 脚本，`espectre.cmd` 只是它的
    Windows 包装壳。用解释器直接执行 espectre 文件是最稳的跨平台方式。
    """
    entry = repo / "espectre"
    if not entry.is_file():
        cmd = repo / "espectre.cmd"
        if cmd.is_file() and os.name == "nt":
            return ["cmd.exe", "/c", str(cmd)]
        return None
    python = venv_python(repo) or Path(sys.executable)
    return [str(python), str(entry)]


def display_cmd(prefix: list[str], argv: list[str]) -> str:
    parts = list(prefix) + list(argv)
    if os.name == "nt":
        return " ".join('"%s"' % p if " " in p else p for p in parts)
    return " ".join(parts)


def run_cli(repo: Path, prefix: list[str], argv: list[str], env: dict,
            timeout_s: float, dry_run: bool) -> int:
    full = list(prefix) + list(argv)
    info("  $ " + display_cmd(prefix, argv))
    if dry_run:
        info("    (dry-run，跳过实际执行)")
        return 0
    started = time.time()
    try:
        proc = subprocess.run(full, cwd=str(repo), env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        err("命令超过 %.0f 秒仍未结束，已终止。" % timeout_s)
        return 124
    except PermissionError:
        err("没有执行权限：" + str(prefix[0]))
        return 126
    except FileNotFoundError:
        err("找不到可执行文件：" + str(prefix[0]))
        return 127
    except OSError as exc:
        err("启动失败：%r" % (exc,))
        return 127
    info("    用时 %.1f 秒，退出码 %d" % (time.time() - started, proc.returncode))
    return proc.returncode


# --------------------------------------------------------------------------
# dataset_info.json 处理
# --------------------------------------------------------------------------
def load_catalog(path: Path) -> dict | None:
    if not path.is_file():
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def catalog_filenames(catalog: dict | None) -> set[str]:
    names: set[str] = set()
    if not catalog:
        return names
    for entries in (catalog.get("files") or {}).values():
        for entry in entries or []:
            name = entry.get("filename")
            if name:
                names.add(str(name))
    return names


def backfill_catalog(path: Path, before: set[str], room: str, role: str,
                     dry_run: bool) -> list[tuple[str, str]]:
    """给本次新增的录音回填 environment / dataset_role，返回新增清单。"""
    catalog = load_catalog(path)
    if catalog is None:
        warn("找不到 %s，跳过元数据回填。" % path)
        return []

    added: list[tuple[str, str]] = []
    files = catalog.get("files") or {}
    for label, entries in files.items():
        for entry in entries or []:
            name = str(entry.get("filename") or "")
            if name and name not in before:
                entry["environment"] = room
                entry["dataset_role"] = role
                added.append((label, name))

    if not added:
        warn("没有检测到新增录音，元数据未改动。")
        return []

    # 确保房间已注册进 environments
    envs = catalog.get("environments")
    if not isinstance(envs, list):
        envs = []
        catalog["environments"] = envs
    if not any(str(e.get("id")) == room for e in envs if isinstance(e, dict)):
        envs.append({"id": room, "description": "%s environment" % room})
        info("  已在 environments 注册房间：" + room)
        # 尝试保持既有排序（按 id）
        try:
            envs.sort(key=lambda e: str(e.get("id", "")))
        except Exception:
            pass

    catalog["updated_at"] = datetime.now().isoformat(timespec="seconds")

    if dry_run:
        info("  (dry-run) 将回填 %d 条录音的 environment=%s, dataset_role=%s"
             % (len(added), room, role))
        return added

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, backup)
        info("  已备份原目录文件到 " + backup.name)
    except Exception as exc:  # noqa: BLE001
        warn("备份失败（继续写入）：%r" % (exc,))

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    info("  已回填 %d 条录音：environment=%s, dataset_role=%s"
         % (len(added), room, role))
    return added


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ESPectre 批量 CSI 采集自动化脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 collect_batch.py --repo ~/espectre --target 192.168.1.50 "
               "--room bedroom --rounds 10 --duration 60\n",
    )
    p.add_argument("--repo", required=True, help="ESPectre 仓库根目录（内含 ./espectre）")
    p.add_argument("--target", required=True, help="设备 IP / 主机名 / Direct 地址")
    p.add_argument("--room", required=True, help="房间名，写入 dataset_info.json 的 environment")
    p.add_argument("--rounds", type=int, default=10, help="每个标签采集条数（默认 10）")
    p.add_argument("--duration", type=float, default=60.0, help="每条录音时长秒数（默认 60）")
    p.add_argument("--start-delay", type=float, default=15.0,
                   help="empty 标签开始前留空秒数，便于离开房间（默认 15；其他标签自动为 0）")
    p.add_argument("--pps", type=int, default=100,
                   help="采集流量发包率（默认 100；应与设备 csi_target_pps 一致）")
    p.add_argument("--detector", choices=["lightweight", "high_accuracy"],
                   default="lightweight", help="readiness 检查所用检测器（默认 lightweight）")
    p.add_argument("--ready-stable-seconds", type=float, default=None,
                   help="检测器保持 idle 多久才开始录制；不传用官方默认。motion 标签自动置 0")
    p.add_argument("--labels", default=",".join(DEFAULT_LABELS),
                   help="要采集的标签，逗号分隔（默认全部三个）")
    p.add_argument("--role", default="exclude",
                   choices=["exclude", "train", "selection", "holdout"],
                   help="写入 dataset_role（默认 exclude；复查无误后再改 train）")
    p.add_argument("--frontend", choices=["native", "esphome", "matter"], default=None,
                   help="传给官方 CLI 的 --frontend，限定设备发现范围（可选）")
    p.add_argument("--self-check", action="store_true", help="只做自检，不采集")
    p.add_argument("--skip-probe", action="store_true", help="跳过 HTTP 自检")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    p.add_argument("--no-wait", action="store_true", help="标签切换时不等待人工确认")
    p.add_argument("--no-validate", action="store_true", help="采集后不运行质量校验")
    p.add_argument("--stop-on-error", action="store_true", help="任一录音失败即中止")
    p.add_argument("--no-log", action="store_true", help="不写日志文件")
    return p.parse_args()


def prepare_plan(args: argparse.Namespace) -> list[str]:
    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    unknown = [l for l in labels if l not in LABEL_HINTS]
    if unknown:
        err("未知标签：%s。可用标签：%s" % (", ".join(unknown), ", ".join(DEFAULT_LABELS)))
        sys.exit(2)
    return labels


def ensure_repo(repo: Path) -> list[str]:
    if not repo.is_dir():
        err("仓库目录不存在：" + str(repo))
        sys.exit(2)
    prefix = resolve_invocation(repo)
    if prefix is None:
        err("在 %s 下找不到 espectre 入口文件。" % repo)
        err("请在 ESPectre 仓库根目录执行，或用 --repo 指定正确路径。")
        sys.exit(2)
    if venv_python(repo) is None:
        warn("未发现 .venv 虚拟环境，官方 CLI 可能因缺少依赖而失败。")
        if os.name == "nt":
            warn("Windows 上请先执行：python -m venv .venv && "
                 ".venv\\Scripts\\Activate.ps1 && python -m pip install -r requirements.txt")
        else:
            warn("请先执行：python3 -m venv .venv && source .venv/bin/activate "
                 "&& python -m pip install -r requirements.txt")
    return prefix


def wait_for_user(prompt: str, skip: bool) -> None:
    if skip:
        info("  (--no-wait，跳过人工确认)")
        return
    try:
        input(prompt)
    except EOFError:
        warn("无法读取输入（非交互环境），自动继续。")
    except KeyboardInterrupt:
        info("")
        info("已被用户中断，退出。")
        sys.exit(130)


def main() -> int:
    args = parse_args()
    setup_console()
    labels = prepare_plan(args)

    repo = Path(args.repo).expanduser().resolve()
    script_dir = Path(__file__).resolve().parent
    tee = None
    log_path = None
    if not args.no_log:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = script_dir / "logs" / ("collect_%s.log" % stamp)
        tee = Tee(log_path)
        sys.stdout = tee  # type: ignore[assignment]

    exit_code = 0
    try:
        hr("=")
        info("ESPectre 批量 CSI 采集")
        info("  时间      : " + datetime.now().isoformat(timespec="seconds"))
        info("  仓库      : " + str(repo))
        info("  目标设备  : " + args.target)
        info("  房间      : " + args.room)
        info("  标签      : " + ", ".join(labels))
        info("  每条时长  : %s 秒" % num(args.duration))
        info("  每标签条数: %d" % args.rounds)
        info("  发包率    : %d pps" % args.pps)
        info("  元数据角色: " + args.role)
        if log_path:
            info("  日志      : " + str(log_path))
        hr("=")

        # ---------- 1. 环境检查 ----------
        info("")
        info("[1/5] 检查仓库环境")
        prefix = ensure_repo(repo)
        ok("调用方式：" + " ".join(prefix))
        cli_env = build_cli_env(repo)
        vpy = venv_python(repo)
        if vpy is not None:
            ok("使用虚拟环境解释器：" + str(vpy))

        # ---------- 2. 设备自检 ----------
        info("")
        info("[2/5] 设备自检")
        probe_failed = False
        if args.skip_probe:
            warn("已跳过设备自检（--skip-probe）")
        else:
            success, probe = probe_device(args.target)
            if success:
                fatal = report_device(probe)
                if fatal:
                    err("设备不满足采集条件：")
                    for item in fatal:
                        err("  - " + item)
                    return 3
                ok("设备自检通过")
            else:
                probe_failed = True
                err("设备自检失败：无法通过 HTTP 访问设备。")
                err("常见原因：")
                err("  - 设备 IP 写错")
                err("  - 电脑与设备不在同一局域网（路由器开了 AP 隔离）")
                err("  - Windows 防火墙拦截了 Python 的网络访问")
                err("若设备确实可用、只是 HTTP 不通，请加 --skip-probe 跳过自检。")

        if args.self_check:
            info("")
            if probe_failed:
                err("自检未通过：设备不可达。")
                return 3
            ok("自检模式结束，未执行采集。")
            return 0

        if probe_failed:
            err("自检未通过，已中止本次运行。")
            err("（这样是为了避免进入采集阶段后每条录音都失败、日志疯狂滚动。）")
            err("请先排查设备连通性再重试。")
            return 3

        # ---------- 3. 记录目录快照 ----------
        catalog_path = repo / "data" / "dataset_info.json"
        before_names = catalog_filenames(load_catalog(catalog_path))
        info("")
        info("[3/5] 采集前目录快照")
        info("  dataset_info.json : %s" % ("已读取" if catalog_path.is_file() else "未找到"))
        info("  已有录音条数      : %d" % len(before_names))

        # ---------- 4. 批量采集 ----------
        info("")
        info("[4/5] 开始批量采集")
        info("")
        wait_for_user("确认设备已就绪、房间状态可控后按回车开始（Ctrl+C 取消）... ",
                      args.no_wait)

        results: list[tuple[str, int, int]] = []  # (label, 失败数, 成功数)
        aborted = False
        consecutive_fail = 0
        max_consecutive_fail = 3

        for idx, label in enumerate(labels, start=1):
            if aborted:
                break
            hint = LABEL_HINTS[label]
            hr("=")
            info("[%d/%d] 标签：%s" % (idx, len(labels), label))
            info("  要求：%s" % hint)
            info("  计划：%d 条 x %s 秒" % (args.rounds, num(args.duration)))
            hr("=")

            ok_count = 0
            fail_count = 0

            for rnd in range(1, args.rounds + 1):
                info("")
                info("  --- %s 第 %d/%d 条 ---" % (label, rnd, args.rounds))
                wait_for_user("  准备好后按回车开始这一条... ", args.no_wait)

                start_delay = args.start_delay if label in LABELS_NEEDING_START_DELAY else 0.0

                # motion 标签若保留 readiness 检查会永远等不到 idle，必须禁用
                if label == "motion":
                    ready_stable = 0.0
                    if args.ready_stable_seconds is None:
                        info("  (motion 标签自动设置 --ready-stable-seconds 0，"
                             "否则检测器等不到 idle 就无法开始录制)")
                else:
                    ready_stable = args.ready_stable_seconds

                argv = ["collect",
                        "--target", args.target,
                        "--label", label,
                        "--duration", num(args.duration),
                        "--pps", str(args.pps),
                        "--detector", args.detector]
                if start_delay > 0:
                    argv += ["--start-delay", num(start_delay)]
                if ready_stable is not None:
                    argv += ["--ready-stable-seconds", num(ready_stable)]
                if args.frontend:
                    argv += ["--frontend", args.frontend]

                if start_delay > 0:
                    info("  提示：命令启动后有 %s 秒留空，请在此之前离开房间。"
                         % num(start_delay))

                timeout_s = start_delay + float(args.duration) + 300.0
                code = run_cli(repo, prefix, argv, cli_env, timeout_s, args.dry_run)

                if code == 0:
                    ok_count += 1
                    consecutive_fail = 0
                    ok("第 %d 条完成" % rnd)
                else:
                    fail_count += 1
                    consecutive_fail += 1
                    err("第 %d 条失败（退出码 %d）" % (rnd, code))
                    if code == 130:
                        info("  用户中断，停止采集。")
                        aborted = True
                        break
                    if args.stop_on_error:
                        info("  --stop-on-error 已启用，停止采集。")
                        aborted = True
                        break
                    if consecutive_fail >= max_consecutive_fail:
                        err("已连续 %d 条失败，判定为环境问题，主动中止采集。"
                            % consecutive_fail)
                        err("请先看清上面的报错内容再重新运行，避免继续刷屏。")
                        aborted = True
                        break

            results.append((label, fail_count, ok_count))

        # ---------- 5. 回填元数据 ----------
        info("")
        info("[5/5] 回填元数据")
        added = backfill_catalog(catalog_path, before_names, args.room,
                                 args.role, args.dry_run)
        for label, name in added:
            info("  + [%s] %s" % (label, name))

        # ---------- 质量校验 ----------
        validate_failed = False
        if added and not args.no_validate and not args.dry_run:
            info("")
            info("运行官方质量校验：tools/validate_dataset_quality.py")
            validator = repo / "tools" / "validate_dataset_quality.py"
            if validator.is_file():
                py = venv_python(repo) or Path(sys.executable)
                info("  $ %s tools/validate_dataset_quality.py" % py)
                try:
                    proc = subprocess.run([str(py), "tools/validate_dataset_quality.py"],
                                          cwd=str(repo), env=cli_env)
                    if proc.returncode != 0:
                        validate_failed = True
                        err("质量校验未通过（退出码 %d）。" % proc.returncode)
                        err("占用率低于 70% 的录音会被判 FAIL，请先解决再训练。")
                    else:
                        ok("质量校验通过")
                except Exception as exc:  # noqa: BLE001
                    warn("校验脚本执行异常：%r" % (exc,))
            else:
                warn("找不到 tools/validate_dataset_quality.py，跳过校验。")
        elif not added:
            warn("本次没有新增录音，跳过校验。")

        # ---------- 汇总 ----------
        info("")
        hr("=")
        info("采集汇总")
        hr("=")
        for label, fail_count, ok_count in results:
            info("  %-16s 成功 %d 条，失败 %d 条" % (label, ok_count, fail_count))
        info("  新增录音总数    : %d" % len(added))
        info("")
        if args.role == "exclude":
            info("  注意：dataset_role 目前是 exclude，训练时会被忽略。")
            info("  复查数据无误后，把 data/dataset_info.json 里对应条目的")
            info("  dataset_role 改成 train（或用 --role train 重跑本次采集）。")
        if validate_failed:
            info("  注意：质量校验存在 FAIL 项，训练前必须解决。")
        info("")
        info("  下一步：")
        info("    ./espectre collect --info                 # 查看已采录音")
        info("    python tools/train_ml_model.py --info     # 查看训练会用到哪些录音")
        info("    python tools/train_ml_model.py            # 训练并导出权重")
        hr("=")

        if validate_failed:
            exit_code = 4
        elif any(f for _, f, _ in results):
            exit_code = 1
        return exit_code

    finally:
        if tee is not None:
            sys.stdout = tee._stdout  # type: ignore[assignment]
            tee.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        info("")
        info("已取消。")
        sys.exit(130)
