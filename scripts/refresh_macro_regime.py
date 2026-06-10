"""Refresh macro regime via the macro-regime-detector skill.

Cron: 0 8 * * 1  (Monday 08:00 ET, alongside watchlist rebuild)

Subprocess-invokes the skill, parses the latest macro_regime_*.json
output, writes a slim state/macro_regime.json with {regime, confidence,
score, ts}. Also cleans up skill output files older than 4 weeks.

Bot reads state/macro_regime.json at cycle start to derive a risk
multiplier (see allocation/position_sizer.py:risk_multiplier_for_regime).

FMP key: skill expects FMP_API_KEY. Bot's existing FMP code reads
FMP_API or FMP_API_KEY (brain/fundamentals.py). This script bridges
both — copies FMP_API → FMP_API_KEY in env if only FMP_API is set.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from subprocess import run

ROOT = Path(__file__).resolve().parent.parent

SKILL_PATH = Path.home() / ".claude" / "skills" / "macro-regime-detector"
SKILL_SCRIPT = SKILL_PATH / "scripts" / "macro_regime_detector.py"
SKILL_OUT_DIR = SKILL_PATH / "scripts"  # skill writes JSON next to itself
STATE_FILE = ROOT / "state" / "macro_regime.json"
RETENTION_WEEKS = 4


def _bridge_fmp_key():
    """Skill expects FMP_API_KEY. Bot config might use FMP_API.
    Make both names available without forcing the operator to edit .env.

    Searches multiple .env locations in priority order so the operator
    can keep a single source of truth at the repo root:
      1. versions/<bot>/.env   (per-bot override, highest priority)
      2. ~/code/tradingbot/.env (repo root, single source of truth)
    """
    # ROOT = versions/<bot>; ROOT.parent.parent = repo root
    repo_root = ROOT.parent.parent
    candidates = [ROOT / ".env", repo_root / ".env"]
    for env_file in candidates:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            # First-found-wins (per-bot beats repo-root)
            if k.strip() and not os.environ.get(k.strip()):
                os.environ[k.strip()] = v
    # Bridge name aliases
    if not os.environ.get("FMP_API_KEY") and os.environ.get("FMP_API"):
        os.environ["FMP_API_KEY"] = os.environ["FMP_API"]


def _find_latest_skill_output() -> Path | None:
    """The skill writes macro_regime_YYYY-MM-DD_HHMMSS.json — find newest."""
    if not SKILL_OUT_DIR.exists():
        return None
    candidates = sorted(SKILL_OUT_DIR.glob("macro_regime_*.json"))
    return candidates[-1] if candidates else None


def _cleanup_old_outputs():
    """Keep only files from the last RETENTION_WEEKS weeks."""
    if not SKILL_OUT_DIR.exists():
        return
    cutoff = dt.datetime.now() - dt.timedelta(weeks=RETENTION_WEEKS)
    for p in SKILL_OUT_DIR.glob("macro_regime_*.json"):
        try:
            mtime = dt.datetime.fromtimestamp(p.stat().st_mtime)
            if mtime < cutoff:
                p.unlink()
                print(f"  cleaned: {p.name}")
        except OSError:
            pass


def main() -> int:
    _bridge_fmp_key()
    if not os.environ.get("FMP_API_KEY"):
        print("error: FMP_API_KEY not set in environment or .env", file=sys.stderr)
        return 1
    if not SKILL_SCRIPT.exists():
        print(f"error: skill not found at {SKILL_SCRIPT}", file=sys.stderr)
        return 1

    print(f"running skill: {SKILL_SCRIPT}")
    res = run(
        [sys.executable, str(SKILL_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        print(f"skill exited rc={res.returncode}", file=sys.stderr)
        print(f"  stderr: {res.stderr[:500]}", file=sys.stderr)
        return res.returncode

    latest = _find_latest_skill_output()
    if latest is None:
        print("error: skill produced no output JSON", file=sys.stderr)
        return 1
    print(f"  parsing: {latest.name}")

    try:
        data = json.loads(latest.read_text())
    except json.JSONDecodeError as e:
        print(f"error: skill output not valid JSON: {e}", file=sys.stderr)
        return 1

    # Skill output shape varies slightly across versions; pull what we use
    regime = (
        data.get("regime") or data.get("classification") or data.get("current_regime") or "unknown"
    )
    confidence = data.get("confidence") or data.get("confidence_score") or 0
    score = data.get("composite_score") or data.get("score") or 0

    slim = {
        "regime": str(regime).strip(),
        "confidence": float(confidence)
        if isinstance(confidence, int | float | str)
        and str(confidence).replace(".", "").replace("-", "").isdigit()
        else 0.0,
        "score": float(score)
        if isinstance(score, int | float | str)
        and str(score).replace(".", "").replace("-", "").isdigit()
        else 0.0,
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "source": str(latest.name),
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(slim, indent=2))
    print(f"  wrote {STATE_FILE.relative_to(ROOT)}: regime={slim['regime']}")

    _cleanup_old_outputs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
