"""Load the plugin directory (``gemini-voice`` is not an importable name) as a package for tests."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = "gemini_voice_under_test"
logging.getLogger("gemini_voice").addHandler(logging.NullHandler())  # tests assert logs explicitly


def load(module: str) -> ModuleType:
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{PACKAGE}.{module}") if module else sys.modules[PACKAGE]
