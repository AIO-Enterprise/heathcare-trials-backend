"""Weekly health agent — entry point.

Usage:
    python -m scripts.health_agent.run_weekly                 # all layers, commit report
    python -m scripts.health_agent.run_weekly --layers infra,routes
    python -m scripts.health_agent.run_weekly --no-commit     # skip git commit
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import List

from . import report
from .config import LAYERS
from .probes import e2e, infra, providers, routes, services
from .probes.base import ProbeResult

RUNNERS = {
    "infra": infra.run,
    "providers": providers.run,
    "routes": routes.run,
    "services": services.run,
    "e2e": e2e.run,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--layers", default=",".join(LAYERS), help=f"Comma-separated subset of: {','.join(LAYERS)}")
    parser.add_argument("--no-commit", action="store_true", help="Write report but don't git-commit it")
    args = parser.parse_args(argv)

    requested = [l.strip() for l in args.layers.split(",") if l.strip()]
    unknown = [l for l in requested if l not in RUNNERS]
    if unknown:
        print(f"Unknown layer(s): {unknown}. Valid: {LAYERS}", file=sys.stderr)
        return 2

    started = dt.datetime.now()
    all_results: list[ProbeResult] = []
    for layer in requested:
        print(f"[{layer}] running...")
        results = RUNNERS[layer]()
        for r in results:
            tag = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}[r.status]
            print(f"  [{tag}] {r.module} ({r.duration_ms}ms) {r.note or ''}")
        all_results.extend(results)

    finished = dt.datetime.now()
    path = report.write(all_results, started, finished, commit=not args.no_commit)
    print(f"\nReport written: {path}")

    failed = any(r.status == "fail" for r in all_results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
