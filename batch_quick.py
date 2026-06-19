"""Batch quick test: test multiple versions on multiple seeds."""
import subprocess, sys, time, json, threading
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

def kill_servers():
    """Kill all processes on game ports."""
    try:
        subprocess.run(
            ["powershell", "-Command",
             "Get-NetTCPConnection -LocalPort 8765,8766 -ErrorAction SilentlyContinue | "
             "Where-Object State -eq Listen | ForEach-Object { "
             "Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=15)
    except Exception:
        pass
    time.sleep(5)

def test_one(student_file: str, seed: int, speed: int = 100) -> dict:
    """Run one test and return metrics."""
    student_path = PROJECT_DIR / "student_template" / f"{student_file}.py"
    if not student_path.exists():
        matches = list((PROJECT_DIR / "student_template").glob(f"**/{student_file}*"))
        if matches:
            student_path = matches[0]
        else:
            return {"seed": seed, "score": None, "error": f"File not found: {student_file}"}

    kill_servers()

    server = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "server" / "game_server.py"),
         "--host", "127.0.0.1", "--port", "8765", "--recording-port", "8766",
         "--speed", str(speed), "--seed", str(seed)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(5)

    if server.poll() is not None:
        _, err = server.communicate()
        return {"seed": seed, "score": None, "error": f"Server died: {err.decode(errors='replace')[:200] if err else 'no error output'}"}

    student = subprocess.Popen(
        [sys.executable, "-u", str(student_path)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    chunks = []
    def drain():
        try:
            for line in student.stdout:
                chunks.append(line)
        except Exception:
            pass
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    time.sleep(4)

    # Start game via viewer
    import asyncio, websockets
    async def start_game():
        try:
            async with websockets.connect("ws://127.0.0.1:8765") as ws:
                await ws.send(json.dumps({"role": "viewer"}))
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"type": "start_game"}))
                # Wait briefly for response
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass

    try:
        asyncio.run(start_game())
    except Exception:
        pass

    try:
        student.wait(timeout=180)
    except subprocess.TimeoutExpired:
        student.kill()

    t.join(timeout=5)
    out = "".join(chunks)
    server.kill()
    try:
        server.wait(timeout=10)
    except Exception:
        pass
    kill_servers()
    time.sleep(2)

    result = {"seed": seed, "score": None, "orders": 0, "value": 0, "drop": 0, "collision": 0, "overtime": 0}
    for line in out.split('\n'):
        if "Final score:" in line:
            try:
                result["score"] = float(line.split("Final score:")[-1].strip())
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
            if result["score"] is None:
                result["score"] = meta.get("final_score")
            result["orders"] = meta.get("completed_orders_count", 0)
            result["value"] = meta.get("completed_orders_value", 0)
            result["drop"] = meta.get("drop_reward_total", 0)
            result["collision"] = meta.get("collision_penalty", 0)
            result["overtime"] = meta.get("overtime_penalty", 0)
        except Exception:
            pass

    return result


if __name__ == "__main__":
    versions = sys.argv[1:] if len(sys.argv) > 1 else [
        "student_v1", "student_v2", "student_v3", "student_v3_1", "student_v3_2"
    ]
    seeds = [20, 40, 60, 80, 100]
    speed = 100

    all_results = {}
    for ver in versions:
        scores = []
        print(f"\n{'='*60}")
        print(f"Testing {ver}")
        print(f"{'='*60}")
        for seed in seeds:
            r = test_one(ver, seed, speed)
            all_results.setdefault(ver, []).append(r)
            if r["score"] is not None:
                scores.append(r["score"])
                print(f"  seed={seed:>3}: {r['score']:>8.1f}  orders={r['orders']:>2}  "
                      f"value={r['value']:>5.0f}  drop={r['drop']:>4.0f}  "
                      f"coll={r['collision']:>5.0f}  OT={r['overtime']:>5.0f}")
            else:
                print(f"  seed={seed:>3}: FAILED ({r.get('error', 'unknown')[:80]})")

        if scores:
            avg = sum(scores) / len(scores)
            print(f"  AVG: {avg:.1f} ({len(scores)}/{len(seeds)} seeds)")

    # Summary
    print(f"\n{'='*70}")
    print(f"{'Version':<20} {'Avg Score':>10} {'Orders':>7} {'Value':>7} {'Drop':>6} {'Coll':>7} {'OT':>7}")
    print(f"{'-'*70}")
    for ver in versions:
        results = all_results.get(ver, [])
        valid = [r for r in results if r["score"] is not None]
        if valid:
            avg = lambda k: sum(r[k] for r in valid) / len(valid)
            print(f"{ver:<20} {avg('score'):>10.1f} {avg('orders'):>7.1f} "
                  f"{avg('value'):>7.0f} {avg('drop'):>6.0f} "
                  f"{avg('collision'):>7.0f} {avg('overtime'):>7.1f}")
        else:
            print(f"{ver:<20} {'N/A':>10}")
