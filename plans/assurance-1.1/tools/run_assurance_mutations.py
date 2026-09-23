#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Assurance 1.1 定点变异：逐条把 src/agent_orchestrator 的一个守卫改坏，跑指定用例，必须失败；跑完原样恢复。

用法：.venv/bin/python plans/assurance-1.1/tools/run_assurance_mutations.py [--out DIR] [--only AM01,AM02]
判定：目标用例集合在变异下出现失败（failures>0 且 errors==0 且 skips==0）= KILLED；否则 NOT_PROVEN。
不改断言、不装依赖；源文件字节在 finally 中恢复并用 git diff 复核。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

SDK = Path(__file__).resolve().parents[3]
SRC = SDK / "src/agent_orchestrator"
PLAN = SDK / "plans/assurance-1.1"


def run(mutation: dict, out: Path) -> dict:
    edits = mutation.get("edits") or [{"file": mutation["file"], "before": mutation["before"], "after": mutation["after"]}]
    originals = {}
    for edit in edits:
        target = SRC / edit["file"]
        text = target.read_bytes().decode("utf-8")
        count = text.count(edit["before"])
        if count != 1:
            return {"id": mutation["id"], "status": "INVALID_TARGET", "count": count, "file": edit["file"]}
        originals[target] = text
    junit = out / f"{mutation['id']}.junit.xml"
    started = time.time()
    try:
        for edit in edits:
            target = SRC / edit["file"]
            target.write_bytes(originals[target].replace(edit["before"], edit["after"]).encode("utf-8"))
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}",
             *mutation["tests"]], cwd=str(SDK), capture_output=True, text=True, timeout=900)
    finally:
        for target, text in originals.items():
            target.write_bytes(text.encode("utf-8"))
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if junit.exists():
        for suite in ET.parse(junit).getroot().iter("testsuite"):
            for key in counts:
                counts[key] += int(suite.get(key, 0))
    killed = counts["tests"] == len(mutation["tests"]) and counts["failures"] > 0 and counts["errors"] == 0 \
        and counts["skipped"] == 0
    (out / f"{mutation['id']}.log").write_text(completed.stdout[-20000:] + "\n--- stderr ---\n" + completed.stderr[-4000:])
    return {"id": mutation["id"], "files": [e["file"] for e in edits], "why": mutation["why"], "tests": mutation["tests"],
            "status": "KILLED" if killed else "NOT_PROVEN", "counts": counts, "returncode": completed.returncode,
            "seconds": round(time.time() - started, 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--only", default=None)
    args = parser.parse_args()
    out = Path(args.out) if args.out else PLAN / "mutation-runs" / time.strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    mutations = json.loads((PLAN / "mutations.json").read_text(encoding="utf-8"))
    only = set(args.only.split(",")) if args.only else None
    results = []
    for mutation in mutations:
        if only and mutation["id"] not in only:
            continue
        result = run(mutation, out)
        results.append(result)
        print(json.dumps({k: result.get(k) for k in ("id", "status", "counts", "seconds")}, ensure_ascii=False), flush=True)
    clean = subprocess.run(["git", "diff", "--quiet", "--", str(SRC)], cwd=str(SDK)).returncode == 0
    report = {"scope": "ASSURANCE_1_1_TARGETED_MUTATIONS", "sdk": str(SDK), "source_restored_clean": clean,
              "total": len(results), "killed": sum(r["status"] == "KILLED" for r in results),
              "all_killed": bool(results) and all(r["status"] == "KILLED" for r in results), "mutations": results}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS" if report["all_killed"] and clean else "FAIL", "killed": report["killed"],
                      "total": report["total"], "source_restored_clean": clean, "report": str(out / "report.json")},
                     ensure_ascii=False))
    return 0 if report["all_killed"] and clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
