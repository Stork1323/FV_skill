import json
import tempfile
import unittest
from pathlib import Path
import sys

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fv_regress import _change  # noqa: E402
from fv_run import check_results_binding, compute_signoff  # noqa: E402


class SemanticRegressionTests(unittest.TestCase):
    def test_trigger_false_is_regression_even_if_proven(self):
        base = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "next", "trigger": "a && b", "expr": "c"}}
        cur = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "next", "trigger": "1'b0", "expr": "c"}}
        self.assertEqual(_change(base, cur), "REGRESSION")

    def test_weakened_consequent_is_regression(self):
        base = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "always", "expr": "a == b"}}
        cur = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "always", "expr": "1'b1"}}
        self.assertEqual(_change(base, cur), "REGRESSION")

    def test_strengthened_assumption_is_regression(self):
        base = {"status": "ASSUMED", "semantic": {"role": "assume", "kind": "always", "expr": "a"}}
        cur = {"status": "ASSUMED", "semantic": {"role": "assume", "kind": "always", "expr": "a && b"}}
        self.assertEqual(_change(base, cur), "REGRESSION")

    def test_removed_obligation_is_regression(self):
        base = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "always", "expr": "a", "obligation": "safety"}}
        cur = {"status": "PROVEN", "semantic": {"role": "assert", "kind": "always", "expr": "a"}}
        self.assertEqual(_change(base, cur), "REGRESSION")


class SignoffBindingTests(unittest.TestCase):
    def test_unresolved_obligation_blocks_signoff(self):
        results = {
            "summary": {"overall": "PASS"},
            "requirements": [{"id": "REQ-1", "disposition": "formalized", "status": "INCOMPLETE_OBLIGATION"}],
            "properties": [],
            "review": {"overall": "OK"},
        }
        signoff = compute_signoff(results)
        self.assertFalse(signoff["strict_signoff_ready"])
        self.assertTrue(any("REQ-1" in r for r in signoff["blocking_reasons"]))

    def test_approved_exception_allows_signoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "formal").mkdir(parents=True)
            (root / "formal" / "signoff_exceptions.json").write_text(json.dumps({
                "exceptions": [{"id": "REQ-1", "approved": True, "reason": "test"}]
            }), encoding="utf-8")
            manifest = {
                "project": {"root": "."},
                "_root": str(root),
                "_paths": {
                    "root": str(root),
                    "contract": str(root / "formal/schemas/formal_contract.json"),
                    "results": str(root / "formal/results/formal_results.json"),
                },
            }
            results = {
                "summary": {"overall": "PASS"},
                "requirements": [{"id": "REQ-1", "disposition": "formalized", "status": "INCOMPLETE_OBLIGATION"}],
                "properties": [],
                "review": {"overall": "OK"},
            }
            signoff = compute_signoff(results, manifest)
            self.assertTrue(signoff["strict_signoff_ready"], signoff)
            self.assertIn("REQ-1", signoff["exceptions_applied"])

    def test_stale_results_binding_rejected(self):
        results = {"contract": {"sha256": "old"}, "properties": [{"id": "P1", "status": "PROVEN", "config": {"backend": "yosys"}, "artifact_hashes": {"contract_sha256": "old"}}], "run_id": "r1"}
        errs = check_results_binding(results)
        self.assertEqual(errs, [])
        results2 = {"contract": {}, "properties": []}
        errs2 = check_results_binding(results2)
        self.assertTrue(any("run_id" in e for e in errs2))
