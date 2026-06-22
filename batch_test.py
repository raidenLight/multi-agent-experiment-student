"""Batch-test student strategies with seed/repeat summaries.

Examples:
    python batch_test.py student_v9 100
    python batch_test.py student_v9 100 --repeat 5
    python batch_test.py student_v7 100 200 300 --repeat 3
    python batch_test.py --latest 100 --repeat 5
    python batch_test.py --all --runs 5 --repeat 2
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets


PROJECT_DIR = Path(__file__).resolve().parent
STUDENT_DIR = PROJECT_DIR / "student_template"
RECORDINGS_DIR = PROJECT_DIR / "recordings"
PORT = 8765
RECORDING_PORT = 8766
SPEED = 100
TIMEOUT = 300

METRIC_KEYS = [
    "orders_completed",
    "order_value",
    "drop_reward",
    "collision_penalty",
    "overtime_penalty",
]


def console_safe(text: object, limit: int | None = None) -> str:
    """Return text that can be printed in the current Windows console."""

    value = str(text)
    if limit is not None:
        value = value[:limit]
    encoding = sys.stdout.encoding or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding, errors="replace")


def decode_bytes(data: bytes | None, limit: int | None = None) -> str:
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    return console_safe(text, limit)


def resolve_output_path(value: str | None, default_name: str) -> Path:
    path = Path(value) if value else PROJECT_DIR / default_name
    if not path.is_absolute():
        path = PROJECT_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def version_sort_key(version: str) -> tuple[int, int | str]:
    match = re.fullmatch(r"student_v(\d+)", version)
    if match:
        return (0, int(match.group(1)))
    if version == "student_vn":
        return (1, 0)
    return (2, version)


def discover_versions() -> list[str]:
    versions = []
    for path in STUDENT_DIR.glob("student_v*.py"):
        if re.fullmatch(r"student_v\d+", path.stem):
            versions.append(path.stem)
    if (STUDENT_DIR / "student_vn.py").exists():
        versions.append("student_vn")
    return sorted(set(versions), key=version_sort_key)


def discover_latest_version() -> str | None:
    numeric_versions = []
    for version in discover_versions():
        match = re.fullmatch(r"student_v(\d+)", version)
        if match:
            numeric_versions.append((int(match.group(1)), version))
    if not numeric_versions:
        return None
    return max(numeric_versions)[1]


def kill_existing_server() -> None:
    """Kill processes that are listening on the default test ports."""

    killed: set[str] = set()
    for port in [PORT, RECORDING_PORT]:
        try:
            if sys.platform == "win32":
                result = subprocess.run(
                    f"netstat -ano | findstr :{port}",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and "LISTENING" in line:
                        pid = parts[-1]
                        if pid in killed:
                            continue
                        killed.add(pid)
                        subprocess.run(
                            f"taskkill /F /PID {pid}",
                            shell=True,
                            capture_output=True,
                        )
                        print(f"  Killed PID {pid} on port {port}")
            else:
                result = subprocess.run(
                    f"lsof -ti :{port}",
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                for pid in result.stdout.strip().splitlines():
                    if pid and pid not in killed:
                        killed.add(pid)
                        os.kill(int(pid), 9)
        except Exception:
            pass
    time.sleep(1)


def empty_metrics(version_file: str, seed: int, run_index: int) -> dict[str, Any]:
    return {
        "version": version_file,
        "seed": seed,
        "run": run_index,
        "status": "pending",
        "score": None,
        "file": None,
        "orders_completed": 0,
        "order_value": 0,
        "drop_reward": 0,
        "collision_penalty": 0,
        "overtime_penalty": 0,
        "error": None,
    }


def terminate_process(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.kill()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def parse_score_from_output(output: str | None) -> float | None:
    for line in (output or "").splitlines():
        if "Final score:" not in line:
            continue
        try:
            return float(line.split("Final score:")[-1].strip())
        except ValueError:
            continue
    return None


def recording_name(version_file: str, seed: int, run_index: int, repeat: int) -> str:
    suffix = f"_run{run_index}" if repeat > 1 else ""
    return f"{Path(version_file).stem}_seed{seed}{suffix}.json"


def attach_latest_recording(
    metrics: dict[str, Any],
    version_file: str,
    seed: int,
    run_index: int,
    repeat: int,
    known_recordings: set[Path],
    started_at: float,
) -> None:
    RECORDINGS_DIR.mkdir(exist_ok=True)
    candidates = []
    for path in RECORDINGS_DIR.glob("*.json"):
        resolved = path.resolve()
        if resolved not in known_recordings or path.stat().st_mtime >= started_at:
            candidates.append(path)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return

    new_file = recording_name(version_file, seed, run_index, repeat)
    new_path = RECORDINGS_DIR / new_file
    if new_path.exists():
        new_path.unlink()
    candidates[0].rename(new_path)
    metrics["file"] = new_file

    try:
        with open(new_path, encoding="utf-8") as handle:
            recording = json.load(handle)
        meta = recording.get("metadata", {})
        metrics["score"] = metrics["score"] if metrics["score"] is not None else meta.get("final_score")
        metrics["orders_completed"] = meta.get("completed_orders_count", 0)
        metrics["order_value"] = meta.get("completed_orders_value", 0)
        metrics["drop_reward"] = meta.get("drop_reward_total", 0)
        metrics["collision_penalty"] = meta.get("collision_penalty", 0)
        metrics["overtime_penalty"] = meta.get("overtime_penalty", 0)
        if metrics["score"] is not None and metrics["status"] in {"pending", "error"}:
            metrics["status"] = "ok"
    except Exception as exc:
        metrics["error"] = f"recording_read_failed: {type(exc).__name__}"


async def run_one(version_file: str, seed: int, run_index: int, repeat: int) -> dict[str, Any]:
    """Run one version/seed/repeat trial and return metrics."""

    label = f"  seed={seed}"
    if repeat > 1:
        label += f" run={run_index}/{repeat}"
    print(f"{label} ...", end=" ", flush=True)

    kill_existing_server()
    RECORDINGS_DIR.mkdir(exist_ok=True)
    known_recordings = {path.resolve() for path in RECORDINGS_DIR.glob("*.json")}
    started_at = time.time()
    metrics = empty_metrics(version_file, seed, run_index)
    server: subprocess.Popen[Any] | None = None
    student: subprocess.Popen[Any] | None = None

    try:
        server = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_DIR / "server" / "game_server.py"),
                "--port",
                str(PORT),
                "--recording-port",
                str(RECORDING_PORT),
                "--speed",
                str(SPEED),
                "--seed",
                str(seed),
            ],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        await asyncio.sleep(2)
        if server.poll() is not None:
            _, err = server.communicate()
            metrics["status"] = "server_died"
            metrics["error"] = decode_bytes(err, 1000)
            print(f"SERVER_DIED({console_safe(metrics['error'], 140)})", end=" ")
            return metrics

        student_path = PROJECT_DIR / "student_template" / f"{version_file}.py"
        student = subprocess.Popen(
            [sys.executable, str(student_path)],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        await asyncio.sleep(3)

        try:
            async with websockets.connect(f"ws://localhost:{PORT}") as viewer:
                await viewer.send(json.dumps({"role": "viewer"}))
                await asyncio.wait_for(viewer.recv(), timeout=10)
                await viewer.send(json.dumps({"type": "start_game"}))
        except Exception as exc:
            metrics["status"] = "viewer_error"
            metrics["error"] = type(exc).__name__
            print(f"VIEWER_ERR({type(exc).__name__})", end=" ")
            return metrics

        try:
            out, _ = await asyncio.wait_for(
                asyncio.to_thread(student.communicate, timeout=TIMEOUT),
                timeout=TIMEOUT + 10,
            )
        except (asyncio.TimeoutError, subprocess.TimeoutExpired):
            metrics["status"] = "timeout"
            print("TIMEOUT", end=" ")
            return metrics

        metrics["score"] = parse_score_from_output(out)
        metrics["status"] = "ok" if metrics["score"] is not None else "score_pending"
        if metrics["score"] is not None:
            print(f"{metrics['score']:.0f}pts", end=" ", flush=True)
        else:
            print("score_pending", end=" ", flush=True)

    except Exception as exc:
        metrics["status"] = "error"
        metrics["error"] = type(exc).__name__
        print(f"ERR({type(exc).__name__})", end=" ")
    finally:
        terminate_process(student)
        terminate_process(server)
        attach_latest_recording(metrics, version_file, seed, run_index, repeat, known_recordings, started_at)

    return metrics


def valid_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in results if item.get("score") is not None]


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def summarize_seed(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = valid_results(results)
    scores = [float(item["score"]) for item in valid]
    summary: dict[str, Any] = {
        "runs": len(results),
        "valid_runs": len(valid),
        "scores": scores,
        "avg_score": average(scores),
        "max_score": max(scores) if scores else None,
        "best_run": None,
    }
    if valid:
        best = max(valid, key=lambda item: float(item["score"]))
        summary["best_run"] = best["run"]
    for key in METRIC_KEYS:
        values = [float(item.get(key, 0)) for item in valid]
        summary[f"avg_{key}"] = average(values)
    return summary


def summarize_version(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_seed: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        by_seed.setdefault(str(item["seed"]), []).append(item)

    seed_summaries = {seed: summarize_seed(items) for seed, items in sorted(by_seed.items(), key=lambda kv: int(kv[0]))}
    valid = valid_results(results)
    run_scores = [float(item["score"]) for item in valid]
    seed_avg_scores = [
        float(summary["avg_score"])
        for summary in seed_summaries.values()
        if summary["avg_score"] is not None
    ]
    seed_max_scores = [
        float(summary["max_score"])
        for summary in seed_summaries.values()
        if summary["max_score"] is not None
    ]
    best_run = max(valid, key=lambda item: float(item["score"])) if valid else None

    return {
        "runs": len(results),
        "valid_runs": len(valid),
        "seeds": seed_summaries,
        "run_avg_score": average(run_scores),
        "run_max_score": max(run_scores) if run_scores else None,
        "seed_avg_score_mean": average(seed_avg_scores),
        "seed_max_score_mean": average(seed_max_scores),
        "best": best_run,
    }


def format_number(value: Any, width: int = 8, decimals: int = 1) -> str:
    if value is None:
        return f"{'N/A':>{width}}"
    return f"{float(value):>{width}.{decimals}f}"


def print_run_result(result: dict[str, Any]) -> None:
    if result.get("score") is None:
        print(f"  FAILED status={result.get('status')} error={console_safe(result.get('error') or '')}")
        return
    print(
        f"  score={result['score']:.1f}"
        f"  orders={result.get('orders_completed', '?')}"
        f"  value={float(result.get('order_value', 0)):.0f}"
        f"  drop={float(result.get('drop_reward', 0)):.0f}"
        f"  collision={float(result.get('collision_penalty', 0)):.0f}"
        f"  overtime={float(result.get('overtime_penalty', 0)):.0f}"
        f"  -> {result.get('file')}"
    )


def print_seed_summary(seed: int, summary: dict[str, Any]) -> None:
    print(
        f"  seed {seed} summary:"
        f" avg={format_number(summary['avg_score'], width=7).strip()}"
        f" max={format_number(summary['max_score'], width=7).strip()}"
        f" valid={summary['valid_runs']}/{summary['runs']}"
        f" best_run={summary['best_run'] or 'N/A'}"
    )


def print_final_summary(summary: dict[str, Any]) -> None:
    header = (
        f"{'Version':<14} {'RunAvg':>9} {'RunMax':>9} {'SeedAvgMean':>12}"
        f" {'SeedMaxMean':>12} {'Runs':>7} {'Seeds':>7} {'Best':>9}"
    )
    print(f"\n{'=' * len(header)}")
    print(header)
    print("-" * len(header))
    for version, data in summary.items():
        seed_count = len(data["seeds"])
        best_score = data["best"]["score"] if data.get("best") else None
        print(
            f"{version:<14}"
            f"{format_number(data['run_avg_score'], 9)}"
            f"{format_number(data['run_max_score'], 9)}"
            f"{format_number(data['seed_avg_score_mean'], 12)}"
            f"{format_number(data['seed_max_score_mean'], 12)}"
            f"{data['valid_runs']:>7}/{data['runs']:<0}"
            f"{seed_count:>7}"
            f"{format_number(best_score, 9)}"
        )


def save_csv(path: Path, all_results: dict[str, list[dict[str, Any]]]) -> None:
    fieldnames = [
        "version",
        "seed",
        "run",
        "status",
        "score",
        "orders_completed",
        "order_value",
        "drop_reward",
        "collision_penalty",
        "overtime_penalty",
        "file",
        "error",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for results in all_results.values():
            for item in results:
                writer.writerow({key: item.get(key) for key in fieldnames})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch-test student strategies.")
    parser.add_argument("version", nargs="?", default=None, help="Version, e.g. student_v9")
    parser.add_argument("seeds", nargs="*", type=int, help="Seed list, e.g. 100 200 300")
    parser.add_argument("--all", action="store_true", help="Test all discovered student_v*.py plus student_vn.py")
    parser.add_argument("--latest", action="store_true", help="Test the highest discovered student_vN.py")
    parser.add_argument("--list", action="store_true", help="List discovered testable versions and exit")
    parser.add_argument("--runs", type=int, default=5, help="Default seed count when no seeds are supplied")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat count per seed")
    parser.add_argument("--output", default="batch_results.json", help="JSON result file path")
    parser.add_argument("--csv", default=None, help="Optional flat CSV result file path")
    return parser


def normalize_seed_args(args: argparse.Namespace) -> list[int]:
    """Handle commands like ``--all 1 2 3`` where argparse fills version=1."""

    seeds = list(args.seeds)
    if (args.all or args.latest) and args.version is not None:
        try:
            seeds.insert(0, int(args.version))
            args.version = None
        except ValueError:
            pass
    return seeds


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.list:
        print(f"Discovered versions: {discover_versions()}")
        print(f"Latest version:      {discover_latest_version()}")
        return

    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")

    seed_args = normalize_seed_args(args)

    if args.all:
        versions = discover_versions()
    elif args.latest:
        latest = discover_latest_version()
        if latest is None:
            raise SystemExit("No student_vN.py files found")
        versions = [latest]
    elif args.version:
        versions = [args.version]
    else:
        print(f"Discovered versions: {discover_versions()}")
        print(f"Latest version:      {discover_latest_version()}")
        parser.print_help()
        return

    missing = [version for version in versions if not (STUDENT_DIR / f"{version}.py").exists()]
    if missing:
        raise SystemExit(f"Missing student files: {missing}")

    seeds = seed_args if seed_args else list(range(20, args.runs * 20 + 1, 20))

    print(f"Versions: {versions}")
    print(f"Seeds:    {seeds}")
    print(f"Repeat:   {args.repeat}")
    print(f"Speed:    {SPEED}x")
    print()

    all_results: dict[str, list[dict[str, Any]]] = {}
    summary: dict[str, Any] = {}
    for version in versions:
        print(f"[{version}]")
        version_results: list[dict[str, Any]] = []
        for seed in seeds:
            seed_results: list[dict[str, Any]] = []
            for run_index in range(1, args.repeat + 1):
                result = await run_one(version, seed, run_index, args.repeat)
                version_results.append(result)
                seed_results.append(result)
                print_run_result(result)
            print_seed_summary(seed, summarize_seed(seed_results))
        all_results[version] = version_results
        summary[version] = summarize_version(version_results)
        await asyncio.sleep(1)

    print_final_summary(summary)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_dir": str(PROJECT_DIR),
        "speed": SPEED,
        "timeout": TIMEOUT,
        "versions": versions,
        "seeds": seeds,
        "repeat": args.repeat,
        "results": all_results,
        "summary": summary,
    }
    output_path = resolve_output_path(args.output, "batch_results.json")
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"\nJSON saved to: {output_path}")

    if args.csv:
        csv_path = resolve_output_path(args.csv, "batch_results.csv")
        save_csv(csv_path, all_results)
        print(f"CSV saved to:  {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
