"""T-BOT-LIVE-ENABLEMENT1 — seeder script for pre-existing positions.

Tests cover:
  - validation rejects malformed input
  - already-tracked symbols are refused (no overwrite)
  - dry-run does not write state
  - real run writes manage=False rows with correct per-share premium
  - emit-template produces parseable JSON

The script never places a broker order; tests don't need an IB stub.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def seeder(tmp_path, monkeypatch):
    """Import scripts/seed_existing_positions.py with POSITIONS_PATH
    pointed at a tmp file so the live state/ is never touched."""
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    sys.modules.pop("seed_existing_positions", None)
    mod = importlib.import_module("seed_existing_positions")
    monkeypatch.setattr(mod, "POSITIONS_PATH", tmp_path / "positions.json")
    yield mod, tmp_path
    sys.modules.pop("seed_existing_positions", None)


def _write_seed(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(rows))
    return p


def _row(symbol: str = "AAPL", **overrides) -> dict:
    base = {
        "symbol": symbol,
        "right": "C",
        "strike": 340.0,
        "expiry": "20260717",
        "contracts": 20,
        "avg_cost_per_contract": 119.4183,
        "currency": "USD",
    }
    base.update(overrides)
    return base


def test_validate_rejects_missing_key(seeder):
    mod, _ = seeder
    bad = dict(_row())
    bad.pop("strike")
    assert mod._validate(bad) is not None


def test_validate_rejects_bad_right(seeder):
    mod, _ = seeder
    assert mod._validate(_row(right="X")) is not None


def test_validate_rejects_bad_expiry(seeder):
    mod, _ = seeder
    assert mod._validate(_row(expiry="2026-07-17")) is not None


def test_validate_accepts_good_row(seeder):
    mod, _ = seeder
    assert mod._validate(_row()) is None


def test_build_row_converts_per_contract_to_per_share(seeder):
    mod, _ = seeder
    row = mod.build_row(_row(avg_cost_per_contract=119.4183))
    assert row["premium_entry"] == 1.1942  # rounded to 4dp
    assert row["manage"] is False
    assert row["claimed"] is True
    assert row["right"] == "C"
    assert row["contracts"] == 20


def test_dry_run_does_not_write(seeder, tmp_path, capsys, monkeypatch):
    mod, _ = seeder
    seed_file = _write_seed(tmp_path, [_row()])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--input",
            str(seed_file),
            "--dry-run",
        ],
    )
    rc = mod.main()
    assert rc == 0
    assert not mod.POSITIONS_PATH.exists()


def test_real_run_writes_rows(seeder, tmp_path, monkeypatch):
    mod, _ = seeder
    seed_file = _write_seed(tmp_path, [_row("AAPL"), _row("GOOG")])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--input",
            str(seed_file),
            "--yes",
        ],
    )
    rc = mod.main()
    assert rc == 0
    assert mod.POSITIONS_PATH.exists()
    payload = json.loads(mod.POSITIONS_PATH.read_text())
    assert set(payload.keys()) == {"AAPL", "GOOG"}
    for sym in ("AAPL", "GOOG"):
        assert payload[sym]["manage"] is False
        assert payload[sym]["claimed"] is True


def test_refuses_to_overwrite_existing(seeder, tmp_path, monkeypatch):
    mod, _ = seeder
    # Pre-populate AAPL with a different shape so we'd notice if it
    # got overwritten.
    mod.POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.POSITIONS_PATH.write_text(
        json.dumps(
            {
                "AAPL": {
                    "contracts": 1,
                    "premium_entry": 99.99,
                    "strike": 1.0,
                    "expiry": "20991231",
                    "right": "C",
                    "manage": True,
                    "claimed": False,
                },
            }
        )
    )
    seed_file = _write_seed(tmp_path, [_row("AAPL"), _row("GOOG")])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--input",
            str(seed_file),
            "--yes",
        ],
    )
    rc = mod.main()
    assert rc == 0
    payload = json.loads(mod.POSITIONS_PATH.read_text())
    # AAPL untouched (refused), GOOG added.
    assert payload["AAPL"]["premium_entry"] == 99.99
    assert payload["AAPL"]["manage"] is True
    assert "GOOG" in payload


def test_emit_template_prints_valid_json(seeder, monkeypatch, capsys):
    mod, _ = seeder
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--emit-template",
        ],
    )
    rc = mod.main()
    assert rc == 0
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert isinstance(rows, list) and len(rows) >= 1
    for r in rows:
        assert mod._validate(r) is None


def test_missing_input_file_returns_error(seeder, tmp_path, monkeypatch):
    mod, _ = seeder
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--input",
            str(tmp_path / "no-such-file.json"),
        ],
    )
    rc = mod.main()
    assert rc == 2


def test_non_array_input_rejected(seeder, tmp_path, monkeypatch):
    mod, _ = seeder
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"not": "a list"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_existing_positions.py",
            "--input",
            str(p),
        ],
    )
    rc = mod.main()
    assert rc == 2
