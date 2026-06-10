"""Per-tree pytest config.

Adds THIS bot's directory to sys.path so `from safety.phantom_guard import ...`
resolves to options-v1.2's safety/, not options-v1.3's. Prevents the
identically-named test_phantom_guard.py files from colliding when pytest
collects both trees in one run.
"""

import sys
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parent.parent
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))
