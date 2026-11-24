import json
import subprocess
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fv_eval import evaluate  # noqa: E402


class AIEvalTests(unittest.TestCase):
    def test_good_corpus_passes(self):
        res = evaluate(SKILL / "tests/eval_corpus.json", SKILL / "tests/eval_actual_good.json")
        self.assertEqual(res["summary"]["overall"], "PASS", json.dumps(res, indent=2))

    def test_bad_corpus_fails(self):
        res = evaluate(SKILL / "tests/eval_corpus.json", SKILL / "tests/eval_actual_bad.json")
        self.assertEqual(res["summary"]["overall"], "FAIL")
        self.assertGreater(res["summary"]["failed_cases"], 0)

    def test_cli_exits_nonzero_on_failure(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "fv_eval.py"),
             "--corpus", str(SKILL / "tests/eval_corpus.json"),
             "--actual", str(SKILL / "tests/eval_actual_bad.json")],
            capture_output=True, text=True, cwd=str(SKILL))
        self.assertNotEqual(proc.returncode, 0)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "fv_eval.py"),
             "--corpus", str(SKILL / "tests/eval_corpus.json"),
             "--actual", str(SKILL / "tests/eval_actual_good.json")],
            capture_output=True, text=True, cwd=str(SKILL))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
