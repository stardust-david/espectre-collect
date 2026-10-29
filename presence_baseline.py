#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESPectre 存在检测可行性基线 —— 第 0 步实验

这个脚本不训练深度模型。它只回答一个前置问题：

    「empty（屋里没人）」和「static_presence（有人但基本不动）」
    在 CSI 上到底分不分得开？

如果这一对都分不开，换任何模型都没用，应该先改采集方式（位置、朝向、窗口长度）。
如果分得开，再投入做更复杂的特征和模型。

设计原则
--------
1. 只用手工特征 + 传统分类器，样本量小的时候比神经网络可靠。
2. 严格按「录音」划分训练/测试（Leave-One-Recording-Out），绝不按窗口随机划分，
   否则相邻窗口高度相关会让指标虚高到 99%。
3. 读 npz 时自动探测字段名，不硬编码，便于适配不同固件版本。

用法
----
    # 只看有哪些数据可用
    python presence_baseline.py --data C:\\espectre\\data --list

    # 跑 empty vs static_presence 的基线实验
    python presence_baseline.py --data C:\\espectre\\data

    # 窗口放到 10 秒（静态检测建议长窗口）
    python presence_baseline.py --data data --window 1000 --stride 250

输出
----
- 控制台：AUC、混淆矩阵、每条录音的预测概率、特征重要性
- 文件：<out>/predictions.csv、<out>/features.csv

依赖
----
    python -m pip install numpy scikit-learn
"""

from __future__ import annotations

import argparse
import csv
import sys
import warnings
from pathlib import Path

import numpy as np

FEATURE_NAMES = [
    "amp_mean",        # 全窗口幅度均值
    "amp_std_sc",      # 子载波均值的标准差
    "amp_var_mean",    # 每子载波时间方差 -> 均值
    "amp_var_median",  # 每子载波时间方差 -> 中位数
    "amp_var_max",     # 每子载波时间方差 -> 最大值
    "amp_cv_mean",     # 变异系数 (std/mean)，抗整体增益漂移
    "corr_mean",       # 子载波间相关系数矩阵上三角均值
    "corr_std",        # 上三角标准差
    "corr_min",        # 上三角最小值
    "amp_range_mean",  # 每子载波 max-min 的均值
    "amp_iqr_mean",    # 每子载波 IQR 的均值
    "amp_skew_mean",   # 每子载波偏度的均值
]


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print("  [ok] " + msg, flush=True)


def warn(msg: str) -> None:
    print("  [!]  " + msg, flush=True)


def bad(msg: str) -> None:
    print("  [x]  " + msg, file=sys.stderr, flush=True)


def rule(title: str = "") -> None:
    print("", flush=True)
    print("-" * 68, flush=True)
    if title:
        print(title, flush=True)
        print("-" * 68, flush=True)


def require_deps() -> bool:
    missing = []
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    try:
        import sklearn  # noqa: F401
    except ImportError:
        missing.append("scikit-learn")
    if missing:
        bad("缺少依赖：" + ", ".join(missing))
        bad("请先安装：")
        bad("    python -m pip install numpy scikit-learn")
        return False
    return True


# --------------------------------------------------------------------------
# 数据发现与加载
# --------------------------------------------------------------------------
def find_recordings(data_dir: Path, labels: list[str]) -> list[tuple[Path, str]]:
    """在 data/<label>/*.npz 结构下收集录音。"""
    found: list[tuple[Path, str]] = []
    for label in labels:
        sub = data_dir / label
        if not sub.is_dir():
            warn("找不到标签目录：%s" % sub)
            continue
        for path in sorted(sub.glob("*.npz")):
            found.append((path, label))
    return found


def probe_npz(path: Path) -> dict:
    """只读出结构信息，不加载全部数组。"""
    with np.load(path, allow_pickle=False) as d:
        keys = list(d.files)
        shape = None
        arr_name = None
        for cand in ("csi_data", "csi", "data", "iq", "csi_array"):
            if cand in d.files:
                shape = tuple(d[cand].shape)
                arr_name = cand
                break
        return {"keys": keys, "array": arr_name, "shape": shape}


def load_csi_amplitude(path: Path) -> tuple[np.ndarray, str]:
    """把 npz 里的 int8 CSI 转成幅度矩阵 [N, SC]。"""
    with np.load(path, allow_pickle=False) as d:
        arr_name = None
        for cand in ("csi_data", "csi", "data", "iq", "csi_array"):
            if cand in d.files:
                arr_name = cand
                break
        if arr_name is None:
            raise KeyError(
                "在 %s 里找不到 CSI 数组，可用字段：%s"
                % (path.name, ", ".join(d.files))
            )
        raw = np.asarray(d[arr_name])

    if raw.ndim != 2:
        raise ValueError("%s 的 CSI 数组不是二维，shape=%s" % (path.name, raw.shape))
    if raw.shape[1] % 2 != 0:
        raise ValueError(
            "%s 的列数 %d 不是偶数，无法按 [Q, I] 成对解析"
            % (path.name, raw.shape[1])
        )

    # Espressif 顺序：[Q0, I0, Q1, I1, ...]
    q = raw[:, 0::2].astype(np.float32)
    i = raw[:, 1::2].astype(np.float32)
    amp = np.sqrt(i * i + q * q)
    return amp, arr_name


def drop_dead_subcarriers(amp: np.ndarray) -> tuple[np.ndarray, int]:
    """剔除全程恒为 0 的子载波（空导频/保护带/无效 bin）。"""
    if amp.shape[1] <= 4:
        return amp, 0
    alive = amp.std(axis=0) > 1e-6
    dropped = int((~alive).sum())
    if alive.sum() < 4:
        return amp, 0
    return amp[:, alive], dropped


# --------------------------------------------------------------------------
# 特征提取
# --------------------------------------------------------------------------
def window_features(win: np.ndarray) -> list[float]:
    """win: [T, SC] 幅度矩阵 -> 一行特征。"""
    t, sc = win.shape
    mean_sc = win.mean(axis=0)
    var_t = win.var(axis=0)
    std_t = win.std(axis=0)

    feats = [
        float(win.mean()),                                  # amp_mean
        float(mean_sc.std()),                               # amp_std_sc
        float(var_t.mean()),                                # amp_var_mean
        float(np.median(var_t)),                            # amp_var_median
        float(var_t.max()),                                 # amp_var_max
        float((std_t / (np.abs(mean_sc) + 1e-6)).mean()),   # amp_cv_mean
    ]

    if sc >= 2:
        corr = np.corrcoef(win.T)
        iu = np.triu_indices_from(corr, k=1)
        cc = corr[iu]
        cc = cc[np.isfinite(cc)]
        if cc.size == 0:
            cc = np.zeros(1, dtype=np.float32)
        feats.extend([float(cc.mean()), float(cc.std()), float(cc.min())])
    else:
        feats.extend([0.0, 0.0, 0.0])

    feats.append(float((win.max(axis=0) - win.min(axis=0)).mean()))   # amp_range_mean

    q75 = np.percentile(win, 75, axis=0)
    q25 = np.percentile(win, 25, axis=0)
    feats.append(float((q75 - q25).mean()))                            # amp_iqr_mean

    # 偏度：用标准化后的三阶矩，避免引入 scipy 依赖
    if t >= 3:
        centered = win - mean_sc
        sd = std_t + 1e-6
        skew = (centered ** 3).mean(axis=0) / (sd ** 3)
        feats.append(float(skew.mean()))
    else:
        feats.append(0.0)

    return feats


def recording_features(amp: np.ndarray, window: int, stride: int) -> np.ndarray | None:
    n = amp.shape[0]
    if n < window:
        return None
    rows = []
    for start in range(0, n - window + 1, stride):
        rows.append(window_features(amp[start:start + window]))
    if not rows:
        return None
    return np.asarray(rows, dtype=np.float32)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ESPectre 存在检测可行性基线（empty vs static_presence）",
    )
    p.add_argument("--data", default="data", help="数据根目录（下含 <label>/*.npz）")
    p.add_argument("--labels", default="empty,static_presence",
                   help="两个标签，逗号分隔；第一个作为负类（无人）")
    p.add_argument("--window", type=int, default=300,
                   help="窗口长度（包数）。100 pps 时 300 包约 3 秒；静态检测建议 1000（约 10 秒）")
    p.add_argument("--stride", type=int, default=100, help="滑动步长（包数）")
    p.add_argument("--out", default="results", help="结果输出目录")
    p.add_argument("--list", action="store_true", help="只列出数据，不训练")
    return p.parse_args()


def main() -> int:
    setup_console()
    args = parse_args()

    if not require_deps():
        return 2

    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        confusion_matrix,
        precision_recall_fscore_support,
        roc_auc_score,
        roc_curve,
    )
    from sklearn.model_selection import LeaveOneGroupOut, cross_val_predict
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    data_dir = Path(args.data).expanduser().resolve()
    labels = [s.strip() for s in args.labels.split(",") if s.strip()]
    if len(labels) != 2:
        bad("--labels 目前只支持两个标签（负类,正类），收到：%s" % args.labels)
        return 2

    info("数据目录 : %s" % data_dir)
    info("标签     : %s=0（无人） / %s=1（有人）" % (labels[0], labels[1]))
    info("窗口     : %d 包，步长 %d 包" % (args.window, args.stride))

    if not data_dir.is_dir():
        bad("数据目录不存在：%s" % data_dir)
        return 2

    recordings = find_recordings(data_dir, labels)
    if not recordings:
        bad("在 %s 下没有找到 %s 的任何 npz 文件。" % (data_dir, " / ".join(labels)))
        bad("请确认数据结构是 <data>/<label>/*.npz")
        return 2

    # ---- 只列出 ----
    if args.list:
        rule("数据清单")
        for label in labels:
            subset = [p for p, l in recordings if l == label]
            info("%s : %d 条" % (label, len(subset)))
            for p in subset:
                try:
                    st = probe_npz(p)
                    info("    %-50s shape=%s" % (p.name, st["shape"]))
                except Exception as exc:  # noqa: BLE001
                    info("    %-50s 读取失败：%r" % (p.name, exc))
        return 0

    # ---- 提取特征 ----
    rule("提取特征")
    X_list, y_list, g_list, rec_list = [], [], [], []
    skipped = []
    for idx, (path, label) in enumerate(recordings):
        try:
            amp, arr_name = load_csi_amplitude(path)
        except Exception as exc:  # noqa: BLE001
            warn("%s 读取失败，跳过：%r" % (path.name, exc))
            skipped.append((path, "读取失败"))
            continue

        amp, dropped = drop_dead_subcarriers(amp)
        feats = recording_features(amp, args.window, args.stride)
        if feats is None:
            warn("%s 只有 %d 包，不足一个窗口（%d），跳过"
                 % (path.name, amp.shape[0], args.window))
            skipped.append((path, "包数不足"))
            continue

        info("  %-46s 标签=%-16s 包=%-6d 子载波=%-3d 窗口=%d%s"
             % (path.name[:46], label, amp.shape[0], amp.shape[1], feats.shape[0],
                "" if dropped == 0 else " (剔除空子载波 %d)" % dropped))

        X_list.append(feats)
        y_list.append(np.full(feats.shape[0], 0 if label == labels[0] else 1, dtype=np.int8))
        g_list.append(np.full(feats.shape[0], idx, dtype=np.int32))
        rec_list.append((idx, label, path.name, feats.shape[0]))

    if not X_list:
        bad("没有任何录音成功提取到特征。")
        return 3

    X = np.vstack(X_list)
    y = np.concatenate(y_list)
    groups = np.concatenate(g_list)

    n_neg = int((y == 0).sum())
    n_pos = int((y == 1).sum())
    n_rec_neg = len({g for g, yy in zip(groups, y) if yy == 0})
    n_rec_pos = len({g for g, yy in zip(groups, y) if yy == 1})

    rule("数据集概览")
    info("  窗口总数 : %d（负类 %d / 正类 %d）" % (len(y), n_neg, n_pos))
    info("  录音数   : %d 条（负类 %d 条 / 正类 %d 条）"
         % (len(rec_list), n_rec_neg, n_rec_pos))

    if n_rec_neg < 2 or n_rec_pos < 2:
        bad("每类至少需要 2 条录音才能做留一录音交叉验证。")
        bad("当前 %d / %d。请再采一些数据。" % (n_rec_neg, n_rec_pos))
        return 3

    if len(rec_list) < 6:
        warn("录音总数少于 6 条，AUC 的波动会很大，结论只能当方向参考。")

    # ---- 交叉验证 ----
    logo = LeaveOneGroupOut()
    models = {
        "LogisticRegression": Pipeline([
            ("scaler", StandardScaler()),
            # C 取小值：类间完全可分时，逻辑回归的最优权重不存在，
            # 权重会一直发散直到数值溢出。强一点的 L2 正则能让它保持有界。
            ("clf", LogisticRegression(max_iter=1000, C=0.1, class_weight="balanced")),
        ]),
        "RandomForest": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(
                n_estimators=300, min_samples_leaf=5,
                class_weight="balanced_subsample", random_state=0, n_jobs=-1,
            )),
        ]),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []

    for name, pipe in models.items():
        rule("模型评估：%s" % name)
        try:
            # 数据完全可分时（AUC=1.0），逻辑回归的权重会发散并打印大量
            # RuntimeWarning。这是数学上的正常现象，不影响预测结果，这里静默掉。
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=RuntimeWarning)
                proba = cross_val_predict(
                    pipe, X, y, groups=groups, cv=logo, method="predict_proba",
                )[:, 1]
        except Exception as exc:  # noqa: BLE001
            bad("交叉验证失败：%r" % (exc,))
            continue

        auc = roc_auc_score(y, proba)
        pred = (proba >= 0.5).astype(np.int8)
        cm = confusion_matrix(y, pred, labels=[0, 1])
        precision, recall, f1, _ = precision_recall_fscore_support(
            y, pred, labels=[0, 1], zero_division=0,
        )

        info("  AUC                : %.4f" % auc)
        info("  混淆矩阵（行=真实，列=预测，顺序：%s, %s）" % (labels[0], labels[1]))
        info("        %-20s %-20s" % ("pred_" + labels[0], "pred_" + labels[1]))
        info("  %-20s %-20d %-20d" % (labels[0] + "(真)", cm[0, 0], cm[0, 1]))
        info("  %-20s %-20d %-20d" % (labels[1] + "(真)", cm[1, 0], cm[1, 1]))
        info("  %s 召回率 : %.3f" % (labels[0], recall[0]))
        info("  %s 召回率 : %.3f" % (labels[1], recall[1]))
        info("  %s 误报率（被误判为有人）: %.3f" % (labels[0], cm[0, 1] / max(cm[0].sum(), 1)))
        info("  %s 漏检率（被误判为无人）: %.3f" % (labels[1], cm[1, 0] / max(cm[1].sum(), 1)))

        # 等错误率附近的操作点
        fpr, tpr, thr = roc_curve(y, proba)
        if len(fpr) > 1:
            for target in (0.01, 0.05, 0.10):
                idx = int(np.searchsorted(fpr, target))
                idx = min(idx, len(fpr) - 1)
                info("  FPR<=%.0f%% 时检出率: %.3f" % (target * 100, tpr[idx]))

        # 每条录音的预测
        info("")
        info("  每条录音的平均预测概率（阈值 0.5）：")
        per_rec = []
        for gid, label, fname, nwin in rec_list:
            mask = groups == gid
            if not mask.any():
                continue
            p = float(proba[mask].mean())
            hit = "有人" if p >= 0.5 else "无人"
            truth = labels[0] if label == labels[0] else labels[1]
            flag = "OK" if (p >= 0.5) == (label != labels[0]) else "MISS"
            per_rec.append((gid, fname, truth, p, hit, flag))
        for gid, fname, truth, p, hit, flag in sorted(per_rec, key=lambda r: r[3]):
            info("    [%s] %-44s 真实=%-16s P(有人)=%.3f -> 判为%s"
                 % (flag, fname[:44], truth, p, hit))

        # 特征重要性（仅随机森林）
        if name == "RandomForest":
            pipe.fit(X, y)
            imp = pipe.named_steps["clf"].feature_importances_
            order = np.argsort(imp)[::-1]
            info("")
            info("  特征重要性（全量拟合，仅供参考）：")
            for i in order:
                info("    %-18s %.4f" % (FEATURE_NAMES[i], imp[i]))

        summary.append((name, auc, recall[0], recall[1], cm[0, 1] / max(cm[0].sum(), 1)))

        with open(out_dir / ("predictions_%s.csv" % name), "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["recording", "true_label", "mean_proba_present", "decision", "verdict"])
            for gid, fname, truth, p, hit, flag in per_rec:
                w.writerow([fname, truth, "%.6f" % p, hit, flag])

    # ---- 特征表 ----
    with open(out_dir / "features.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["group", "label"] + FEATURE_NAMES)
        for row, gid, label in zip(X, groups, y):
            w.writerow([int(gid), labels[0] if label == 0 else labels[1]] + ["%.6f" % v for v in row])

    # ---- 结论 ----
    rule("结论")
    if not summary:
        bad("没有成功评估的模型。")
        return 3

    best = max(summary, key=lambda item: item[1])
    info("  最佳模型 : %s" % best[0])
    info("  AUC      : %.4f" % best[1])
    info("")
    if best[1] >= 0.80:
        ok("信号里有可用信息（AUC >= 0.80）。")
        info("    下一步：加入呼吸频段能量和子载波间相位特征，再考虑换成更强的模型。")
    elif best[1] >= 0.60:
        warn("信号很弱（0.60 <= AUC < 0.80），边缘可用。")
        info("    下一步：先改采集，不要急着换模型。")
        info("    - 窗口加长到 10 秒（--window 1000 --stride 250）")
        info("    - 设备/路由器/家具位置固定，重新交替采集")
        info("    - 确认采集时没人时房间真的完全空")
    else:
        bad("基本不可分（AUC < 0.60）。")
        info("    这个配置下人静止的信号很可能被噪声淹没了。换模型没有用，必须改采集：")
        info("    - 加长窗口、固定位置、缩短设备到人的距离")
        info("    - 检查采集期间是否有其他人/宠物/风扇等干扰")
        info("    - 确认 empty 和 static_presence 是在同一位置、同一时段交替采集的")

    info("")
    info("  结果文件已写入：%s" % out_dir.resolve())
    info("    predictions_<model>.csv  每条录音的预测概率")
    info("    features.csv             每个窗口的特征值")

    if skipped:
        info("")
        info("  被跳过的文件：")
        for path, reason in skipped:
            info("    %-46s %s" % (path.name[:46], reason))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        info("")
        info("已取消。")
        sys.exit(130)
