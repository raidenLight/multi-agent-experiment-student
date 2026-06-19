"""Quick single-test harness: runs one strategy on one seed and reports score."""
import subprocess, sys, time, json, re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

def test_one(student_file: str, seed: int, speed: int = 100) -> dict:
    """Run one test and return metrics dict."""
    student_path = PROJECT_DIR / "student_template" / f"{student_file}.py"
    if not student_path.exists():
        student_path = PROJECT_DIR / "student_template" / student_file
    if not student_path.exists():
        # Try glob
        matches = list((PROJECT_DIR / "student_template").glob(f"**/{student_file}*"))
        if matches:
            student_path = matches[0]
        else:
            raise FileNotFoundError(f"Cannot find {student_file}")

"""Quick single-test harness: runs one strategy on one seed and reports score."""
import subprocess, sys, time, json, threading, os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent

def test_one(student_file: str, seed: int, speed: int = 100) -> dict:
    """Run one test and return metrics dict."""
    student_path = PROJECT_DIR / "student_template" / f"{student_file}.py"
    if not student_path.exists():
        student_path = PROJECT_DIR / "student_template" / student_file
    if not student_path.exists():
        matches = list((PROJECT_DIR / "student_template").glob(f"**/{student_file}*"))
        if matches:
            student_path = matches[0]
        else:
            raise FileNotFoundError(f"Cannot find {student_file}")

    port = 8765
    rec_port = 8766

    # Kill any processes using our ports
    kill_script = PROJECT_DIR / "kill_servers.ps1"
    if kill_script.exists():
        subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(kill_script)],
            capture_output=True, timeout=15)
        time.sleep(5)

    # Start game server (unbuffered to capture output)
    senv = os.environ.copy()
    senv["PYTHONUNBUFFERED"] = "1"
    print(f"  Starting server on port {port} (seed={seed})...", flush=True)
    server = subprocess.Popen(
        [sys.executable, "-u", str(PROJECT_DIR / "server" / "game_server.py"),
         "--host", "127.0.0.1",
         "--port", str(port),
         "--recording-port", str(rec_port),
         "--speed", str(speed),
         "--seed", str(seed)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=senv,
    )
    time.sleep(5)

    if server.poll() is not None:
        out, err = server.communicate()
        print(f"  Server died! out={out.decode(errors='replace')[:200] if out else ''} err={err.decode(errors='replace')[:200] if err else ''}")
        return _empty(seed)
    print("  Server running.", flush=True)

    # Start student with threaded output capture
    print(f"  Starting student ({student_path.name})...", flush=True)
    student = subprocess.Popen(
        [sys.executable, "-u", str(student_path)],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    chunks = []
    def _drain():
        try:
            for line in student.stdout:
                chunks.append(line)
        except Exception:
            pass
    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    time.sleep(4)

    # Connect viewer and start game
    import asyncio
    import websockets

    async def start_game():
        try:
            async with websockets.connect("ws://127.0.0.1:8765") as ws:
                await ws.send(json.dumps({"role": "viewer"}))
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"type": "start_game"}))
        except Exception as e:
            print(f"  Viewer error: {e}", flush=True)

    try:
        asyncio.run(start_game())
    except Exception as e:
        print(f"  Start game error: {e}", flush=True)

    # Wait for game to finish
    print("  Waiting for game to finish...", flush=True)
    try:
        student.wait(timeout=180)
    except subprocess.TimeoutExpired:
        student.kill()
        print("  TIMEOUT!", flush=True)
        server.kill()
        return _empty(seed)

    drain.join(timeout=5)
    out = "".join(chunks)
    # Give server time to detect disconnect and write recording
    time.sleep(4)
    # Show first 300 and last 300 chars
    head = out.strip()[:300].replace('\n', '\\n')
    tail = out.strip()[-300:].replace('\n', '\\n')
    print(f"  Student output ({len(out)} chars):", flush=True)
    print(f"    HEAD: {head}", flush=True)
    print(f"    TAIL: {tail}", flush=True)

    # Kill server and get its output
    server.kill()
    try:
        server_out, server_err = server.communicate(timeout=10)
    except Exception:
        server_out, server_err = b"", b""
        server.kill()
    server_text = ""
    if server_err:
        server_text = server_err.decode(errors='replace')
    if server_out:
        server_text += server_out.decode(errors='replace')
    if server_text.strip():
        # Print first 300 and last 1000 chars of server output
        shead = server_text.strip()[:300]
        stail = server_text.strip()[-1000:]
        print(f"  Server output ({len(server_text)} chars):", flush=True)
        print(f"    HEAD: {shead}", flush=True)
        if len(server_text) > 1300:
            print(f"    TAIL: ...{stail}", flush=True)
    else:
        print(f"  Server: NO OUTPUT", flush=True)

    # Parse score
    result = _empty(seed)
    for line in out.split('\n'):
        if "Final score:" in line:
            try:
                result["score"] = float(line.split("Final score:")[-1].strip())
            except ValueError:
                pass

    # Try recording file as fallback
    recordings = sorted(
        (PROJECT_DIR / "recordings").glob("game_*.json"),
        key=lambda p: p.stat().st_mtime, reverse=True
    )
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


def _empty(seed):
    return {"seed": seed, "score": None, "orders": 0, "value": 0,
            "drop": 0, "collision": 0, "overtime": 0}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python quick_test.py <student_name> [seed] [speed]")
        print("Example: python quick_test.py student_v2 20 100")
        sys.exit(1)

    name = sys.argv[1]
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    speed = int(sys.argv[3]) if len(sys.argv) > 3 else 100

    print(f"Testing: {name} seed={seed} speed={speed}x")
    r = test_one(name, seed, speed)
    if r["score"] is not None:
        print(f"Score: {r['score']:.1f} | Orders: {r['orders']} | "
              f"Value: {r['value']:.0f} | Drop: {r['drop']:.0f} | "
              f"Collision: {r['collision']:.0f} | OT: {r['overtime']:.0f}")
    else:
        print("FAILED - no score")
