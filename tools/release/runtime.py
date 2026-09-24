"""Load runtime distribution only from this trusted toolkit checkout."""

import importlib.util
from pathlib import Path


def helper():
    path = Path(__file__).resolve().parents[1] / "runtime/sync.py"
    spec = importlib.util.spec_from_file_location("golden_runtime_distribution", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded
