"""Optional contract test against a real Hermes checkout.

    GEMINI_VOICE_TEST_ENGINE_ROOT=<hermes-agent checkout> \
    GEMINI_VOICE_TEST_ENGINE_PYTHON=<python with Hermes's dependencies> \
    python -B -m unittest discover -s plugins/gemini-voice/tests -p test_engine_contract.py -v

Without GEMINI_VOICE_TEST_ENGINE_ROOT the test is skipped; a skip is not a pass.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = os.environ.get("GEMINI_VOICE_TEST_ENGINE_ROOT", "")
ENGINE_PYTHON = os.environ.get("GEMINI_VOICE_TEST_ENGINE_PYTHON", sys.executable)
KEEP_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL")


@unittest.skipUnless(ENGINE_ROOT, "set GEMINI_VOICE_TEST_ENGINE_ROOT to run the engine contract probe")
class EngineContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as home:
            env = {k: os.environ[k] for k in KEEP_ENV if k in os.environ}
            env.update(HERMES_HOME=home, PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [ENGINE_PYTHON, "-I", "-B", str(HERE / "engine_contract_probe.py"), ENGINE_ROOT, str(HERE.parent)],
                cwd=home, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        results = json.loads(proc.stdout.strip().splitlines()[-1])
        failed = {name: detail for name, (ok, detail) in results.items() if not ok}
        self.assertEqual(failed, {})
        self.assertGreaterEqual(len(results), 43)


if __name__ == "__main__":
    unittest.main()
