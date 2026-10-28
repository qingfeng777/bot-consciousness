"""Local CLI. Nothing in this module controls real hardware or calls a model."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from .domain import DomainError, Signal
from .driver import SimulatedDriver
from .journal import Journal
from .replay import load_scenario, replay
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
    if args.command == "replay":
        emit(await replay(load_scenario(args.scenario)))
        return 0
    journal_path, driver_path = storage(args.data_dir)
    journal = Journal(journal_path)
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
                await runtime.submit(Signal(text))
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
    execute = sub.add_parser("run", help="文本输入到模拟动作")
    execute.add_argument("text", nargs="+", help="输入文本，按一批受理后排序")
    execute.add_argument("--data-dir", help="持久化目录，默认内存")
    execute.add_argument("--duration", type=float, default=0.01, help="模拟每个动作耗时，秒")
    resume = sub.add_parser("resume", help="核对未终结动作并继续已排队任务")
    resume.add_argument("--data-dir", required=True)
    resume.add_argument("--duration", type=float, default=0.01)
    inspect = sub.add_parser("inspect", help="导出事件和任务；运行实例需已关闭")
    inspect.add_argument("--data-dir", required=True)
    playback = sub.add_parser("replay", help="在全新内存模拟器中回放输入文件")
    playback.add_argument("scenario")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (DomainError, ValueError, OSError) as exc:
        print(json.dumps({"error": getattr(exc, "code", type(exc).__name__), "detail": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
