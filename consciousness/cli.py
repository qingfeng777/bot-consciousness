"""Local CLI. Nothing in this module controls real hardware or calls a model."""
import argparse
import asyncio
import json
import hashlib
from pathlib import Path
import sys

from .domain import DomainError, Signal
from .driver import SimulatedDriver
from .journal import Journal
from .replay import load_scenario, load_stream, replay, replay_stream
from .runtime import Runtime


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def storage(directory):
    if directory is None:
        return ":memory:", ":memory:"
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    return str(root / "journal.sqlite3"), str(root / "simulator.sqlite3")


async def run(args):
    if args.command == "simulate":
        from .simulation import simulate
        result = await simulate(args.scenario, args.data_dir)
        emit(result)
        return 2 if result["runtime"]["blocked"] else 0
    if args.command == "replay":
        result = (await replay_stream(load_stream(args.scenario), duration=args.duration,
                                      stop_delay=args.stop_delay) if args.timed else
                  await replay(load_scenario(args.scenario)))
        emit(result)
        return 2 if result.get("blocked") else 0
    if args.command in {"inspect", "resume", "backup"} and not (Path(args.data_dir) / "journal.sqlite3").is_file():
        raise FileNotFoundError("data directory has no journal.sqlite3")
    if args.data_dir and args.command != "inspect" and (Path(args.data_dir) / "INCOMPLETE").exists():
        raise ValueError("refusing to run or copy an incomplete backup")
    journal_path, driver_path = storage(args.data_dir)
    journal = Journal(journal_path, robot_id=getattr(args, "robot_id", None),
                      max_tasks=getattr(args, "max_tasks", None),
                      max_events=getattr(args, "max_events", None),
                      aging_seconds=getattr(args, "aging_seconds", None))
    if args.command == "backup":
        try:
            destination = Path(args.destination)
            if destination.exists():
                raise FileExistsError("backup destination must be a new directory")
            destination.mkdir(parents=True)
            marker = destination / "INCOMPLETE"
            marker.write_text("Backup must not be restored until manifest.json exists.\n")
            report = journal.backup(destination / "journal.sqlite3")
            if Path(driver_path).exists():
                import sqlite3
                source = sqlite3.connect(driver_path)
                target = sqlite3.connect(destination / "simulator.sqlite3")
                try:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("simulator backup integrity check failed")
                finally:
                    source.close()
                    target.close()
            report["files"] = {}
            for file in destination.glob("*.sqlite3"):
                digest = hashlib.sha256()
                with file.open("rb") as source:
                    for chunk in iter(lambda: source.read(128 * 1024), b""):
                        digest.update(chunk)
                report["files"][file.name] = digest.hexdigest()
            (destination / "manifest.json").write_text(json.dumps(report, indent=2))
            marker.unlink()
            emit(report)
        finally:
            journal.close()
        return 0
    if args.command == "inspect":
        try:
            cursor = 0
            while True:
                batch = journal.history(cursor)
                if not batch:
                    break
                for event in batch:
                    emit(event)
                cursor = batch[-1]["seq"]
            emit({"tasks": journal.tasks()})
        finally:
            journal.close()
        return 0
    driver = SimulatedDriver(path=driver_path, duration=args.duration)
    runtime = Runtime(journal, driver)
    try:
        await runtime.start()  # Always reconcile unfinished actions before dispatch.
        if args.command == "run":
            for text in args.text:
                await runtime.submit(Signal(text, robot_id=journal.robot_id))
        summary = await runtime.drain()
        emit(summary)
        return 2 if summary["blocked"] else 0
    finally:
        try:
            await runtime.close()
        finally:
            # For a pending simulator call, preserve the unresolved journal;
            # event loop shutdown is not advertised as a physical stop receipt.
            if not driver.active_count:
                driver.close()
            journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="机器人认知内核 V0，仅模拟执行")
    sub = parser.add_subparsers(dest="command", required=True)
    simulation = sub.add_parser("simulate", help="在本机运行多模块认知仿真场景")
    simulation.add_argument("scenario")
    simulation.add_argument("--data-dir", help="持久化模块状态及模拟动作回执")
    execute = sub.add_parser("run", help="文本输入到模拟动作")
    execute.add_argument("text", nargs="+", help="输入文本，按一批受理后排序")
    execute.add_argument("--data-dir", help="持久化目录，默认内存")
    execute.add_argument("--duration", type=float, default=0.01, help="模拟每个动作耗时，秒")
    execute.add_argument("--robot-id", help="固定机器人标识，与已有数据库必须一致")
    execute.add_argument("--max-tasks", type=int)
    execute.add_argument("--max-events", type=int)
    execute.add_argument("--aging-seconds", type=float)
    resume = sub.add_parser("resume", help="核对未终结动作并继续已排队任务")
    resume.add_argument("--data-dir", required=True)
    resume.add_argument("--duration", type=float, default=0.01)
    inspect = sub.add_parser("inspect", help="导出事件和任务；运行实例需已关闭")
    inspect.add_argument("--data-dir", required=True)
    backup = sub.add_parser("backup", help="独占实例锁后备份日志和模拟器数据库，不覆盖已有目录")
    backup.add_argument("--data-dir", required=True)
    backup.add_argument("destination")
    playback = sub.add_parser("replay", help="在全新内存模拟器中回放输入文件")
    playback.add_argument("scenario")
    playback.add_argument("--timed", action="store_true", help="按虚拟时钟回放带 at 的事件")
    playback.add_argument("--duration", type=float, default=1.0)
    playback.add_argument("--stop-delay", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (DomainError, ValueError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "detail": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
