"""Portable paths for opt-in diagnostic seams; output always stays Git-ignored."""
from datetime import datetime
from pathlib import Path
import sys

SDK = Path(__file__).resolve().parents[2]
EVIDENCE = SDK.parents[1] / ".local-test-evidence" / datetime.now().strftime("%Y-%m-%d") / "assurance-integration"
EVIDENCE.mkdir(parents=True, exist_ok=True)
sys.path[:0] = [str(SDK / "src"), str(SDK / "tests/agents"),
               str(SDK / "tests/orchestrator/full_target"),
               str(SDK / "tests/orchestrator/full_target/operation_completion")]
