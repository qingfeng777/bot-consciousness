"""Reproducible multi-module local simulation evaluation, not a research score."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import platform
import tempfile
import time

from . import __version__
from .domain import canonical
from .simulation import simulate


async def evaluate(scenario, runs=100):
    if type(runs) is not int or not 1 <= runs <= 10000:
        raise ValueError("runs must be 1..10000")
    started = time.monotonic()
    hashes = set()
    max_active, actions, events = 0, 0, 0
    with tempfile.TemporaryDirectory(prefix="cognitive-evaluation-") as root:
        for index in range(runs):
            # Every trial starts from empty on-disk state, then resumes it.
            path = Path(root) / str(index)
            result = await simulate(scenario, path)
            resumed = await simulate(scenario, path)
            if result["runtime"]["blocked"] or any(g["status"] != "satisfied" for g in result["goals"].values()):
                raise AssertionError("scenario did not achieve its observable goals")
            if result["runtime"]["counts"] != resumed["runtime"]["counts"] or resumed["max_external_concurrency"] != 0:
                raise AssertionError("resume changed or repeated effects")
            normalized = {key: result[key] for key in ("facts", "goals", "affect", "conflicts", "scene", "self_state", "identity")}
            hashes.add(hashlib.sha256(canonical(normalized).encode()).hexdigest())
            max_active = max(max_active, result["max_external_concurrency"])
            actions += result["runtime"]["counts"]["completed"]
            events += result["events"]
    if len(hashes) != 1 or max_active > 1:
        raise AssertionError("nondeterministic cognitive outcome or concurrent effects")
    digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    return dict(version=__version__, simulation=True, python=platform.python_version(), platform=platform.platform(),
                scenario=str(scenario), scenario_sha256=result["scenario_sha256"], source_sha256=digest.hexdigest(),
                runs=runs, scenario_events=events, completed_actions=actions, restarts=runs,
                max_external_concurrency=max_active, distinct_outcome_hashes=len(hashes),
                outcome_sha256=next(iter(hashes)), elapsed_seconds=time.monotonic() - started,
                checks={"observable_goals_satisfied": True, "restart_without_repeated_effect": True,
                        "deterministic_outcome": True, "serial_execution": True},
                limitations=["fixed annotated fixtures, not learned perception", "offline model candidate fixture",
                             "same scenario repeated, not independent research validation", "not a 72-hour soak"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="examples/cognitive.jsonl")
    parser.add_argument("--runs", type=int, default=100)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = json.dumps(asyncio.run(evaluate(args.scenario, args.runs)), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(result + "\n")
    print(result)


if __name__ == "__main__":
    main()
