"""批量测试工具 —— 进程内快速测试 + 箱线图。

用法:
    python batch_test.py                       # 测试所有版本，10种子，输出表格+箱线图
    python batch_test.py --seeds 20 40 60      # 指定种子
    python batch_test.py --num-seeds 10        # 等差数列生成10个种子
    python batch_test.py --versions V2 V3      # 指定版本
    python batch_test.py --network             # WebSocket 网络模式
    python batch_test.py --no-plot             # 跳过箱线图
    python batch_test.py --verbose             # 逐 tick 进度
    python batch_test.py --list                # 列出可用版本

常用：
    python batch_test.py --num-seeds 20 --versions V0 V1 V2 V3 V3_1 V4 V5 VN
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

DEFAULT_SEEDS = list(range(10, 101, 10))  # [10,20,30,...,100]
DEFAULT_VERSIONS: list[str] = []  # 空列表 = 自动发现全部


# ═══════════════════════════════════════════════════════════════════════════
# 进程内测试引擎
# ═══════════════════════════════════════════════════════════════════════════

def _build_mock_sdk(state, config):
    """构建模拟 SDK，注入路网数据，跳过 WebSocket 连接。"""
    from student_template.strategy.sdk_ext import StrategySDK
    sdk = StrategySDK.__new__(StrategySDK)
    sdk._nodes = {nid: {"x": n.x, "y": n.y} for nid, n in state.nodes.items()}
    sdk._zone_map = {}
    for z in (list(state.raw_zones.values()) +
              list(state.processing_zones.values()) +
              list(state.consumer_zones.values())):
        sdk._zone_map[z.id] = {"node": z.node_id, "position": list(z.position)}
    sdk._adjacency = {}
    for edge in state.edges:
        sdk._adjacency.setdefault(edge.from_node, []).append((edge.to_node, edge.distance))
        sdk._adjacency.setdefault(edge.to_node, []).append((edge.from_node, edge.distance))
    sdk._recipes = {rid: {"value": r.value, "processing_time": r.processing_time}
                    for rid, r in state.recipes.items()}
    sdk._map_width = config.get("map", {}).get("width", 200)
    sdk._map_height = config.get("map", {}).get("height", 200)
    sdk._collision_radius = config.get("game", {}).get("collision_radius", 1.0)
    sdk._zone_interaction_radius = config.get("game", {}).get("zone_interaction_radius", 3.0)
    sdk._raw_production_time = config.get("raw_materials", {}).get("production_time", 10.0)
    sdk._orders_timeout_base = config.get("orders", {}).get("timeout_base", 80.0)
    sdk._graph = {}
    sdk._state = None
    sdk.server_url = ""
    return sdk


def run_one(strategy_class, seed: int, verbose: bool = False) -> dict:
    """进程内运行策略一次，返回 {score, orders, value, drop, collision, overtime, ticks, elapsed}。"""
    from server.game_engine import (
        init_game_state, tick, is_game_over,
        get_state_snapshot, load_config, load_map, process_commands)

    config = load_config("config.json")
    map_data = load_map("map.json")
    state = init_game_state(config, map_data, seed=seed)
    sdk = _build_mock_sdk(state, config)
    strategy = strategy_class(sdk)

    tick_rate = config["game"]["tick_rate"]
    dt = 1.0 / tick_rate
    tick_count = 0
    t0 = time.time()

    while not is_game_over(state):
        state_dict = get_state_snapshot(state)
        sdk._state = state_dict
        try:
            commands = strategy(state_dict)
        except Exception as e:
            if verbose:
                import traceback; traceback.print_exc()
            print(f"  Strategy error at tick {tick_count}: {e}")
            break
        if commands:
            process_commands(state, commands)
        tick(state, dt)
        tick_count += 1
        if verbose and tick_count % 2000 == 0:
            e = time.time() - t0
            total = state.config['game']['duration'] * tick_rate
            print(f"    tick {tick_count}/{total:.0f} ({e:.1f}s) score={state.score:.0f}", flush=True)

    return {"score": state.score, "orders": state.completed_orders_count,
            "value": state.completed_orders_value, "drop": state.drop_reward_total,
            "collision": state.collision_penalty_total, "overtime": state.overtime_penalty_total,
            "ticks": tick_count, "elapsed": time.time() - t0}


# ═══════════════════════════════════════════════════════════════════════════
# 网络测试引擎（--network）
# ═══════════════════════════════════════════════════════════════════════════

def _kill_server(port=8765, rec_port=8766):
    for p in [port, rec_port]:
        try:
            if sys.platform == "win32":
                r = subprocess.run(f'netstat -ano | findstr :{p}', shell=True,
                                   capture_output=True, text=True)
                for line in r.stdout.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 5 and "LISTENING" in line:
                        subprocess.run(f"taskkill /F /PID {parts[-1]}", shell=True,
                                       capture_output=True)
            else:
                r = subprocess.run(f"lsof -ti :{p}", shell=True, capture_output=True, text=True)
                for pid in r.stdout.strip().split("\n"):
                    if pid: os.kill(int(pid), 9)
        except Exception:
            pass
    time.sleep(2)


def run_one_network(version_file: str, seed: int) -> dict:
    """WebSocket 网络模式运行一次测试。"""
    import asyncio
    _kill_server()

    server = subprocess.Popen(
        [sys.executable, "-u", str(PROJECT_DIR / "server" / "game_server.py"),
         "--host", "127.0.0.1", "--port", "8765", "--recording-port", "8766",
         "--speed", "100", "--seed", str(seed)],
        cwd=str(PROJECT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(3)
    empty = {"score": None, "orders": 0, "value": 0, "drop": 0, "collision": 0, "overtime": 0}
    if server.poll() is not None:
        return empty

    student = subprocess.Popen(
        [sys.executable, "-u", str(PROJECT_DIR / "student_template" / f"{version_file}.py")],
        cwd=str(PROJECT_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)

    async def _start():
        import websockets as _ws
        try:
            async with _ws.connect("ws://localhost:8765") as v:
                await v.send(json.dumps({"role": "viewer"}))
                await asyncio.wait_for(v.recv(), timeout=10)
                await v.send(json.dumps({"type": "start_game"}))
        except Exception:
            pass
    try:
        asyncio.run(_start())
    except Exception:
        pass

    empty = {"score": None, "orders": 0, "value": 0, "drop": 0, "collision": 0, "overtime": 0}
    metrics: dict = empty.copy()
    try:
        out, _ = asyncio.run(asyncio.wait_for(
            asyncio.to_thread(student.communicate, timeout=300), timeout=310))
        for line in (out or "").split("\n"):
            if "Final score:" in line:
                try: metrics["score"] = float(line.split("Final score:")[-1].strip())
                except ValueError: pass
    except Exception:
        student.kill()
    server.kill(); student.kill()
    return metrics


# ═══════════════════════════════════════════════════════════════════════════
# 版本发现
# ═══════════════════════════════════════════════════════════════════════════

def _discover_versions() -> dict:
    """自动扫描 strategy 模块中所有 Strategy 子类。"""
    import inspect
    from student_template import strategy as _mod
    versions = {}
    for name, cls in inspect.getmembers(_mod, inspect.isclass):
        if name.endswith('Strategy') and hasattr(cls, '_compute_commands'):
            short = name.replace('Strategy', '')
            versions[short] = cls
    return versions


# ═══════════════════════════════════════════════════════════════════════════
# 输出：表格 + 箱线图
# ═══════════════════════════════════════════════════════════════════════════

def _print_table(results: dict, versions: list[str], seeds: list[int]):
    """汇总表格 + 差距分析 + 按种子展开。"""
    # 汇总
    print(f"\n{'=' * 90}")
    print(f"{'Version':<10} {'Score':>8} {'Orders':>7} {'Value':>8} "
          f"{'Drop':>7} {'Coll':>8} {'OT':>7} {'vs Prev':>10}")
    print(f"{'-' * 90}")
    prev_avg = None
    for name in versions:
        rlist = results.get(name, {}).get("details", [])
        if not rlist: continue
        a = lambda k: sum(r.get(k, 0) or 0 for r in rlist) / len(rlist)
        diff = f"+{a('score') - prev_avg:.0f}" if prev_avg else "-"
        print(f"{name:<10} {a('score'):>8.1f} {a('orders'):>7.1f} "
              f"{a('value'):>8.0f} {a('drop'):>7.0f} "
              f"{a('collision'):>8.0f} {a('overtime'):>7.1f} {diff:>10}")
        prev_avg = a('score')

    # 差距检查
    print(f"\n{'=' * 90}")
    print("版本间差距 (目标 >= +150):")
    for i, name in enumerate(versions[1:], 1):
        prev = versions[i - 1]
        if name in results and prev in results:
            gap = results[name]["avg"] - results[prev]["avg"]
            print(f"  {prev:>5} -> {name:<5}: {gap:+7.0f}  "
                  f"[{'PASS' if gap >= 150 else 'FAIL'}]")

    # 按种子
    print(f"\n{'=' * 90}")
    header = f"{'Seed':>5}" + "".join(f" {n:>8}" for n in versions)
    print(header + "\n" + "-" * (6 + 9 * len(versions)))
    for si, seed in enumerate(seeds):
        row = f"{seed:>5}"
        for name in versions:
            d = results.get(name, {}).get("details", [])
            row += f" {d[si]['score']:>8.1f}" if si < len(d) else "      N/A"
        print(row)


def _plot_boxplot(results: dict, versions: list[str]):
    """箱线图（使用 seaborn + matplotlib 绘制）。"""
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import seaborn as sns
    except ImportError:
        print("[WARN] matplotlib/seaborn 未安装，跳过箱线图。pip install matplotlib seaborn")
        return

    # 转换为长格式（tidy data），seaborn 原生支持
    records: list[dict] = []
    for name in versions:
        for s in results.get(name, {}).get("scores", []):
            records.append({"Version": name, "Score": s})
    if not records:
        return

    import pandas as pd
    df = pd.DataFrame(records)

    # 主题 + 调色板
    sns.set_theme(style="whitegrid")
    palette = sns.color_palette("Greens", n_colors=len(versions))

    fig, ax = plt.subplots(figsize=(max(8, len(versions) * 1), 6))

    # 箱线图
    sns.boxplot(
        x="Version", y="Score", data=df, ax=ax,
        hue="Version", palette=palette, legend=False,
        width=0.5, linewidth=1.5,
        showmeans=True,
        meanprops={"marker": "D", "markerfacecolor": "#d32f2f", "markeredgecolor": "#d32f2f",
                    "markersize": 8, "zorder": 5},
        medianprops={"color": "black", "linewidth": 2},
        flierprops={"marker": "o", "markerfacecolor": "gray", "markersize": 5, "alpha": 0.5},
    )

    # 样本散点（jitter 避免重叠）
    sns.stripplot(
        x="Version", y="Score", data=df, ax=ax,
        color="#1565c0", size=6, jitter=False, alpha=0.9,
        edgecolor="white", linewidth=0.5, zorder=3,
    )

    ax.set_title("Strategy Performance Comparison", fontsize=14, fontweight="bold")
    ax.set_ylabel("Score")
    ax.set_xlabel("")

    # 版本间差距标注
    group = df.groupby("Version")["Score"]
    means = group.mean()
    if len(means) >= 2:
        y_max = df["Score"].max()
        for i in range(len(means) - 1):
            base_y = max(means.iloc[i], means.iloc[i + 1])
            ax.annotate(
                f"{means.iloc[i + 1] - means.iloc[i]:+.0f}",
                xy=(i + 0.5, base_y + y_max * 0.03),
                fontsize=9, ha="center", va="bottom", color="#d32f2f",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#ffebee", alpha=0.85),
            )

    sns.despine(left=True)
    plt.tight_layout()
    path = "batch_results.png"
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"箱线图已保存: {path}")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    p = argparse.ArgumentParser(description="批量测试工具", epilog="""
示例:
  python batch_test.py                        # 全部版本，10种子，表格+箱线图
  python batch_test.py --num-seeds 10         # 等差数列10个种子
  python batch_test.py --seeds 20 40 60       # 指定种子
  python batch_test.py --versions V2 V3 V3_1  # 指定版本
  python batch_test.py --no-plot              # 跳过箱线图
  python batch_test.py --network              # WebSocket 网络模式
  python batch_test.py --list                 # 列出可用版本
""", formatter_class=argparse.RawDescriptionHelpFormatter)

    p.add_argument("--seeds", type=int, nargs="+", default=DEFAULT_SEEDS,
                   help="种子列表（默认: 10,20,30,...,100 共10个）")
    p.add_argument("--num-seeds", type=int, metavar="N",
                   help="等差数列生成 N 个种子：10,20,...,10*N（覆盖 --seeds）")
    p.add_argument("--versions", type=str, nargs="+",
                   help="要测试的版本（默认: 自动发现全部）")
    p.add_argument("--network", action="store_true",
                   help="使用 WebSocket 网络模式（默认进程内直连）")
    p.add_argument("--no-plot", action="store_true",
                   help="跳过箱线图，只输出文本表格")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="逐 tick 显示进度")
    p.add_argument("--list", action="store_true",
                   help="列出可测试版本并退出")

    args = p.parse_args()

    # --list
    vm = _discover_versions()
    if args.list:
        print("可测试版本:")
        for n, c in sorted(vm.items()):
            print(f"  {n:<8}  {getattr(c, '__name__', c)}")
        return

    # 种子
    if args.num_seeds:
        seeds = [10 * (i + 1) for i in range(args.num_seeds)]
    else:
        seeds = args.seeds

    # 版本
    if args.versions:
        test_versions = [v for v in args.versions if v in vm]
    else:
        test_versions = sorted(vm.keys())

    if not test_versions:
        print("无可测试版本。--list 查看")
        sys.exit(1)

    # ── 执行测试 ──
    results = {}
    for name in test_versions:
        cls = vm[name]
        print(f"\n{'=' * 60}\nTesting {name}\n{'=' * 60}")

        scores, details = [], []
        for seed in seeds:
            r = run_one(cls, seed, verbose=args.verbose)
            details.append(r)
            scores.append(r.get("score") or 0)
            print(f"  seed={seed}: {r['score']:>8.1f}  orders={r.get('orders', '?'):>2}  "
                  f"val={r.get('value', 0):>6.0f}  coll={r.get('collision', 0):>6.0f}  "
                  f"OT={r.get('overtime', 0):>6.0f}  "
                  f"({r.get('ticks', '?')}t/{r.get('elapsed', 0):.1f}s)")

        avg = sum(scores) / max(len(scores), 1)
        print(f"  AVG: {avg:.1f}")
        results[name] = {"avg": avg, "scores": scores, "details": details}

    # ── 输出 ──
    _print_table(results, test_versions, seeds)
    if not args.no_plot:
        _plot_boxplot(results, test_versions)


if __name__ == "__main__":
    main()
