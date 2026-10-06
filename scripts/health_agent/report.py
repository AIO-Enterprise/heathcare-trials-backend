"""Render probe results to Markdown and write to reports/."""

from __future__ import annotations

import datetime as dt
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import List

from .config import REPO_ROOT, REPORTS_DIR
from .probes.base import ProbeResult

STATUS_ICON = {"ok": "PASS", "fail": "FAIL", "skip": "SKIP"}


def _summary_line(results: list[ProbeResult]) -> str:
    c = Counter(r.status for r in results)
    return f"{c.get('ok', 0)} pass / {c.get('fail', 0)} fail / {c.get('skip', 0)} skip"


def render(results: list[ProbeResult], started_at: dt.datetime, finished_at: dt.datetime) -> str:
    by_layer = defaultdict(list)
    for r in results:
        by_layer[r.layer].append(r)

    duration = (finished_at - started_at).total_seconds()
    summary = _summary_line(results)
    headline = "PASS" if not any(r.status == "fail" for r in results) else "FAIL"

    lines: list[str] = []
    lines.append(f"# Health check {started_at.date().isoformat()} — {headline}")
    lines.append("")
    lines.append(f"- Started: {started_at.isoformat(timespec='seconds')}")
    lines.append(f"- Duration: {duration:.1f}s")
    lines.append(f"- Summary: {summary}")
    lines.append("")

    for layer in ["infra", "providers", "routes", "services", "e2e"]:
        items = by_layer.get(layer, [])
        if not items:
            continue
        lines.append(f"## {layer}")
        lines.append("")
        lines.append("| Module | Status | Time | Note |")
        lines.append("|---|---|---|---|")
        for r in items:
            note = r.note or ""
            if r.status == "fail":
                note = "see details below"
            lines.append(f"| {r.module} | {STATUS_ICON[r.status]} | {r.duration_ms}ms | {note} |")
        lines.append("")

    fails = [r for r in results if r.status == "fail"]
    if fails:
        lines.append("## Failures")
        lines.append("")
        for r in fails:
            lines.append(f"### {r.layer}.{r.module}")
            lines.append("")
            lines.append("```")
            lines.append((r.error or "").strip())
            lines.append("```")
            lines.append("")

    return "\n".join(lines)


def write(results: list[ProbeResult], started_at: dt.datetime, finished_at: dt.datetime, commit: bool = True) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    body = render(results, started_at, finished_at)
    path = REPORTS_DIR / f"health-{started_at.date().isoformat()}.md"
    path.write_text(body, encoding="utf-8")

    if commit:
        try:
            subprocess.run(["git", "add", str(path)], cwd=REPO_ROOT, check=True, capture_output=True, text=True)
            headline = "PASS" if not any(r.status == "fail" for r in results) else "FAIL"
            subprocess.run(
                ["git", "commit", "-m", f"chore(health): weekly report {started_at.date().isoformat()} — {headline}"],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            (REPORTS_DIR / "_last_commit_error.log").write_text(
                (e.stdout or "") + "\n" + (e.stderr or ""), encoding="utf-8"
            )
    return path
