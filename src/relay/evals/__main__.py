"""relay-eval CLI.

  relay-eval redteam  [--payloads src/relay/evals/payloads.yaml] [--config config]   (no LLM, deterministic)
  relay-eval review-build --repo /path/to/repo --out evals/review_bench.jsonl
  relay-eval review       --bench evals/review_bench.jsonl
  relay-eval coding       --tasks evals/coding_tasks.jsonl [--arm relay|baseline] [--predictions out.jsonl]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from relay.common.settings import get_settings
from relay.evals import redteam, review_bench
from relay.evals.agents_bench import coding_summary, run_coding_task, write_swebench_predictions
from relay.llm.client import GatewayLLM


async def _main(a: argparse.Namespace) -> int:
    out = Path("evals/reports")
    out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "redteam":
        rep = await redteam.run_suite(Path(a.payloads), Path(a.config))
        (out / "redteam.md").write_text(redteam.report_markdown(rep), encoding="utf-8")
        print(redteam.report_markdown(rep))
        return 0 if rep["pass_rate"] >= a.min_pass else 1
    if a.cmd == "review-build":
        items = review_bench.build_from_repo(Path(a.repo))
        review_bench.save(items, Path(a.out))
        print(f"{len(items)} items ({sum(i.kind == 'buggy' for i in items)} buggy)")
        return 0
    llm = GatewayLLM(get_settings())
    try:
        if a.cmd == "review":
            rep = await review_bench.run(llm, review_bench.load(Path(a.bench)))
            (out / "review.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
            print(json.dumps(rep["best"], indent=2))
        elif a.cmd == "coding":
            tasks = [json.loads(x) for x in Path(a.tasks).read_text(encoding="utf-8").splitlines() if x.strip()]
            results = [await run_coding_task(llm, t, Path(a.config), arm=a.arm) for t in tasks]
            if a.predictions:
                write_swebench_predictions(results, Path(a.predictions), f"relay-{a.arm}")
            summary = coding_summary(results)
            (out / f"coding-{a.arm}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            print(json.dumps(summary, indent=2))
    finally:
        await llm.aclose()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="relay-eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("redteam")
    r.add_argument("--payloads", default=str(Path(__file__).with_name("payloads.yaml")))
    r.add_argument("--config", default="config")
    r.add_argument("--min-pass", type=float, default=1.0)
    b = sub.add_parser("review-build")
    b.add_argument("--repo", required=True)
    b.add_argument("--out", default="evals/review_bench.jsonl")
    rv = sub.add_parser("review")
    rv.add_argument("--bench", required=True)
    c = sub.add_parser("coding")
    c.add_argument("--tasks", required=True)
    c.add_argument("--arm", choices=["relay", "baseline"], default="relay")
    c.add_argument("--predictions")
    c.add_argument("--config", default="config")
    sys.exit(asyncio.run(_main(ap.parse_args())))


if __name__ == "__main__":
    main()
