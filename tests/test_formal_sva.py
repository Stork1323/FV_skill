"""Tests of the formal-sva skill.

Run from the skill directory:

    python3 -m unittest discover -s tests -v

The unit tests need no external tool.  The integration tests use the checked-in
counter fixture and are skipped when Yosys is not installed.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fv_common import FvError, STATUS_ORDER, load_manifest, status_rank  # noqa: E402
from fv_contract import (  # noqa: E402
    Validation,
    find_calls,
    map_calls,
    validate_contract,
    validate_expr,
)
from fv_design import const_eval, parse_port_decls, resolve_dims  # noqa: E402
from fv_regress import _change, _is_blocking, compare, make_baseline  # noqa: E402
from fv_render import map_expr, substitute_params  # noqa: E402
from fv_extract import extract_spec_sva, merge_spec_sva_requirements  # noqa: E402

COUNTER = SKILL / "examples" / "counter"


def _interface() -> dict:
    return {
        "top": "counter",
        "parameters": {"WIDTH": 4},
        "localparams": {},
        "ports": [
            {"name": "i_clk", "direction": "input", "dims": [], "width": 1},
            {"name": "i_rst_n", "direction": "input", "dims": [], "width": 1},
            {"name": "i_inc", "direction": "input", "dims": [], "width": 1},
            {"name": "q", "direction": "output", "dims": ["[3:0]"], "width": 4},
            {"name": "bus", "direction": "input", "dims": ["[1:0]", "[3:0]"], "width": 8},
        ],
    }


def _has_yosys() -> bool:
    return shutil.which("yosys") is not None


def _copy_counter(tmp: Path) -> Path:
    dest = tmp / "counter"
    shutil.copytree(COUNTER, dest)
    return dest


def _run_cli(*args: str, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str, str]:
    import os, subprocess
    e = os.environ.copy()
    # Tests use the checked-in counter approval.  Gate-specific tests can
    # explicitly opt into the test-only bypass with
    # env={"FV_ALLOW_MISSING_EXTRACTION_APPROVAL":"1"}.
    e.pop("FV_ALLOW_MISSING_EXTRACTION_APPROVAL", None)
    if env:
        e.update(env)
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "fv.py"), *args],
        cwd=str(cwd or SKILL),
        capture_output=True,
        text=True,
        timeout=600,
        env=e,
    )
    return proc.returncode, proc.stdout, proc.stderr


class ExpressionTests(unittest.TestCase):
    def test_arithmetic_and_history_are_valid(self) -> None:
        for expr in (
            "q == prev(q) + 4'd1",
            "rose(i_rst_n) && q == 0",
            "fell(i_inc) || stable(q)",
            "onehot(q) && !onehot0(bus)",
            "bus[0][3:0] == 4'd0",
            "q == 4'b0000",
        ):
            with self.subTest(expr=expr):
                v = Validation()
                validate_expr(expr, _interface(), v, "test")
                self.assertTrue(v.ok, [str(e) for e in v.errors])

    def test_raw_temporal_syntax_is_rejected(self) -> None:
        for expr in (
            "q |-> q",
            "##[1:2] q == 0",
            "$past(q) == 0",
            "always q == 0",
        ):
            with self.subTest(expr=expr):
                v = Validation()
                validate_expr(expr, _interface(), v, "test")
                self.assertFalse(v.ok)

    def test_unknown_signal_and_nested_history_are_rejected(self) -> None:
        v = Validation()
        validate_expr("internal_state == 1", _interface(), v, "test")
        self.assertFalse(v.ok)
        v = Validation()
        validate_expr("prev(rose(q))", _interface(), v, "test")
        self.assertFalse(v.ok)

    def test_history_can_be_disallowed(self) -> None:
        v = Validation()
        validate_expr("prev(q) == q", _interface(), v, "test", allow_history=False)
        self.assertFalse(v.ok)

    def test_find_calls_ignores_sva_builtins(self) -> None:
        # The SVA mapper turns prev(x) into $past(x); find_calls must not
        # match the 'past' inside '$past' on the next pass.
        self.assertEqual(find_calls("$past(q) && $rose(i_rst_n)"), [])
        calls = find_calls("prev(q) && rose(i_rst_n)")
        self.assertEqual([c[0] for c in calls], ["prev", "rose"])

    def test_map_calls_is_idempotent_after_rewrite(self) -> None:
        def mapper(name: str, raw: str, mapped: str) -> str:
            return {"prev": f"$past({mapped})", "rose": f"$rose({mapped})"}[name]

        once = map_calls("prev(q) && rose(i_rst_n)", mapper)
        self.assertEqual(once, "$past(q) && $rose(i_rst_n)")
        self.assertEqual(map_calls(once, mapper), once)


class SpecSvaRequirementTests(unittest.TestCase):
    def test_spec_sva_becomes_compulsory_requirements(self) -> None:
        text = """# Spec
```sv
assert property (@(posedge clk) disable iff (!rst_n) req |-> ##[1:2] ack);
assume property (@(posedge clk) !rst_n |-> !req);
```
"""
        doc = {
            "spec_sva": extract_spec_sva(text),
            "features": [],
            "requirements": [],
            "errors": [],
            "warnings": [],
        }
        self.assertEqual(len(doc["spec_sva"]), 2)
        added = merge_spec_sva_requirements(doc)
        self.assertEqual(added, 2)
        ids = [r["id"] for r in doc["requirements"]]
        self.assertEqual(ids, ["REQ-SVA-001", "REQ-SVA-002"])
        self.assertTrue(all(r.get("spec_sva") for r in doc["requirements"]))
        self.assertEqual(doc["requirements"][0]["proposed"], "response")
        self.assertEqual(doc["requirements"][1]["proposed"], "assume")
        self.assertIn("F-SVA", [f["id"] for f in doc["features"]])
        # Idempotent: re-running extraction must not duplicate requirements.
        self.assertEqual(merge_spec_sva_requirements(doc), 0)
        self.assertEqual(len(doc["requirements"]), 2)

    def test_contract_rejects_missing_or_mismapped_spec_sva(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            project.mkdir()
            (project / "spec").mkdir()
            spec = project / "spec" / "spec.md"
            spec.write_text(
                "# Demo\n\n"
                "While i_rst_n is low, q shall be 0.\n"
                "```sv\n"
                "assert property (@(posedge i_clk) disable iff (!i_rst_n) i_inc |-> ##1 q == $past(q)+1);\n"
                "```\n",
                encoding="utf-8",
            )
            manifest = _manifest_for(project)
            bundle = {
                "design": {"name": "demo", "top": "demo"},
                "spec": {"markdown": "spec/spec.md"},
                "sources": {"spec_markdown": {"sha256": __import__("hashlib").sha256(spec.read_bytes()).hexdigest()}},
                "features": [{"id": "F01", "class": "Mandatory"}],
                "requirements": [
                    {
                        "id": "REQ-001", "feature": "F01", "text": "While i_rst_n is low, q shall be 0.",
                        "clarity": "clear", "sva_hint": "",
                    },
                    {
                        "id": "REQ-SVA-001", "feature": "F-SVA", "text": "Spec SVA",
                        "clarity": "clear", "sva_hint": "",
                        "spec_sva": {
                            "id": "REQ-SVA-001", "role": "assert", "kind": "next",
                            "text": "assert property (@(posedge i_clk) disable iff (!i_rst_n) i_inc |-> ##1 q == $past(q)+1);",
                        },
                    },
                ],
                "spec_sva": [{
                    "text": "assert property (@(posedge i_clk) disable iff (!i_rst_n) i_inc |-> ##1 q == $past(q)+1);",
                    "role": "assert",
                }],
            }
            interface = {
                "top": "demo", "parameters": {}, "localparams": {},
                "ports": [
                    {"name": "i_clk", "direction": "input", "dims": [], "width": 1},
                    {"name": "i_rst_n", "direction": "input", "dims": [], "width": 1},
                    {"name": "i_inc", "direction": "input", "dims": [], "width": 1},
                    {"name": "q", "direction": "output", "dims": ["[3:0]"], "width": 4},
                ],
            }
            contract = {
                "schema_version": "1.0",
                "design": {"name": "demo", "top": "demo",
                           "clock": {"signal": "i_clk", "edge": "posedge"},
                           "reset": {"signal": "i_rst_n", "active": "low", "kind": "async", "cycles": 2},
                           "parameters": {}},
                "requirements": [
                    {"id": "REQ-001", "feature": "F01", "disposition": "formalized",
                     "reason": "Reset clears q while reset is asserted low.",
                     "evidence": {"section": "L3", "quote": "While i_rst_n is low, q shall be 0."},
                     "properties": ["PRP-REQ-001-01"]},
                    {"id": "REQ-SVA-001", "feature": "F-SVA", "disposition": "formalized",
                     "reason": "Spec-authored SVA is compulsory and must be preserved.",
                     "evidence": {"section": "L5", "quote": "assert property (@(posedge i_clk) disable iff (!i_rst_n) i_inc |-> ##1 q == $past(q)+1);"},
                     "properties": ["PRP-REQ-SVA-001-01"]},
                ],
                "properties": [
                    {"id": "PRP-REQ-001-01", "req": "REQ-001", "feature": "F01", "title": "Reset clears q",
                     "kind": "reset_value", "role": "assert", "expr": "q == 4'd0", "disable_reset": False,
                     "evidence": {"section": "L3", "quote": "While i_rst_n is low, q shall be 0."}},
                ],
                "scope": {"configuration": {}, "proven": [], "not_proven": []},
            }
            v = validate_contract(manifest, contract, bundle, interface)
            codes = {e.code for e in v.errors}
            self.assertIn("SPEC_SVA_PROPERTY_MISSING", codes)

            contract["properties"].append({
                "id": "PRP-REQ-SVA-001-01", "req": "REQ-SVA-001", "feature": "F-SVA", "title": "Spec SVA preserved",
                "kind": "next", "role": "assume", "trigger": "i_inc", "expr": "q == prev(q) + 4'd1",
                "evidence": {"section": "L5", "quote": "assert property (@(posedge i_clk) disable iff (!i_rst_n) i_inc |-> ##1 q == $past(q)+1);"},
            })
            v = validate_contract(manifest, contract, bundle, interface)
            codes = {e.code for e in v.errors}
            self.assertIn("SPEC_SVA_ROLE", codes)
            self.assertIn("SPEC_SVA_ID", codes)

            contract["properties"][1]["role"] = "assert"
            contract["properties"][1]["spec_sva_id"] = "REQ-SVA-001"
            v = validate_contract(manifest, contract, bundle, interface)
            self.assertTrue(v.ok, [str(e) for e in v.errors])


class DesignParserTests(unittest.TestCase):
    def test_const_eval(self) -> None:
        self.assertEqual(const_eval("$clog2(8)", {}), 3)
        self.assertEqual(const_eval("WIDTH + 1", {"WIDTH": 7}), 8)
        self.assertEqual(const_eval("8'hff", {}), 255)
        self.assertIsNone(const_eval("UNKNOWN + 1", {}))

    def test_ansi_ports_and_dimensions(self) -> None:
        ports = parse_port_decls(
            "input logic i_clk, input logic [W-1:0] i_data, "
            "output logic [1:0][7:0] o_data"
        )
        self.assertEqual([p["name"] for p in ports], ["i_clk", "i_data", "o_data"])
        dims, width, ok = resolve_dims(ports[1]["dims_src"], {"W": 8})
        self.assertTrue(ok)
        self.assertEqual(width, 8)
        dims, width, ok = resolve_dims(ports[2]["dims_src"], {})
        self.assertTrue(ok)
        self.assertEqual(dims, ["[1:0]", "[7:0]"])
        self.assertEqual(width, 16)


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class RenderTests(unittest.TestCase):
    def test_parameters_are_substituted_in_rendered_expressions(self) -> None:
        self.assertEqual(substitute_params("q == WIDTH - 1", {"WIDTH": 4}), "q == 4 - 1")
        self.assertEqual(
            substitute_params("i_myWIDTH != WIDTH", {"WIDTH": 4}),
            "i_myWIDTH != 4",
        )
        history = {"q": {"name": "fv_prev_q"}}
        self.assertEqual(
            map_expr("q == prev(q) + WIDTH", history, "monitor", {"WIDTH": 4}),
            "q == fv_prev_q + 4",
        )
        self.assertEqual(
            map_expr("q == prev(q) + WIDTH", {}, "sva", {"WIDTH": 4}),
            "q == $past(q) + 4",
        )


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class InitTests(unittest.TestCase):
    def test_init_and_prepare_from_a_bare_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "bare_counter"
            project.mkdir()
            for name in ("spec", "schemas", "src"):
                shutil.copytree(COUNTER / name, project / name)
            rc, out, err = _run_cli("init", "--project", str(project))
            self.assertEqual(rc, 0, out + err)
            design = project / "formal" / "design.json"
            self.assertTrue(design.is_file())
            manifest = json.loads(design.read_text())
            self.assertEqual(manifest["top"], "counter")
            self.assertEqual(manifest["clock"]["signal"], "i_clk")
            self.assertEqual(manifest["reset"]["signal"], "i_rst_n")
            self.assertTrue((project / "formal" / "schemas" / "input_bundle.json").is_file())
            self.assertTrue((project / "formal" / "schemas" / "design_interface.json").is_file())
            # A second init must not overwrite the manifest by accident.
            rc, _out, err = _run_cli("init", "--project", str(project))
            self.assertEqual(rc, 2)
            self.assertIn("already exists", err)


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.project = _copy_counter(Path(cls.tmp.name))
        rc, _out, err = _run_cli("prepare", "--design", str(cls.project / "formal" / "design.json"))
        if rc != 0:
            raise unittest.SkipTest(f"prepare failed: {err}")
        cls.manifest = load_manifest(cls.project / "formal" / "design.json")
        cls.bundle = json.loads((cls.project / "formal" / "schemas" / "input_bundle.json").read_text())
        cls.interface = json.loads((cls.project / "formal" / "schemas" / "design_interface.json").read_text())
        cls.contract = json.loads(
            (cls.project / "formal" / "schemas" / "formal_contract.json").read_text()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def _validate(self, contract: dict) -> Validation:
        return validate_contract(self.manifest, contract, self.bundle, self.interface)

    def test_counter_contract_is_valid(self) -> None:
        v = self._validate(self.contract)
        self.assertTrue(v.ok, [str(e) for e in v.errors])

    def test_every_requirement_needs_a_disposition(self) -> None:
        contract = json.loads(json.dumps(self.contract))
        contract["requirements"] = contract["requirements"][1:]
        v = self._validate(contract)
        self.assertFalse(v.ok)
        self.assertTrue(any(e.code == "REQ_MISSING" for e in v.errors), [str(e) for e in v.errors])

    def test_evidence_must_be_in_the_spec(self) -> None:
        contract = json.loads(json.dumps(self.contract))
        contract["properties"][0]["evidence"]["quote"] = "This sentence is not in the specification at all."
        v = self._validate(contract)
        self.assertFalse(v.ok)
        self.assertTrue(any(e.code.startswith("EVIDENCE") for e in v.errors), [str(e) for e in v.errors])

    def test_unknown_signal_is_rejected(self) -> None:
        contract = json.loads(json.dumps(self.contract))
        contract["properties"][1]["expr"] = "not_a_port == 0"
        v = self._validate(contract)
        self.assertFalse(v.ok)

    def test_partial_formalization_needs_a_limitation(self) -> None:
        contract = json.loads(json.dumps(self.contract))
        req = next(r for r in contract["requirements"] if r["id"] == "REQ-004")
        req["disposition"] = "partially_formalized"
        v = self._validate(contract)
        self.assertFalse(v.ok)
        self.assertTrue(any(e.code == "REQ_LIMITATIONS" for e in v.errors), [str(e) for e in v.errors])
        req["limitations"] = "Only the hold transition is checked; the increment direction is not."
        v = self._validate(contract)
        self.assertTrue(v.ok, [str(e) for e in v.errors])

    def test_reset_value_rejects_history(self) -> None:
        contract = json.loads(json.dumps(self.contract))
        prop = next(p for p in contract["properties"] if p["id"] == "PRP-REQ-001-01")
        prop["expr"] = "q == prev(q)"
        v = self._validate(contract)
        self.assertFalse(v.ok)
        self.assertTrue(any(e.code.startswith("EXPR") for e in v.errors), [str(e) for e in v.errors])


class RegressionLogicTests(unittest.TestCase):
    def test_status_ranking(self) -> None:
        self.assertLess(status_rank("PROVEN"), status_rank("BMC_PASS"))
        self.assertLess(status_rank("BMC_PASS"), status_rank("FAIL"))
        self.assertEqual(_change("PROVEN", "BMC_PASS"), "REGRESSION")
        self.assertEqual(_change("BMC_PASS", "PROVEN"), "IMPROVEMENT")
        self.assertEqual(_change("PROVEN", "PROVEN"), "UNCHANGED")
        self.assertEqual(_change(None, "FAIL"), "NEW")
        self.assertEqual(_change("PROVEN", None), "REMOVED")

    def test_blocking_rules(self) -> None:
        self.assertTrue(_is_blocking("UNCHANGED", "FAIL", "error"))
        self.assertTrue(_is_blocking("REGRESSION", "BMC_PASS", "error"))
        self.assertFalse(_is_blocking("REGRESSION", "BMC_PASS", "info"))
        self.assertFalse(_is_blocking("UNCHANGED", "BMC_PASS", "error"))
        self.assertTrue(_is_blocking("REMOVED", None, "error"))

    def _results(self, status: str, severity: str = "error") -> dict:
        return {
            "design": {"name": "d", "top": "d"},
            "contract": {"path": "formal/schemas/formal_contract.json", "sha256": "abc"},
            "summary": {"overall": "PASS", "statuses": {status: 1}},
            "properties": [{
                "id": "PRP-1", "req": "REQ-1", "status": status,
                "severity": severity, "kind": "always", "role": "assert",
            }],
            "requirements": [],
            "features": [],
        }

    def test_compare_flags_a_weaker_property(self) -> None:
        baseline = make_baseline(self._results("PROVEN"))
        report = compare(self._results("BMC_PASS"), baseline)
        self.assertEqual(report["overall"], "FAIL")
        self.assertTrue(report["properties"][0]["blocking"])
        self.assertEqual(report["properties"][0]["change"], "REGRESSION")

    def test_informational_cover_regression_only_warns(self) -> None:
        baseline = make_baseline(self._results("COVERED", "info"))
        report = compare(self._results("UNREACHABLE", "info"), baseline)
        self.assertEqual(report["overall"], "WARN")
        self.assertFalse(report["properties"][0]["blocking"])

    def test_contract_change_is_reported(self) -> None:
        baseline = make_baseline(self._results("PROVEN"))
        current = self._results("PROVEN")
        current["contract"]["sha256"] = "def"
        report = compare(current, baseline)
        self.assertTrue(report["contract_changed"])


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class CounterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.project = _copy_counter(Path(cls.tmp.name))
        cls.design = cls.project / "formal" / "design.json"
        rc, out, err = _run_cli("all", "--design", str(cls.design), "--jobs", "2")
        if rc != 0:
            raise AssertionError(f"fv.py all failed ({rc}):\n{out}\n{err}")
        cls.results = json.loads(
            (cls.project / "formal" / "results" / "formal_results.json").read_text()
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_statuses(self) -> None:
        self.assertEqual(self.results["summary"]["overall"], "PASS")
        statuses = {p["id"]: p["status"] for p in self.results["properties"]}
        self.assertEqual(statuses["PRP-REQ-001-01"], "BMC_PASS")
        self.assertEqual(statuses["PRP-REQ-003-01"], "PROVEN")
        self.assertEqual(statuses["PRP-REQ-004-01"], "PROVEN")
        self.assertEqual(statuses["PRP-REQ-005-01"], "PROVEN")
        self.assertEqual(statuses["COV-REQ-006-01"], "COVERED")
        self.assertEqual(self.results["summary"]["partial"], 0)

    def test_report_and_junit(self) -> None:
        reports = self.project / "formal" / "reports"
        report = (reports / "report.md").read_text()
        self.assertIn("# Formal verification report: counter", report)
        self.assertIn("## Requirement coverage", report)
        self.assertIn("## Feature coverage", report)
        root = ET.parse(reports / "junit.xml").getroot()
        self.assertEqual(root.tag, "testsuite")
        self.assertEqual(root.get("tests"), "5")
        self.assertEqual(root.get("failures"), "0")

    def test_generated_sva_has_no_raw_contract_syntax(self) -> None:
        sva = (self.project / "formal" / "src" / "generated" / "fv_sva_checker.sv").read_text()
        self.assertIn("assert property", sva)
        self.assertIn("cover property", sva)
        self.assertIn("$past", sva)
        self.assertNotIn("prev(", sva)
        if any(p.get("role") == "assume" for p in self.results["properties"]):
            self.assertIn("assume property", sva)

    def test_parameters_are_baked_into_generated_sources(self) -> None:
        schemas = self.project / "formal" / "schemas"
        contract = json.loads((schemas / "formal_contract.json").read_text())
        for prop in contract["properties"]:
            for key in ("trigger", "expr", "response", "when"):
                if prop.get(key):
                    prop[key] = prop[key].replace("4'd15", "(WIDTH - 1)")
        param_contract = schemas / "formal_contract_params.json"
        param_contract.write_text(json.dumps(contract, indent=2))
        rc, out, err = _run_cli(
            "render", "--design", str(self.design), "--contract", str(param_contract)
        )
        self.assertEqual(rc, 0, out + err)
        generated = self.project / "formal" / "src" / "generated"
        # Wrapper files contain the parameter override on the DUT instance by
        # design; monitors and the standard SVA checker must not.
        paths = sorted(generated.glob("*_monitor.sv")) + [generated / "fv_sva_checker.sv"]
        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotIn("WIDTH", path.read_text())
        # Restore original harness so later tests in this class see a consistent state.
        rc, out, err = _run_cli("render", "--design", str(self.design))
        self.assertEqual(rc, 0, out + err)

    def test_regression_against_committed_baseline(self) -> None:
        rc, out, err = _run_cli("regress", "--design", str(self.design))
        self.assertEqual(rc, 0, out + err)
        report = json.loads(
            (self.project / "formal" / "reports" / "regression.json").read_text()
        )
        self.assertEqual(report["overall"], "PASS")
        self.assertFalse(report["contract_changed"])


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class FailureDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.project = _copy_counter(Path(cls.tmp.name))
        cls.design = cls.project / "formal" / "design.json"
        cls.fail_contract = cls.project / "formal" / "schemas" / "formal_contract_fail.json"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_false_property_is_reported_with_a_counterexample(self) -> None:
        rc, out, err = _run_cli(
            "run", "--design", str(self.design), "--contract", str(self.fail_contract),
            "--jobs", "1",
        )
        self.assertEqual(rc, 1, out + err)
        results = json.loads(
            (self.project / "formal" / "results" / "formal_results.json").read_text()
        )
        prop = next(p for p in results["properties"] if p["id"] == "PRP-REQ-004-02")
        self.assertEqual(prop["status"], "FAIL")
        self.assertTrue(prop["counterexample"])

    def test_explicit_contract_without_a_run_must_match_the_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            design = project / "formal" / "design.json"
            fail_contract = project / "formal" / "schemas" / "formal_contract_fail.json"
            rc, out, err = _run_cli("run", "--design", str(design), "--jobs", "1", "--quiet")
            self.assertEqual(rc, 0, out + err)
            rc, out, err = _run_cli(
                "regress", "--design", str(design), "--contract", str(fail_contract), "--update"
            )
            self.assertEqual(rc, 2, out + err)
            self.assertIn("does not match", err)

    def test_baseline_update_is_refused_after_a_failure(self) -> None:
        rc, out, err = _run_cli(
            "regress", "--design", str(self.design), "--contract", str(self.fail_contract),
            "--run", "--update", "--jobs", "1",
        )
        self.assertEqual(rc, 2, out + err)
        self.assertIn("refusing to update the baseline", err)


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class SuiteTests(unittest.TestCase):
    def test_suite_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            project = _copy_counter(tmpdir)
            design = project / "formal" / "design.json"
            rc, out, err = _run_cli("run", "--design", str(design), "--jobs", "1", "--quiet")
            self.assertEqual(rc, 0, out + err)
            suite = tmpdir / "suite.json"
            suite.write_text(json.dumps({
                "schema_version": "1.0",
                "designs": [{"name": "counter", "design": str(design)}],
            }))
            rc, out, err = _run_cli(
                "regress", "--suite", str(suite), "--out", str(tmpdir / "out")
            )
            self.assertEqual(rc, 0, out + err)
            report = json.loads((tmpdir / "out" / "regression-suite.json").read_text())
            self.assertEqual(report["overall"], "PASS")
            self.assertEqual(report["summary"]["designs"], 1)


class MultiClockContractTests(unittest.TestCase):
    def test_contract_accepts_per_property_clock_reset_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "mc"
            project.mkdir()
            (project / "spec").mkdir()
            spec = project / "spec" / "spec.md"
            spec.write_text("# S\n\nWhile rst_a_n is low, q_a shall be 0.\nWhile rst_b_n is low, q_b shall be 0.\n", encoding="utf-8")
            manifest = _manifest_for(project)
            manifest["clock"] = {"signal": "clk_a", "edge": "posedge"}
            manifest["reset"] = {"signal": "rst_a_n", "active": "low", "kind": "async", "cycles": 2}
            import hashlib
            sha = hashlib.sha256(spec.read_bytes()).hexdigest()
            bundle = {
                "design": {"name": "demo", "top": "demo"},
                "spec": {"markdown": "spec/spec.md"},
                "sources": {"spec_markdown": {"sha256": sha}},
                "features": [{"id": "F01", "class": "Mandatory"}],
                "requirements": [
                    {"id": "REQ-001", "feature": "F01", "text": "While rst_a_n is low, q_a shall be 0.", "clarity": "clear"},
                    {"id": "REQ-002", "feature": "F01", "text": "While rst_b_n is low, q_b shall be 0.", "clarity": "clear"},
                ],
                "spec_sva": [],
            }
            interface = {
                "top": "demo", "parameters": {}, "localparams": {},
                "ports": [
                    {"name": "clk_a", "direction": "input", "dims": [], "width": 1},
                    {"name": "rst_a_n", "direction": "input", "dims": [], "width": 1},
                    {"name": "clk_b", "direction": "input", "dims": [], "width": 1},
                    {"name": "rst_b_n", "direction": "input", "dims": [], "width": 1},
                    {"name": "q_a", "direction": "output", "dims": [], "width": 1},
                    {"name": "q_b", "direction": "output", "dims": [], "width": 1},
                ],
            }
            contract = {
                "schema_version": "1.0",
                "design": {
                    "name": "demo", "top": "demo",
                    "clock": {"signal": "clk_a", "edge": "posedge"},
                    "reset": {"signal": "rst_a_n", "active": "low", "kind": "async", "cycles": 2},
                    "clocks": [
                        {"name": "clk_a", "signal": "clk_a", "edge": "posedge", "domain": "a"},
                        {"name": "clk_b", "signal": "clk_b", "edge": "posedge", "domain": "b"},
                    ],
                    "resets": [
                        {"name": "rst_a", "signal": "rst_a_n", "active": "low", "kind": "async", "cycles": 2, "domain": "a", "clock": "clk_a"},
                        {"name": "rst_b", "signal": "rst_b_n", "active": "low", "kind": "async", "cycles": 3, "domain": "b", "clock": "clk_b"},
                    ],
                    "parameters": {},
                },
                "requirements": [
                    {"id": "REQ-001", "feature": "F01", "disposition": "formalized",
                     "reason": "Domain A reset clears q_a while the domain A reset is asserted.",
                     "evidence": {"section": "L3", "quote": "While rst_a_n is low, q_a shall be 0."},
                     "properties": ["PRP-REQ-001-01"]},
                    {"id": "REQ-002", "feature": "F01", "disposition": "formalized",
                     "reason": "Domain B reset clears q_b while the domain B reset is asserted.",
                     "evidence": {"section": "L4", "quote": "While rst_b_n is low, q_b shall be 0."},
                     "properties": ["PRP-REQ-002-01"]},
                ],
                "properties": [
                    {"id": "PRP-REQ-001-01", "req": "REQ-001", "feature": "F01", "title": "A reset clears q_a",
                     "kind": "reset_value", "role": "assert", "clock": "clk_a", "reset": "rst_a",
                     "expr": "q_a == 1'b0", "disable_reset": False,
                     "evidence": {"section": "L3", "quote": "While rst_a_n is low, q_a shall be 0."}},
                    {"id": "PRP-REQ-002-01", "req": "REQ-002", "feature": "F01", "title": "B reset clears q_b",
                     "kind": "reset_value", "role": "assert", "domain": "b",
                     "expr": "q_b == 1'b0", "disable_reset": False,
                     "evidence": {"section": "L4", "quote": "While rst_b_n is low, q_b shall be 0."}},
                ],
                "scope": {"configuration": {}, "proven": [], "not_proven": []},
            }
            v = validate_contract(manifest, contract, bundle, interface)
            self.assertTrue(v.ok, [str(e) for e in v.errors])

            bad = json.loads(json.dumps(contract))
            bad["properties"][1]["clock"] = "missing_clk"
            v = validate_contract(manifest, bad, bundle, interface)
            self.assertTrue(any(e.code == "PROP_CLOCK" for e in v.errors), [str(e) for e in v.errors])


class WaveformEvidenceTests(unittest.TestCase):
    def test_waveforms_always_generate_vcd_and_svg(self) -> None:
        from fv_waveform import generate_for_results

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "formal" / "results").mkdir(parents=True)
            (root / "formal" / "build").mkdir(parents=True)
            (root / "formal" / "reports").mkdir(parents=True)
            (root / "formal" / "design.json").write_text(json.dumps({
                "schema_version": "1.0", "name": "d", "top": "d",
                "paths": {"results": "formal/results/formal_results.json",
                          "build": "formal/build", "reports": "formal/reports"}
            }))
            manifest = load_manifest(root / "formal" / "design.json")
            results = {
                "schema_version": "1.0",
                "properties": [
                    {"id": "PRP-001", "status": "PROVEN", "kind": "always", "role": "assert"},
                    {"id": "COV-001", "status": "COVERED", "kind": "cover", "role": "cover",
                     "counterexample": ["trace_vcd: formal/build/sby/cov_001/engine_0/trace.vcd"]},
                ],
            }
            (root / "formal" / "results" / "formal_results.json").write_text(json.dumps(results))
            trace = root / "formal" / "build" / "sby" / "cov_001" / "engine_0"
            trace.mkdir(parents=True)
            trace.joinpath("trace.vcd").write_text(
                "$timescale 1ns $end\n$scope module t $end\n$var wire 1 ! clk $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#1\n1!\n"
            )
            out = generate_for_results(manifest)
            self.assertEqual(out["generated"], 2)
            for pid in ("PRP-001", "COV-001"):
                self.assertTrue((root / "formal" / "reports" / "waveforms" / f"{pid}.vcd").is_file())
                self.assertTrue((root / "formal" / "reports" / "waveforms" / f"{pid}.svg").is_file())
                self.assertTrue((root / "formal" / "reports" / "waveforms" / f"{pid}.gtkw").is_file())
                self.assertTrue((root / "formal" / "reports" / "waveforms" / f"{pid}.signals.json").is_file())
            kinds = {a["property"]: a["vcd_kind"] for a in out["artifacts"]}
            self.assertEqual(kinds["PRP-001"], "synthetic_no_trace")
            self.assertEqual(kinds["COV-001"], "trace")


class RepositoryTests(unittest.TestCase):
    def test_all_checked_in_json_parses(self) -> None:
        skip = ("formal/build", "formal/reports", "formal/results", "formal/src")
        for path in sorted(SKILL.rglob("*.json")):
            rel = path.relative_to(SKILL).as_posix()
            if any(part in rel for part in skip):
                continue
            with self.subTest(path=rel):
                json.loads(path.read_text())


class CliTests(unittest.TestCase):
    def test_version(self) -> None:
        rc, out, _err = _run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn("formal-sva", out)

    def test_doctor(self) -> None:
        rc, out, _err = _run_cli("doctor")
        self.assertIn(rc, (0, 1))
        self.assertIn("formal-sva", out)
        self.assertIn("python", out)

    def test_unknown_design_is_a_user_error(self) -> None:
        rc, _out, err = _run_cli("validate", "--design", "/nonexistent/formal/design.json")
        self.assertEqual(rc, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()


# -- new stages ---------------------------------------------------------------------------------


def _manifest_for(project: Path, name: str = "demo", top: str = "demo") -> dict:
    (project / "formal").mkdir(parents=True, exist_ok=True)
    (project / "spec").mkdir(parents=True, exist_ok=True)
    (project / "formal" / "design.json").write_text(json.dumps({
        "schema_version": "1.0",
        "name": name,
        "top": top,
        "spec": {"markdown": "spec/spec.md"},
        "clock": {"signal": "i_clk", "edge": "posedge"},
        "reset": {"signal": "i_rst_n", "active": "low", "kind": "async", "cycles": 2},
        "formal": {"backend": "yosys", "depth": 8},
    }, indent=2))
    return load_manifest(project / "formal" / "design.json")


DEMO_SPEC = "\n".join([
    "# Demo",
    "",
    "## Features",
    "",
    "| ID | Class | Statement | Source |",
    "|---|---|---|---|",
    "| F01 | Mandatory | Reset behavior | L9 |",
    "| F02 | Mandatory | Counting behavior | L10 |",
    "While i_rst_n is low, q shall be 0.",
    "When i_inc is 1, q shall increment.",
    "",
])


def _demo_requirements(clarity: str = "clear", question: str = "",
                       blocking: str = "no", evidence: str | None = None) -> str:
    quote = evidence or "While i_rst_n is low, q shall be 0."
    lines = [
        "---",
        "schema_version: 1.0",
        "design: demo",
        "top: demo",
        "spec: spec/spec.md",
        "---",
        "",
        "# Extraction",
        "",
        "## Features",
        "",
        "| ID | Class | Statement | Source |",
        "|---|---|---|---|",
        "| `F01` | Mandatory | Reset behavior | L9 |",
        "| `F02` | Mandatory | Counting behavior | L10 |",
        "",
        "## Requirements",
        "",
        "### REQ-001 -- Reset",
        "",
        "- **Feature:** F01",
        "- **Category:** reset",
        "- **Statement:** While i_rst_n is low, q shall be 0.",
        "- **Source:** L9",
        f'- **Evidence:** "{quote}"',
        f"- **Clarity:** {clarity}",
        "- **Proposed property:** reset_value",
        "- **Owner:** spec_agent",
        f"- **Blocking:** {blocking}",
    ]
    if question:
        lines.append(f"- **Question:** {question}")
    lines += [
        "",
        "### REQ-002 -- Counting",
        "",
        "- **Feature:** F02",
        "- **Category:** functional",
        "- **Statement:** When i_inc is 1, q shall increment.",
        "- **Source:** L10",
        '- **Evidence:** "When i_inc is 1, q shall increment."',
        "- **Clarity:** clear",
        "- **Proposed property:** always",
        "- **Owner:** spec_agent",
        "- **Blocking:** no",
        "",
    ]
    return "\n".join(lines)


class ExtractionTests(unittest.TestCase):
    def test_spec_only_scaffold_then_parse(self) -> None:
        from fv_extract import extract

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            doc = extract(manifest, force=True, quiet=True)
            self.assertFalse(doc["errors"], doc["errors"])
            self.assertEqual(doc["stats"]["requirements"], 0)
            self.assertEqual(len(doc["features"]), 2)
            self.assertTrue((project / "formal" / "spec" / "requirements.md").is_file())

            (project / "formal" / "spec" / "requirements.md").write_text(_demo_requirements())
            doc = extract(manifest, quiet=True)
            self.assertTrue(doc["stats"]["ready"], doc["errors"])
            self.assertEqual(doc["stats"]["clear"], 2)
            self.assertEqual(doc["requirements"][0]["source"], "L9")
            self.assertEqual(doc["requirements"][0]["proposed"], "reset_value")

    def test_bad_evidence_is_an_error(self) -> None:
        from fv_extract import extract

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            (project / "formal" / "spec").mkdir(parents=True)
            (project / "formal" / "spec" / "requirements.md").write_text(
                _demo_requirements(evidence="This sentence is not in the specification."))
            doc = extract(manifest, quiet=True)
            self.assertFalse(doc["stats"]["ready"])
            self.assertTrue(any("evidence" in e.lower() for e in doc["errors"]), doc["errors"])

    def test_unclear_requirement_needs_a_question_and_produces_one(self) -> None:
        from fv_extract import extract
        from fv_questions import collect

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            (project / "formal" / "spec").mkdir(parents=True)
            md = _demo_requirements(clarity="ambiguous", blocking="yes")
            (project / "formal" / "spec" / "requirements.md").write_text(md)
            doc = extract(manifest, quiet=True)
            self.assertTrue(any("requires a Question" in e for e in doc["errors"]), doc["errors"])

            (project / "formal" / "spec" / "requirements.md").write_text(
                _demo_requirements(clarity="ambiguous", blocking="yes",
                                   question="Is the reset synchronous or asynchronous?"))
            doc = extract(manifest, quiet=True)
            self.assertTrue(doc["stats"]["ready"], doc["errors"])
            questions = collect(manifest, bundle=doc, contract={})
            self.assertEqual(questions["summary"]["open"], 1)
            self.assertEqual(questions["summary"]["blocking"], 1)
            self.assertEqual(questions["questions"][0]["owner"], "spec_agent")

            (project / "formal" / "spec" / "answers.json").write_text(json.dumps({
                "schema_version": "1.0",
                "answers": {"Q-REQ-001-01": {"status": "answered",
                                             "answer": "Asynchronous, active low."}},
            }, indent=2))
            questions = collect(manifest, bundle=doc, contract={})
            self.assertEqual(questions["summary"]["open"], 0)
            self.assertEqual(questions["summary"]["answered"], 1)

    def test_missing_feature_and_source_are_errors(self) -> None:
        from fv_extract import extract

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            (project / "formal" / "spec").mkdir(parents=True)
            (project / "formal" / "spec" / "requirements.md").write_text(
                _demo_requirements().replace("- **Feature:** F01", "- **Feature:** F99")
                .replace("- **Source:** L9", "- **Source:**"))
            doc = extract(manifest, quiet=True)
            messages = "\n".join(doc["errors"])
            self.assertIn("not in the feature table", messages)
            self.assertIn("missing Source", messages)

    def test_unknown_owner_and_malformed_source_are_errors(self) -> None:
        from fv_extract import extract

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            (project / "formal" / "spec").mkdir(parents=True)
            text = (_demo_requirements()
                    .replace("- **Owner:** spec_agent", "- **Owner:** somebody")
                    .replace("- **Source:** L9", "- **Source:** section 4"))
            (project / "formal" / "spec" / "requirements.md").write_text(text)
            doc = extract(manifest, quiet=True)
            messages = "\n".join(doc["errors"])
            self.assertIn("Owner", messages)
            self.assertIn("is not L<n>", messages)


class ContractClarityTests(unittest.TestCase):
    def test_unclear_requirement_cannot_be_formalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            from fv_bundle import prepare

            manifest = load_manifest(project / "formal" / "design.json")
            bundle = prepare(manifest, quiet=True)
            bundle = json.loads(json.dumps(bundle))
            next(r for r in bundle["requirements"] if r["id"] == "REQ-001")["clarity"] = "ambiguous"
            (project / "formal" / "schemas" / "input_bundle.json").write_text(json.dumps(bundle))
            interface = json.loads((project / "formal" / "schemas" / "design_interface.json").read_text())
            contract = json.loads(
                (project / "formal" / "schemas" / "formal_contract.json").read_text())
            v = validate_contract(manifest, contract, bundle, interface)
            self.assertFalse(v.ok)
            self.assertTrue(any(e.code == "REQ_UNCLEAR_FORMALIZED" for e in v.errors),
                            [str(e) for e in v.errors])


class ToolConfigTests(unittest.TestCase):
    def test_default_backend_is_symbiyosys(self) -> None:
        from fv_tools import backend_name, ensure_tools, load_tools

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            manifest["_formal"]["backend"] = "auto"
            path = ensure_tools(manifest)
            self.assertTrue(path.is_file())
            tools = load_tools(manifest, create=False)
            self.assertEqual(tools["default"], "symbiyosys")
            self.assertEqual(backend_name(manifest, tools), "symbiyosys")
            manifest["_formal"]["backend"] = "yosys"
            self.assertEqual(backend_name(manifest, tools), "yosys")
            self.assertEqual(backend_name(manifest, tools, "vc_formal"), "vc_formal")

    def test_invalid_result_pattern_is_reported(self) -> None:
        from fv_tools import load_tools, validate_tools, tools_path

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            from fv_tools import ensure_tools

            ensure_tools(manifest)
            path = tools_path(manifest)
            tools = json.loads(path.read_text())
            tools["backends"]["vc_formal"]["result_pattern"] = "(unclosed"
            errors = validate_tools(tools, path)
            self.assertTrue(any("result_pattern" in e for e in errors), errors)

    def test_unknown_backend_is_rejected(self) -> None:
        from fv_tools import backend_name, ensure_tools, load_tools

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            ensure_tools(manifest)
            tools = load_tools(manifest, create=False)
            with self.assertRaises(FvError):
                backend_name(manifest, tools, "does_not_exist")


class RuleTests(unittest.TestCase):
    def test_rules_are_generated_hashed_and_checked(self) -> None:
        from fv_rules import check, ensure, generate, rules_hash

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            doc = generate(manifest, quiet=True)
            self.assertEqual(len(doc["files"]), 8)
            self.assertEqual(rules_hash(manifest), doc["rules_sha256"])
            self.assertFalse(check(manifest))
            self.assertEqual(ensure(manifest)["rules_sha256"], doc["rules_sha256"])
            path = project / "formal" / "rules" / "20-contract.md"
            path.write_text(path.read_text() + "\nlocal edit\n")
            errors = check(manifest)
            self.assertTrue(any("modified" in e for e in errors), errors)

    def test_rules_substitute_the_project_paths(self) -> None:
        from fv_rules import generate

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            generate(manifest, quiet=True)
            text = (project / "formal" / "rules" / "00-workflow.md").read_text()
            self.assertIn("spec/spec.md", text)
            self.assertIn("generated by formal-sva", text)

    def test_consistent_rules_refuse_a_hand_edit(self) -> None:
        from fv_rules import ensure_consistent, generate

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            generated = generate(manifest, quiet=True)
            self.assertEqual(ensure_consistent(manifest)["rules_sha256"],
                             generated["rules_sha256"])
            path = project / "formal" / "rules" / "50-review.md"
            path.write_text(path.read_text() + "\nlocal edit\n")
            with self.assertRaises(FvError):
                ensure_consistent(manifest)

    def test_rules_refresh_after_a_spec_change(self) -> None:
        from fv_rules import ensure, generate

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            spec = project / "spec" / "spec.md"
            spec.write_text(DEMO_SPEC)
            first = generate(manifest, quiet=True)
            spec.write_text(DEMO_SPEC + "\nA new normative sentence.\n")
            second = ensure(manifest)
            self.assertNotEqual(first["spec_sha256"], second["spec_sha256"])
            self.assertEqual(second["spec_sha256"],
                             __import__("hashlib").sha256(spec.read_bytes()).hexdigest())


class ReviewTests(unittest.TestCase):
    def test_review_finds_vacuity_duplicates_and_constants(self) -> None:
        from fv_review import review, write_review

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            schemas = project / "formal" / "schemas"
            schemas.mkdir(parents=True)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            bundle = {
                "design": {"name": "demo", "top": "demo"},
                "features": [{"id": "F01", "class": "Mandatory", "requirements": ["REQ-001"]}],
                "requirements": [{"id": "REQ-001", "feature": "F01",
                                  "text": "While i_rst_n is low, q shall be 0.",
                                  "clarity": "clear"}],
                "spec": {"markdown": "spec/spec.md"},
            }
            interface = {
                "top": "demo", "parameters": {"WIDTH": 4}, "localparams": {},
                "ports": [
                    {"name": "i_clk", "direction": "input", "dims": [], "width": 1},
                    {"name": "i_rst_n", "direction": "input", "dims": [], "width": 1},
                    {"name": "i_inc", "direction": "input", "dims": [], "width": 1},
                    {"name": "q", "direction": "output", "dims": ["[3:0]"], "width": 4},
                ],
            }
            contract = {
                "schema_version": "1.0",
                "design": {"name": "demo", "top": "demo",
                           "clock": {"signal": "i_clk", "edge": "posedge"},
                           "reset": {"signal": "i_rst_n", "active": "low", "kind": "async"},
                           "parameters": {"WIDTH": 4}},
                "requirements": [{
                    "id": "REQ-001", "feature": "F01", "disposition": "formalized",
                    "reason": "Checked by the generated properties for the reset behavior.",
                }],
                "properties": [
                    {"id": "A1", "req": "REQ-001", "feature": "F01", "role": "assert",
                     "kind": "always", "expr": "1"},
                    {"id": "A2", "req": "REQ-001", "feature": "F01", "role": "assert",
                     "kind": "always", "expr": "1"},
                    {"id": "A3", "req": "REQ-001", "feature": "F01", "role": "assert",
                     "kind": "next", "trigger": "i_inc", "expr": "q == 4'd1"},
                    {"id": "AS1", "req": "REQ-001", "feature": "F01", "role": "assume",
                     "kind": "always", "expr": "i_inc"},
                    {"id": "C1", "req": "REQ-001", "feature": "F01", "role": "cover",
                     "kind": "cover", "expr": "q == 4'd3"},
                ],
                "scope": {"configuration": {"WIDTH": 4}},
            }
            (schemas / "input_bundle.json").write_text(json.dumps(bundle))
            (schemas / "design_interface.json").write_text(json.dumps(interface))
            (schemas / "formal_contract.json").write_text(json.dumps(contract))
            results = {
                "design": {"name": "demo", "top": "demo"},
                "properties": [
                    {"id": "A1", "status": "BMC_PASS"},
                    {"id": "A2", "status": "BMC_PASS"},
                    {"id": "A3", "status": "BMC_PASS"},
                    {"id": "AS1", "status": "VACUOUS"},
                    {"id": "C1", "status": "UNREACHABLE"},
                ],
            }
            doc = review(manifest, results)
            codes = {f["code"] for f in doc["findings"]}
            for code in ("CONSTRAINT_UNSAT", "CONSTRAINT_UNGUARDED",
                         "PROPERTY_CONSTANT", "PROPERTY_TAUTOLOGY",
                         "PROPERTY_DUPLICATE", "TRIGGER_UNCOVERED",
                         "COVER_UNREACHABLE"):
                self.assertIn(code, codes, sorted(codes))
            self.assertEqual(doc["overall"], "ACTION_REQUIRED")
            artifacts = write_review(manifest, doc)
            self.assertTrue((project / artifacts["review_md"]).is_file())


class CommandBackendTests(unittest.TestCase):
    def _harness(self, root: Path) -> dict:
        return {
            "_root": str(root),
            "_paths": {"build": str(root / "formal" / "build"),
                       "reports": str(root / "formal" / "reports")},
            "design": {"top": "demo", "reset_cycles": 2},
            "formal": {"depth": 4, "timeout_s": 10, "induction": False},
            "files": {"dut": [], "include_dirs": [], "defines": [], "assumptions": None},
        }

    def _cfg(self) -> dict:
        return {
            "name": "fake_formal",
            "kind": "command",
            "enabled": True,
            "command": ["/bin/sh", "{script}"],
            "script_template": "#!/bin/sh\necho \"RESULT: {status}\"\n{extra}",
            "script_extension": ".sh",
            "result_pattern": r"^RESULT:\s*(?P<status>PROVEN|PASS|FAIL|ERROR|TIMEOUT)\b",
            "timeout_s": 10,
        }

    def test_proven_result(self) -> None:
        from fv_command import run_property

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg()
            cfg["script_template"] = cfg["script_template"].replace("{status}", "PROVEN").replace("{extra}", "")
            prop = {"id": "P1", "mode": "assert", "kind": "always", "role": "assert",
                    "file_san": "p1", "top": "p1_top", "depth": 4}
            result = run_property(self._harness(root), prop, cfg, quiet=True)
            self.assertEqual(result["status"], "PROVEN", result)
            self.assertTrue((root / result["bmc"]["script"]).is_file())

    def test_fail_result_keeps_a_counterexample(self) -> None:
        from fv_command import run_property

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg()
            cfg["script_template"] = (
                "#!/bin/sh\necho \"RESULT: FAIL\"\necho \"  3  q = 1\"\n")
            prop = {"id": "P1", "mode": "assert", "kind": "always", "role": "assert",
                    "file_san": "p1", "top": "p1_top", "depth": 4}
            result = run_property(self._harness(root), prop, cfg, quiet=True)
            self.assertEqual(result["status"], "FAIL")
            self.assertTrue(result["counterexample"], result)

    def test_unrecognised_output_is_an_error(self) -> None:
        from fv_command import run_property

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg()
            cfg["script_template"] = "#!/bin/sh\necho \"hello\"\n"
            prop = {"id": "P1", "mode": "assert", "kind": "always", "role": "assert",
                    "file_san": "p1", "top": "p1_top", "depth": 4}
            result = run_property(self._harness(root), prop, cfg, quiet=True)
            self.assertEqual(result["status"], "ERROR")
            self.assertIn("result_pattern", result["error"])


class SymbiYosysRenderingTests(unittest.TestCase):
    def test_sby_text_uses_the_configured_engine_and_mode(self) -> None:
        from fv_sby import _sby_text

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "dut.sv"
            source.write_text("module demo; endmodule\n")
            harness = {
                "_root": str(root),
                "formal": {"depth": 8},
                "files": {"dut": [str(source)], "include_dirs": [], "defines": []},
            }
            text = _sby_text(harness, {"top": "demo_top"}, "bmc", 6,
                             [source], {"engine": "abc"})
            self.assertIn("mode bmc", text)
            self.assertIn("depth 6", text)
            self.assertIn("abc", text)
            self.assertIn("prep -top demo_top -flatten", text)

    def test_sby_output_classification(self) -> None:
        from fv_sby import _parse

        self.assertEqual(_parse("SBY DONE (PASS, rc=0)", "bmc")["result"], "pass")
        self.assertEqual(_parse("SBY DONE (FAIL, rc=1)", "bmc")["result"], "fail")
        self.assertEqual(_parse("Induction step proven: SUCCESS!", "prove")["result"], "proven")
        self.assertEqual(_parse("DONE (TIMEOUT, rc=124)", "bmc")["result"], "timeout")
        self.assertEqual(_parse("no result here", "bmc")["result"], "unknown")


class PipelineStateTests(unittest.TestCase):
    def test_missing_extraction_fails_the_first_step(self) -> None:
        from fv_pipeline import run, status

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            result = run(manifest, quiet=True)
            self.assertEqual(result["overall"], "FAIL")
            self.assertEqual(result["failed_step"], "extract")
            rows = {r["step"]: r for r in status(manifest)}
            self.assertEqual(rows["rules"]["status"], "ok")
            self.assertEqual(rows["extract"]["status"], "failed")
            self.assertEqual(rows["contract"]["status"], "missing")

    def test_status_cli_reports_missing_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            _manifest_for(project)
            rc, out, _err = _run_cli("status", "--design",
                                     str(project / "formal" / "design.json"))
            self.assertEqual(rc, 1)
            self.assertIn("INCOMPLETE", out)
            self.assertIn("extract", out)


class SpecOnlyWorkflowTests(unittest.TestCase):
    def test_changed_spec_invalidates_the_extraction(self) -> None:
        from fv_extract import extract, update_spec_hash

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "design"
            manifest = _manifest_for(project)
            (project / "spec" / "spec.md").write_text(DEMO_SPEC)
            (project / "formal" / "spec").mkdir(parents=True)
            text = _demo_requirements().replace(
                "spec: spec/spec.md\n",
                "spec: spec/spec.md\nspec_sha256: deadbeef\n",
            )
            (project / "formal" / "spec" / "requirements.md").write_text(text)
            doc = extract(manifest, quiet=True)
            self.assertFalse(doc["stats"]["ready"])
            self.assertTrue(any("spec.md changed" in e for e in doc["errors"]),
                            doc["errors"])

            refresh = update_spec_hash(manifest)
            self.assertTrue(refresh["changed"])
            self.assertIn(refresh["spec_sha256"],
                          (project / "formal" / "spec" / "requirements.md").read_text())
            doc = extract(manifest, quiet=True)
            self.assertTrue(doc["stats"]["ready"], doc["errors"])
            self.assertFalse(update_spec_hash(manifest)["changed"])

    def test_init_spec_only_stops_at_the_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "bare"
            project.mkdir()
            shutil.copytree(COUNTER / "spec", project / "spec")
            shutil.copytree(COUNTER / "src", project / "src")
            rc, out, err = _run_cli(
                "init", "--project", str(project), "--top", "counter",
                "--spec", str(project / "spec" / "spec.md"),
                "--rtl", str(project / "src" / "rtl" / "filelist.f"))
            self.assertEqual(rc, 0, out + err)
            formal = project / "formal"
            self.assertTrue((formal / "spec" / "requirements.md").is_file())
            self.assertFalse((formal / "schemas" / "input_bundle.json").is_file())
            self.assertIn("prepare skipped", out)
            tools = json.loads((formal / "tools.json").read_text())
            self.assertEqual(tools["default"], "symbiyosys")
            self.assertTrue((formal / "rules" / "manifest.json").is_file())
            metadata = json.loads((formal / "spec" / "requirements.json").read_text())
            self.assertEqual(metadata["stats"]["requirements"], 0)


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class BundleFreshnessTests(unittest.TestCase):
    def test_load_contract_rebuilds_a_stale_bundle(self) -> None:
        from fv_bundle import prepare
        from fv_contract import _bundle_is_current, load_contract

        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            manifest = load_manifest(project / "formal" / "design.json")
            prepare(manifest, quiet=True)
            paths = {k: Path(v) for k, v in manifest["_paths"].items()}
            self.assertTrue(_bundle_is_current(manifest, paths))

            requirements = paths["requirements"]
            text = requirements.read_text().replace(
                "- **Statement:** While i_rst_n is low, q shall be 4'b0000.",
                "- **Statement:** While i_rst_n is low, q shall be 4'b0000. Reviewed.",
                1)
            requirements.write_text(text)
            self.assertFalse(_bundle_is_current(manifest, paths))

            _contract, bundle, _interface = load_contract(manifest)
            entry = next(r for r in bundle["requirements"] if r["id"] == "REQ-001")
            self.assertIn("Reviewed", entry["text"])
            self.assertTrue(_bundle_is_current(manifest, paths))


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class SpecOnlyIntegrationTests(unittest.TestCase):
    def test_spec_only_project_runs_the_ordered_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "counter"
            project.mkdir()
            shutil.copytree(COUNTER / "spec", project / "spec")
            shutil.copytree(COUNTER / "src", project / "src")
            formal = project / "formal"
            (formal / "schemas").mkdir(parents=True)
            (formal / "regression").mkdir(parents=True)

            rc, out, err = _run_cli(
                "init", "--project", str(project), "--top", "counter",
                "--spec", str(project / "spec" / "spec.md"),
                "--rtl", str(project / "src" / "rtl" / "filelist.f"))
            self.assertEqual(rc, 0, out + err)
            shutil.copy(COUNTER / "formal" / "spec" / "requirements.md",
                        formal / "spec" / "requirements.md")
            shutil.copy(COUNTER / "formal" / "spec" / "extraction_approval.json",
                        formal / "spec" / "extraction_approval.json")
            shutil.copy(COUNTER / "formal" / "schemas" / "formal_contract.json",
                        formal / "schemas" / "formal_contract.json")
            shutil.copy(COUNTER / "formal" / "regression" / "baseline.json",
                        formal / "regression" / "baseline.json")

            rc, out, err = _run_cli(
                "all", "--design", str(formal / "design.json"),
                "--backend", "yosys", "--jobs", "2")
            self.assertEqual(rc, 0, out + err)
            state = json.loads((formal / "state" / "pipeline.json").read_text())
            self.assertEqual(
                [s for s in state["steps"]],
                ["rules", "extract", "contract", "render", "run", "review", "regress"])
            results = json.loads((formal / "results" / "formal_results.json").read_text())
            self.assertEqual(results["summary"]["overall"], "PASS")
            self.assertEqual(results["backend"]["name"], "yosys")
            self.assertEqual(len(results["requirements"]), 7)
            self.assertTrue((formal / "reports" / "questions.json").is_file())
            self.assertTrue((formal / "reports" / "sva_review.json").is_file())


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class PipelineGateTests(unittest.TestCase):
    def test_hand_edited_rule_file_stops_the_rules_step(self) -> None:
        from fv_pipeline import run, status

        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            manifest = load_manifest(project / "formal" / "design.json")
            from fv_rules import generate
            generate(manifest, force=True, quiet=True)
            rule = project / "formal" / "rules" / "50-review.md"
            rule.write_text(rule.read_text() + "\nlocal edit\n")
            result = run(manifest, quiet=True, backend="yosys", jobs=2)
            self.assertEqual(result["overall"], "FAIL")
            self.assertEqual(result["failed_step"], "rules")
            rows = {r["step"]: r for r in status(manifest)}
            self.assertEqual(rows["rules"]["status"], "failed")
            self.assertIn(rows["extract"]["status"], ("missing", "stale"))

    def test_baseline_update_is_refused_while_review_requires_action(self) -> None:
        from fv_pipeline import run

        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            manifest = load_manifest(project / "formal" / "design.json")
            contract_path = project / "formal" / "schemas" / "formal_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["properties"].append({
                "id": "PRP-REQ-001-99",
                "req": "REQ-001",
                "feature": "F01",
                "title": "constant probe",
                "kind": "always",
                "role": "assert",
                "expr": "1",
                "severity": "error",
                "evidence": {
                    "section": "L37",
                    "quote": "While `i_rst_n` is low, `q` shall be `4'b0000`. "
                             "Reset assertion is asynchronous;",
                },
            })
            contract_path.write_text(json.dumps(contract, indent=2))
            baseline = project / "formal" / "regression" / "baseline.json"
            before = baseline.read_bytes()

            result = run(manifest, update_baseline=True, quiet=True,
                         backend="yosys", jobs=2)
            self.assertEqual(result["overall"], "FAIL")
            self.assertEqual(result["failed_step"], "regress")
            self.assertIn("refusing to update", result["error"])
            self.assertEqual(baseline.read_bytes(), before)
            review = json.loads(
                (project / "formal" / "reports" / "sva_review.json").read_text())
            self.assertEqual(review["overall"], "ACTION_REQUIRED")
            self.assertTrue(any(f["code"] == "PROPERTY_TAUTOLOGY"
                                for f in review["findings"]))

            # The explicit exception is the only way to record this baseline.
            result = run(manifest, update_baseline=True, quiet=True,
                         backend="yosys", jobs=2, allow_review_errors=True)
            self.assertEqual(result["overall"], "PASS", result)
            self.assertNotEqual(baseline.read_bytes(), before)


@unittest.skipUnless(_has_yosys(), "yosys is not installed")
class StandaloneBaselineGateTests(unittest.TestCase):
    def test_regress_update_is_refused_when_review_requires_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = _copy_counter(Path(tmp))
            design = project / "formal" / "design.json"
            contract_path = project / "formal" / "schemas" / "formal_contract.json"
            contract = json.loads(contract_path.read_text())
            contract["properties"].append({
                "id": "PRP-REQ-001-98",
                "req": "REQ-001",
                "feature": "F01",
                "title": "constant probe",
                "kind": "always",
                "role": "assert",
                "expr": "1",
                "severity": "error",
                "evidence": {
                    "section": "L37",
                    "quote": "While `i_rst_n` is low, `q` shall be `4'b0000`. "
                             "Reset assertion is asynchronous;",
                },
            })
            contract_path.write_text(json.dumps(contract, indent=2))
            baseline = project / "formal" / "regression" / "baseline.json"
            before = baseline.read_bytes()

            rc, out, err = _run_cli(
                "regress", "--design", str(design), "--update", "--run",
                "--jobs", "2")
            self.assertEqual(rc, 1, out + err)
            self.assertIn("not updated", out + err)
            self.assertEqual(baseline.read_bytes(), before)

            rc, out, err = _run_cli(
                "regress", "--design", str(design), "--update", "--run",
                "--jobs", "2", "--allow-review-errors")
            self.assertEqual(rc, 0, out + err)
            self.assertNotEqual(baseline.read_bytes(), before)
