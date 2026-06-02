"""批量测试脚本：多随机种子下自动测试策略性能并统计。

用法:
    python batch_test.py student_v4               # 默认 3 个随机种子
    python batch_test.py student_v4 100 200 300   # 指定种子
    python batch_test.py --all                    # 测试所有版本
    python batch_test.py --all  --runs 5          # 所有版本，5 个随机种子

注意：运行前确保 8765/8766 端口未被占用（关闭已有的 game_server）。
"""

import asyncio
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import websockets

PROJECT_DIR = Path(__file__).resolve().parent
PORT = 8765
RECORDING_PORT = 8766
SPEED = 100
TIMEOUT = 300  # 300s仿真 * 1000x ≈ CPU耗时6-9s


# ======================================================================
# 端口清理
# ======================================================================

def kill_existing_server():
    """杀掉占用默认端口的旧 server 进程。"""
    for port in [PORT, RECORDING_PORT]:
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    f'netstat -ano | findstr :{port}',
                    shell=True, capture_output=True, text=True)
                for line in result.stdout.strip().split("\n"):
                    parts = line.split()
                    if len(parts) >= 5 and "LISTENING" in line:
                        pid = parts[-1]
                        subprocess.run(f"taskkill /F /PID {pid}",
                                       shell=True, capture_output=True)
                        print(f"  Killed PID {pid} on port {port}")
            else:
                result = subprocess.run(
                    f"lsof -ti :{port}", shell=True,
                    capture_output=True, text=True)
                for pid in result.stdout.strip().split("\n"):
                    if pid:
                        os.kill(int(pid), 9)
        except Exception:
            pass
    time.sleep(1)


# ======================================================================
# 单次测试
# ======================================================================

async def run_one(version_file: str, seed: int) -> dict:
    """运行一次测试，返回 {seed, score, file}。"""
    print(f"  seed={seed} ...", end=" ", flush=True)

    # 杀掉旧进程
    kill_existing_server()

    # 1. 启动 server
    server = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "server" / "game_server.py"),
         "--port", str(PORT),
         "--recording-port", str(RECORDING_PORT),
         "--speed", str(SPEED),
         "--seed", str(seed)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # 等 server 启动
    await asyncio.sleep(2)
    if server.poll() is not None:
        _, err = server.communicate()
        print(f"SERVER_DIED({err.decode(errors='replace')[:100]})", end=" ")
        return {"seed": seed, "score": None, "file": None,
                "orders_completed": 0, "order_value": 0,
                "drop_reward": 0, "collision_penalty": 0, "overtime_penalty": 0}

    # 2. 启动 student
    student = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "student_template" / f"{version_file}.py")],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # 等 student 连接 server
    await asyncio.sleep(3)

    metrics = {"seed": seed, "score": None, "file": None,
               "orders_completed": 0, "order_value": 0,
               "drop_reward": 0, "collision_penalty": 0, "overtime_penalty": 0}
    try:
        # 3. 连接 viewer，发 start_game 后立即断开
        import websockets as _ws
        async with _ws.connect(f"ws://localhost:{PORT}") as viewer:
            await viewer.send(json.dumps({"role": "viewer"}))
            msg = json.loads(await asyncio.wait_for(viewer.recv(), timeout=10))
            await viewer.send(json.dumps({"type": "start_game"}))
        # viewer 已断开，游戏在后台运行

        # 4. 等 student 进程结束
        try:
            out, _ = await asyncio.wait_for(
                asyncio.to_thread(student.communicate, timeout=TIMEOUT),
                timeout=TIMEOUT + 10)
        except asyncio.TimeoutError:
            student.kill()
            print("TIMEOUT", end=" ")
            return metrics

        # 从 student 输出提取分数
        for line in (out or "").split("\n"):
            if "Final score:" in line:
                try:
                    metrics["score"] = float(
                        line.split("Final score:")[-1].strip())
                except ValueError:
                    pass
        print(f"{metrics['score']:.0f}pts", end=" ", flush=True)

    except Exception as e:
        print(f"ERR({type(e).__name__})", end=" ")
        student.kill()

    server.kill()
    try:
        server.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    try:
        student.wait(timeout=3)
    except subprocess.TimeoutExpired:
        student.kill()

    # 5. 重命名录像，并从中提取完整指标
    version_name = Path(version_file).stem
    recordings_dir = PROJECT_DIR / "recordings"
    recordings = sorted(
        recordings_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    new_file = None
    if recordings:
        new_file = f"{version_name}_seed{seed}.json"
        new_path = recordings_dir / new_file
        if new_path.exists():
            new_path.unlink()
        recordings[0].rename(new_path)

        # 从录像文件读取完整指标
        try:
            with open(new_path, encoding="utf-8") as f:
                rec = json.load(f)
            meta = rec.get("metadata", {})
            metrics["score"] = metrics["score"] or meta.get("final_score")
            metrics["orders_completed"] = meta.get("completed_orders_count", 0)
            metrics["order_value"] = meta.get("completed_orders_value", 0)
            metrics["drop_reward"] = meta.get("drop_reward_total", 0)
            metrics["collision_penalty"] = meta.get("collision_penalty", 0)
            metrics["overtime_penalty"] = meta.get("overtime_penalty", 0)
        except Exception:
            pass

    metrics["file"] = new_file
    return metrics


# ======================================================================
# 批量
# ======================================================================

async def main():
    import argparse
    p = argparse.ArgumentParser(description="批量测试策略性能")
    p.add_argument("version", nargs="?", default=None, help="版本 e.g. student_v4")
    p.add_argument("seeds", nargs="*", type=int, help="种子列表")
    p.add_argument("--all", action="store_true", help="测试全部 V1-V5+VN")
    p.add_argument("--runs", type=int, default=3, help="随机种子数量")
    args = p.parse_args()

    if args.all:
        versions = [f"student_v{i}" for i in range(0, 6)] + ["student_vn"]
    elif args.version:
        versions = [args.version]
    else:
        p.print_help()
        return

    seeds = args.seeds if args.seeds else list(range(1, args.runs + 1))

    print(f"Versions: {versions}")
    print(f"Seeds:    {seeds}")
    print(f"Speed:    {SPEED}x")
    print()

    all_results = {}
    for ver in versions:
        print(f"[{ver}]")
        results = []
        for sd in seeds:
            r = await run_one(ver, sd)
            results.append(r)
            if r.get("score") is not None:
                print(f"  score={r['score']:.1f}  orders={r.get('orders_completed','?')}"
                      f"  value={r.get('order_value','?'):.0f}  drop={r.get('drop_reward','?'):.0f}"
                      f"  collision={r.get('collision_penalty','?'):.0f}  overtime={r.get('overtime_penalty','?'):.0f}"
                      f"  -> {r['file']}")
            else:
                print("  FAILED")
        all_results[ver] = results
        await asyncio.sleep(1)

    # 汇总表
    header = f"{'Version':<14} {'Score':>8} {'Orders':>7} {'Value':>7} {'Drop':>7} {'Coll':>7} {'OT':>7} {'Runs':>5}"
    print(f"\n{'='*len(header)}")
    print(header)
    print(f"{'-'*len(header)}")
    for ver, results in all_results.items():
        valid = [r for r in results if r.get("score") is not None]
        if not valid:
            print(f"{ver:<14} {'N/A':>8} {'N/A':>7} {'N/A':>7} {'N/A':>7} {'N/A':>7} {'N/A':>7} {0:>5}")
            continue
        def avg(k): return sum(r[k] for r in valid) / len(valid)
        print(f"{ver:<14} {avg('score'):>8.1f} {avg('orders_completed'):>7.1f} "
              f"{avg('order_value'):>7.0f} {avg('drop_reward'):>7.0f} "
              f"{avg('collision_penalty'):>7.0f} {avg('overtime_penalty'):>7.1f} "
              f"{len(valid):>5}")

    # 保存汇总 JSON
    summary = {}
    for ver, results in all_results.items():
        valid = [r for r in results if r.get("score") is not None]
        if valid:
            def avg(k): return sum(r[k] for r in valid) / len(valid)
            summary[ver] = {
                "seeds": [r["seed"] for r in valid],
                "avg_score": avg("score"),
                "avg_orders": avg("orders_completed"),
                "avg_order_value": avg("order_value"),
                "avg_drop_reward": avg("drop_reward"),
                "avg_collision": avg("collision_penalty"),
                "avg_overtime": avg("overtime_penalty"),
                "runs": len(valid),
            }
    summary_file = PROJECT_DIR / "batch_results.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\nSummary saved to: {summary_file}")


if __name__ == "__main__":
    asyncio.run(main())
