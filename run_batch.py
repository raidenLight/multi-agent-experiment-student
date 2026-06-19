"""One-shot batch test: tests V1,V2,V3,V3_1,V3_2 on 5 seeds and prints summary."""
import subprocess, sys, time, json, threading, os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SEEDS = [20, 40, 60, 80, 100]
SPEED = 100

def kill_all_python():
    """Kill ALL python processes except this one."""
    import signal
    my_pid = os.getpid()
    # Use wmic which works reliably on Windows
    try:
        result = subprocess.run(
            'wmic process where "name=\'python.exe\' and processid!=' + str(my_pid) + '" get processid',
            shell=True, capture_output=True, text=True, timeout=10)
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.isdigit() and int(line) != my_pid:
                try:
                    os.kill(int(line), signal.SIGTERM)
                except Exception:
                    pass
    except Exception:
        pass
    time.sleep(3)

def run_test(name, seed):
    """Run one game with given strategy and seed. Returns score dict."""
    student_path = None
    for pattern in [f"student_template/{name}.py",
                    f"student_template/**/{name}.py"]:
        matches = list(PROJECT_DIR.glob(pattern))
        if matches:
            student_path = matches[0]
            break
    if not student_path:
        return {"seed": seed, "score": None, "error": f"not found: {name}"}

    out = {"seed": seed, "score": None, "orders": 0, "value": 0,
           "drop": 0, "collision": 0, "overtime": 0}

    # Kill old processes
    kill_all_python()
    time.sleep(5)

    # Start server
    server = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "server" / "game_server.py"),
         "--host", "127.0.0.1", "--port", "8765",
         "--recording-port", "8766", "--speed", str(SPEED),
         "--seed", str(seed)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(5)

    if server.poll() is not None:
        _, err = server.communicate()
        out["error"] = f"server died: {err.decode(errors='replace')[:150] if err else 'no output'}"
        return out

    # Start student
    student = subprocess.Popen(
        [sys.executable, "-u", str(student_path)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    lines = []
    def drain():
        try:
            for line in student.stdout:
                lines.append(line)
        except Exception:
            pass
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    time.sleep(4)

    # Start game via websocket viewer
    import asyncio, websockets
    async def start():
        try:
            async with websockets.connect("ws://127.0.0.1:8765") as ws:
                await ws.send(json.dumps({"role": "viewer"}))
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"type": "start_game"}))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass
    try:
        asyncio.run(start())
    except Exception:
        pass

    # Wait for game to finish
    try:
        student.wait(timeout=180)
    except subprocess.TimeoutExpired:
        student.kill()
        out["error"] = "timeout"
        server.kill()
        return out

    t.join(timeout=5)
    output = "".join(lines)

    # Kill server
    server.kill()
    try:
        server.wait(timeout=10)
    except Exception:
        pass

    # Parse score
    for line in output.split('\n'):
        if "Final score:" in line:
            try:
                out["score"] = float(line.split("Final score:")[-1].strip())
            except ValueError:
                pass

    # Fallback: recording file
    recordings = sorted(
        (PROJECT_DIR / "recordings").glob("game_*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if recordings:
        try:
            with open(recordings[0], encoding="utf-8") as f:
                rec = json.load(f)
            meta = rec.get("metadata", {})
            if out["score"] is None:
                out["score"] = meta.get("final_score")
            out.update({k: meta.get(k, 0) for k in
                       ["orders", "value", "drop", "collision", "overtime"]})
            out["orders"] = meta.get("completed_orders_count", 0)
            out["value"] = meta.get("completed_orders_value", 0)
            out["drop"] = meta.get("drop_reward_total", 0)
            out["collision"] = meta.get("collision_penalty", 0)
            out["overtime"] = meta.get("overtime_penalty", 0)
        except Exception:
            pass

    return out


if __name__ == "__main__":
    versions = ["student_v1", "student_v2", "student_v3",
                "student_v3_1", "student_v3_2"]

    print(f"Batch test: {len(versions)} versions x {len(SEEDS)} seeds")
    print(f"Speed: {SPEED}x")
    print()

    all_results = {}
    for ver in versions:
        print(f"--- {ver} ---")
        results = []
        for seed in SEEDS:
            r = run_test(ver, seed)
            results.append(r)
            if r["score"] is not None:
                print(f"  seed={seed:>3}: {r['score']:>8.1f}  orders={r['orders']:>2}  "
                      f"val={r['value']:>5.0f}  drop={r['drop']:>4.0f}  "
                      f"coll={r['collision']:>5.0f}  OT={r['overtime']:>5.0f}")
            else:
                err = r.get('error', 'unknown')[:80]
                print(f"  seed={seed:>3}: FAILED ({err})")
        all_results[ver] = results

    # Summary
    print(f"\n{'='*75}")
    print(f"{'Version':<16} {'Score':>8} {'Orders':>7} {'Value':>7} "
          f"{'Drop':>6} {'Coll':>7} {'OT':>7} {'Diff':>8}")
    print(f"{'-'*75}")

    prev_avg = None
    for ver in versions:
        valid = [r for r in all_results[ver] if r["score"] is not None]
        if valid:
            avg = lambda k: sum(r[k] for r in valid) / len(valid)
            diff = f"+{avg('score')-prev_avg:.0f}" if prev_avg else "-"
            print(f"{ver:<16} {avg('score'):>8.1f} {avg('orders'):>7.1f} "
                  f"{avg('value'):>7.0f} {avg('drop'):>6.0f} "
                  f"{avg('collision'):>7.0f} {avg('overtime'):>7.1f} {diff:>8}")
            prev_avg = avg('score')
        else:
            print(f"{ver:<16} {'N/A':>8}")

    print()
    print("Done. Results saved in memory (no file output).")
