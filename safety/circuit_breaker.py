import json
from pathlib import Path

from config import KILL_SWITCH_FILE, MAX_DAILY_LOSS_PCT, STATE_FILE


class CircuitBreaker:
    """Hard limits that work independent of the AI brain."""

    def __init__(self):
        self.state_file = Path(STATE_FILE)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save(self, s: dict) -> None:
        self.state_file.write_text(json.dumps(s, indent=2, default=str))

    def kill_switch_active(self) -> bool:
        return Path(KILL_SWITCH_FILE).exists()

    def check(self, current_equity: float) -> tuple[bool, str]:
        if self.kill_switch_active():
            return False, "kill_switch"

        s = self._load()
        if s.get("start_of_day_equity") is None:
            s["start_of_day_equity"] = current_equity
            s["tripped"] = False
            self._save(s)
            return True, "ok"

        dd = (current_equity - s["start_of_day_equity"]) / s["start_of_day_equity"]
        if dd <= -MAX_DAILY_LOSS_PCT:
            s["tripped"] = True
            self._save(s)
            return False, f"max_daily_loss:{dd:.2%}"

        if s.get("tripped"):
            return False, "tripped_today"

        return True, "ok"

    def reset_daily(self, equity: float) -> None:
        s = self._load()
        s["start_of_day_equity"] = equity
        s["tripped"] = False
        self._save(s)
