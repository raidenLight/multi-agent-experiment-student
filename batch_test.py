"""Batch-test student strategies with seed/repeat summaries.

Examples:
    python batch_test.py
    python batch_test.py --list
    python batch_test.py --latest 100
    python batch_test.py --all --seeds 1 2 3 4 5 100
    python batch_test.py --versions V7 V8 V9 --num-seeds 10
    python batch_test.py --mode network student_v9 100 --repeat 5
    python batch_test.py student_v9 100
    python batch_test.py student_v9 100 --repeat 5 --force-repeat
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import importlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))
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


def normalize_version_name(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"vn", "student_vn"}:
        return "student_vn"
    match = re.fullmatch(r"student_v(\d+)", lowered)
    if match:
        return f"student_v{int(match.group(1))}"
    match = re.fullmatch(r"v(\d+)", lowered)
    if match:
        return f"student_v{int(match.group(1))}"
    return lowered


def version_sort_key(version: str) -> tuple[int, int | str]:
    match = re.fullmatch(r"student_v(\d+)", version)
    if match:
        return (0, int(match.group(1)))
    if version == "student_vn":
        return (1, 0)
    return (2, version)


class NoopLogger:
    def log_snapshot(self, state: dict[str, Any]) -> None:
        return None

    def log_command(self, state: dict[str, Any], vehicle_id: str, command: dict[str, Any], source: str = "") -> None:
        return None


class ModuleStrategy:
    MODULE_NAME = ""

    def __init__(self, sdk: Any) -> None:
        if not self.MODULE_NAME:
            raise ValueError(f"{self.__class__.__name__} must set MODULE_NAME")
        self.module = importlib.import_module(self.MODULE_NAME)
        self.module.sdk = sdk
        if hasattr(self.module, "logger"):
            self.module.logger = NoopLogger()

    def __call__(self, state: dict[str, Any]) -> dict[str, Any]:
        return self.module.my_strategy(state) or {}


class StudentV0ModuleStrategy(ModuleStrategy):
    MODULE_NAME = "student_template.student_v0"


def discover_network_versions() -> list[str]:
    versions = []
    for path in STUDENT_DIR.glob("student_v*.py"):
        if re.fullmatch(r"student_v\d+", path.stem):
            versions.append(path.stem)
    if (STUDENT_DIR / "student_vn.py").exists():
        versions.append("student_vn")
    return sorted(set(versions), key=version_sort_key)


def discover_fast_version_map() -> dict[str, type[Any]]:
    from student_template import strategy as strategy_module

    versions: dict[str, type[Any]] = {}
    if (STUDENT_DIR / "student_v0.py").exists():
        versions["student_v0"] = StudentV0ModuleStrategy
    for symbol in getattr(strategy_module, "__all__", []):
        if symbol == "VNStrategy":
            version = "student_vn"
        else:
            match = re.fullmatch(r"V(\d+)Strategy", symbol)
            if not match:
                continue
            version = f"student_v{int(match.group(1))}"
        strategy_class = getattr(strategy_module, symbol, None)
        if isinstance(strategy_class, type):
            versions[version] = strategy_class
    return dict(sorted(versions.items(), key=lambda item: version_sort_key(item[0])))


def discover_fast_versions() -> list[str]:
    return list(discover_fast_version_map())


def discover_versions(mode: str) -> list[str]:
    if mode == "fast":
        return discover_fast_versions()
    if mode == "network":
        return discover_network_versions()
    raise ValueError(f"Unknown test mode: {mode}")


def discover_latest_version(mode: str) -> str | None:
    numeric_versions = []
    for version in discover_versions(mode):
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
    time.sleep(2)


def empty_metrics(version_file: str, seed: int, run_index: int) -> dict[str, Any]:
    return {
        "version": version_file,
        "seed": seed,
        "run": run_index,
        "mode": None,
        "status": "pending",
        "score": None,
        "file": None,
        "orders_completed": 0,
        "order_value": 0,
        "drop_reward": 0,
        "collision_penalty": 0,
        "overtime_penalty": 0,
        "ticks": 0,
        "elapsed": 0.0,
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


def build_mock_sdk(state: Any, config: dict[str, Any]) -> Any:
    """Build a StrategySDK-like object for in-process engine tests."""

    from student_template.strategy.sdk_ext import StrategySDK

    sdk = StrategySDK.__new__(StrategySDK)
    sdk._nodes = {node_id: {"x": node.x, "y": node.y} for node_id, node in state.nodes.items()}
    sdk._zone_map = {}
    for zone in (
        list(state.raw_zones.values())
        + list(state.processing_zones.values())
        + list(state.consumer_zones.values())
    ):
        sdk._zone_map[zone.id] = {"node": zone.node_id, "position": list(zone.position)}
    sdk._adjacency = {}
    for edge in state.edges:
        sdk._adjacency.setdefault(edge.from_node, []).append((edge.to_node, edge.distance))
        sdk._adjacency.setdefault(edge.to_node, []).append((edge.from_node, edge.distance))
    sdk._recipes = {
        recipe_id: {"value": recipe.value, "processing_time": recipe.processing_time}
        for recipe_id, recipe in state.recipes.items()
    }
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


def run_one_fast(
    version_file: str,
    seed: int,
    run_index: int,
    repeat: int,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run one trial in-process without WebSocket/server subprocesses."""

    from server.game_engine import (
        get_state_snapshot,
        init_game_state,
        is_game_over,
        load_config,
        load_map,
        process_commands,
        tick,
    )

    label = f"  seed={seed}"
    if repeat > 1:
        label += f" run={run_index}/{repeat}"
    print(f"{label} ...", end=" ", flush=True)

    metrics = empty_metrics(version_file, seed, run_index)
    metrics["mode"] = "fast"
    strategy_map = discover_fast_version_map()
    strategy_class = strategy_map.get(version_file)
    if strategy_class is None:
        metrics["status"] = "unsupported_fast_version"
        metrics["error"] = f"No strategy class exported for {version_file}"
        print("UNSUPPORTED_FAST", end=" ")
        return metrics

    t0 = time.time()
    try:
        config = load_config(str(PROJECT_DIR / "config.json"))
        map_data = load_map(str(PROJECT_DIR / "map.json"))
        state = init_game_state(config, map_data, seed=seed)
        sdk = build_mock_sdk(state, config)
        strategy = strategy_class(sdk)
        tick_rate = config["game"]["tick_rate"]
        dt = 1.0 / tick_rate
        tick_count = 0

        while not is_game_over(state):
            state_dict = get_state_snapshot(state)
            sdk._state = state_dict
            try:
                commands = strategy(state_dict)
            except Exception as exc:
                metrics["status"] = "strategy_error"
                metrics["error"] = f"{type(exc).__name__}: {exc}"
                if verbose:
                    import traceback

                    traceback.print_exc()
                print(f"STRATEGY_ERR({type(exc).__name__})", end=" ")
                break
            if commands:
                process_commands(state, commands)
            tick(state, dt)
            tick_count += 1
            if verbose and tick_count % 2000 == 0:
                total_ticks = state.config["game"]["duration"] * tick_rate
                elapsed = time.time() - t0
                print(f"\n    tick {tick_count}/{total_ticks:.0f} ({elapsed:.1f}s) score={state.score:.0f}", end=" ")

        metrics.update(
            {
                "score": state.score,
                "orders_completed": state.completed_orders_count,
                "order_value": state.completed_orders_value,
                "drop_reward": state.drop_reward_total,
                "collision_penalty": state.collision_penalty_total,
                "overtime_penalty": state.overtime_penalty_total,
                "ticks": tick_count,
                "elapsed": time.time() - t0,
            }
        )
        if metrics["status"] == "pending":
            metrics["status"] = "ok"
        print(f"{metrics['score']:.0f}pts", end=" ", flush=True)
    except Exception as exc:
        metrics["status"] = "error"
        metrics["error"] = f"{type(exc).__name__}: {exc}"
        metrics["elapsed"] = time.time() - t0
        print(f"ERR({type(exc).__name__})", end=" ")
    return metrics


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


async def run_one_network(version_file: str, seed: int, run_index: int, repeat: int) -> dict[str, Any]:
    """Run one version/seed/repeat trial through WebSocket/server processes."""

    label = f"  seed={seed}"
    if repeat > 1:
        label += f" run={run_index}/{repeat}"
    print(f"{label} ...", end=" ", flush=True)

    kill_existing_server()
    RECORDINGS_DIR.mkdir(exist_ok=True)
    known_recordings = {path.resolve() for path in RECORDINGS_DIR.glob("*.json")}
    started_at = time.time()
    metrics = empty_metrics(version_file, seed, run_index)
    metrics["mode"] = "network"
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
            import websockets

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


async def run_one(
    version_file: str,
    seed: int,
    run_index: int,
    repeat: int,
    mode: str,
    *,
    verbose: bool = False,
) -> dict[str, Any]:
    if mode == "fast":
        return run_one_fast(version_file, seed, run_index, repeat, verbose=verbose)
    if mode == "network":
        return await run_one_network(version_file, seed, run_index, repeat)
    raise ValueError(f"Unknown test mode: {mode}")


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
    runtime = ""
    if result.get("ticks") or result.get("elapsed"):
        runtime = f"  ticks={int(result.get('ticks') or 0)} elapsed={float(result.get('elapsed') or 0):.1f}s"
    print(
        f"  score={result['score']:.1f}"
        f"  orders={result.get('orders_completed', '?')}"
        f"  value={float(result.get('order_value', 0)):.0f}"
        f"  drop={float(result.get('drop_reward', 0)):.0f}"
        f"  collision={float(result.get('collision_penalty', 0)):.0f}"
        f"  overtime={float(result.get('overtime_penalty', 0)):.0f}"
        f"{runtime}"
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
        "mode",
        "status",
        "score",
        "orders_completed",
        "order_value",
        "drop_reward",
        "collision_penalty",
        "overtime_penalty",
        "ticks",
        "elapsed",
        "file",
        "error",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for results in all_results.values():
            for item in results:
                writer.writerow({key: item.get(key) for key in fieldnames})


def plot_boxplot(all_results: dict[str, list[dict[str, Any]]], versions: list[str], path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[WARN] matplotlib is not installed; skipping boxplot.")
        return False

    data = []
    labels = []
    for version in versions:
        scores = [float(item["score"]) for item in valid_results(all_results.get(version, []))]
        if scores:
            data.append(scores)
            labels.append(version.replace("student_", ""))
    if not data:
        print("[WARN] no valid scores; skipping boxplot.")
        return False

    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.1), 6))
    ax.boxplot(data, tick_labels=labels, showmeans=True, meanline=False)
    for index, scores in enumerate(data, start=1):
        if len(scores) == 1:
            offsets = [0.0]
        else:
            span = 0.16
            offsets = [(-span / 2) + span * i / (len(scores) - 1) for i in range(len(scores))]
        ax.scatter([index + offset for offset in offsets], scores, s=18, alpha=0.75)
    ax.set_title("Strategy Performance Comparison")
    ax.set_ylabel("Score")
    ax.set_xlabel("Version")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Boxplot saved to: {path}")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch-test student strategies.")
    parser.add_argument("version", nargs="?", default=None, help="Version, e.g. student_v9")
    parser.add_argument("seeds", nargs="*", type=int, help="Seed list, e.g. 100 200 300")
    parser.add_argument("--all", action="store_true", help="Test all discovered versions for the selected mode")
    parser.add_argument("--latest", action="store_true", help="Test the highest discovered student_vN.py")
    parser.add_argument("--list", action="store_true", help="List discovered testable versions and exit")
    parser.add_argument("--versions", nargs="+", help="Version list, e.g. V7 V8 student_v9 VN")
    parser.add_argument("--seeds", dest="seed_options", nargs="+", type=int, help="Seed list")
    parser.add_argument("--num-seeds", type=int, metavar="N", help="Use seeds 10,20,...,10*N")
    parser.add_argument("--mode", choices=["fast", "network"], default="fast", help="Test backend")
    parser.add_argument("--network", action="store_const", const="network", dest="mode", help="Alias for --mode network")
    parser.add_argument("--runs", type=int, default=5, help="Default seed count when no seeds are supplied")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat count per seed")
    parser.add_argument("--force-repeat", action="store_true", help="Also repeat deterministic fast-mode tests")
    parser.add_argument("--output", default="batch_results.json", help="JSON result file path")
    parser.add_argument("--csv", default=None, help="Optional flat CSV result file path")
    parser.add_argument("--no-plot", action="store_true", help="Skip boxplot generation")
    parser.add_argument("--plot-output", default="batch_results.png", help="Boxplot image path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show fast-mode tick progress")
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
        print(f"Fast versions:    {discover_versions('fast')}")
        print(f"Network versions: {discover_versions('network')}")
        print(f"Latest fast:      {discover_latest_version('fast')}")
        print(f"Latest network:   {discover_latest_version('network')}")
        return

    if args.repeat < 1:
        raise SystemExit("--repeat must be >= 1")
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    if args.num_seeds is not None and args.num_seeds < 1:
        raise SystemExit("--num-seeds must be >= 1")

    seed_args = normalize_seed_args(args)

    if args.all:
        versions = discover_versions(args.mode)
    elif args.latest:
        latest = discover_latest_version(args.mode)
        if latest is None:
            raise SystemExit(f"No student_vN.py files found for mode={args.mode}")
        versions = [latest]
    elif args.versions:
        versions = [normalize_version_name(version) for version in args.versions]
    elif args.version:
        versions = [normalize_version_name(args.version)]
    else:
        versions = discover_versions(args.mode)

    if args.mode == "network":
        missing = [version for version in versions if not (STUDENT_DIR / f"{version}.py").exists()]
        if missing:
            raise SystemExit(f"Missing student files: {missing}")
    else:
        fast_map = discover_fast_version_map()
        missing = [version for version in versions if version not in fast_map]
        if missing:
            raise SystemExit(f"Fast mode has no exported strategy class for: {missing}")

    if args.num_seeds is not None:
        seeds = [10 * (index + 1) for index in range(args.num_seeds)]
    elif args.seed_options:
        seeds = args.seed_options
    elif seed_args:
        seeds = seed_args
    else:
        seeds = list(range(20, args.runs * 20 + 1, 20))

    effective_repeat = args.repeat
    if args.mode == "fast" and args.repeat > 1 and not args.force_repeat:
        effective_repeat = 1
        print(
            f"[INFO] fast mode is deterministic for a fixed seed; "
            f"requested repeat={args.repeat} collapsed to 1. "
            f"Use --force-repeat to run repeated fast checks."
        )

    print(f"Versions: {versions}")
    print(f"Seeds:    {seeds}")
    print(f"Repeat:   {effective_repeat}")
    if effective_repeat != args.repeat:
        print(f"Requested repeat: {args.repeat}")
    print(f"Mode:     {args.mode}")
    print(f"Speed:    {SPEED}x")
    print()

    all_results: dict[str, list[dict[str, Any]]] = {}
    summary: dict[str, Any] = {}
    for version in versions:
        print(f"[{version}]")
        version_results: list[dict[str, Any]] = []
        for seed in seeds:
            seed_results: list[dict[str, Any]] = []
            for run_index in range(1, effective_repeat + 1):
                result = await run_one(version, seed, run_index, effective_repeat, args.mode, verbose=args.verbose)
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
        "mode": args.mode,
        "versions": versions,
        "seeds": seeds,
        "requested_repeat": args.repeat,
        "effective_repeat": effective_repeat,
        "force_repeat": args.force_repeat,
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

    if not args.no_plot:
        plot_path = resolve_output_path(args.plot_output, "batch_results.png")
        plot_boxplot(all_results, versions, plot_path)


if __name__ == "__main__":
    asyncio.run(main())
