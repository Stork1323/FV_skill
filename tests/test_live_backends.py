from __future__ import annotations
import json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL / "scripts"
COUNTER = SKILL / "examples" / "counter"


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "fv.py"), *args],
                          cwd=str(cwd or SKILL), capture_output=True, text=True)


def _copy_counter(dst: Path) -> Path:
    shutil.copytree(COUNTER, dst / "counter")
    return dst / "counter"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_text(text: str) -> dict:
    return json.loads(text)


@unittest.skipUnless(_has("yosys"), "yosys required")
class LiveYosysQualification(unittest.TestCase):
    def test_pass_fail_cover(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _copy_counter(Path(td))
            design = proj / "formal" / "design.json"
            r = _run("run", "--design", str(design), "--backend", "yosys", "--jobs", "1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            statuses = {p["id"]: p["status"] for p in res["properties"]}
            self.assertIn(statuses["COV-REQ-006-01"], ("COVERED",))
            self.assertTrue(any(s in ("PROVEN", "BMC_PASS") for s in statuses.values()))

            # FAIL case
            r = _run("run", "--design", str(design), "--backend", "yosys", "--jobs", "1",
                     "--contract", str(proj / "formal" / "schemas" / "formal_contract_fail.json"))
            self.assertNotEqual(r.returncode, 0)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            self.assertTrue(any(p["status"] == "FAIL" and p.get("counterexample") for p in res["properties"]))


@unittest.skipUnless(_has("sby"), "sby required")
class LiveSBYQualification(unittest.TestCase):
    def test_pass_fail_cover_not_reached(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _copy_counter(Path(td))
            design = proj / "formal" / "design.json"
            r = _run("run", "--design", str(design), "--backend", "symbiyosys", "--jobs", "2")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            statuses = {p["id"]: p["status"] for p in res["properties"]}
            self.assertEqual(statuses["COV-REQ-006-01"], "COVERED")
            self.assertTrue(any(s == "PROVEN" for s in statuses.values()))

            # FAIL
            r = _run("run", "--design", str(design), "--backend", "symbiyosys", "--jobs", "1",
                     "--contract", str(proj / "formal" / "schemas" / "formal_contract_fail.json"))
            self.assertNotEqual(r.returncode, 0)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            fail = [p for p in res["properties"] if p["status"] == "FAIL"]
            self.assertTrue(fail)
            self.assertTrue(fail[0].get("counterexample"))

            # replay (best effort)
            import shutil as _sh
            if _sh.which("iverilog") and _sh.which("vvp"):
                rr = _run("replay", "--design", str(design), "--property", "PRP-REQ-004-02", "--json")
                # replay may be SKIPPED/UNAVAILABLE/REPLAYED_FAIL depending on trace; don't hard-fail on env
                if rr.returncode == 0:
                    robj = _load_json_text(rr.stdout)
                    self.assertIn(robj.get("status"), ("REPLAYED_FAIL", "SKIPPED"))

            # cover not reached
            cpath = proj / "formal" / "schemas" / "formal_contract.json"
            c = _load(cpath)
            c["properties"].append({
                "id": "COV-UNREACH-01", "req": "REQ-006", "kind": "cover", "role": "cover",
                "expr": "q == 4'd15 && q == 4'd0", "title": "unreachable cover", "severity": "info",
                "evidence": {"section": "5.3", "quote": "A cycle with i_inc 1 and q 0 shall be reachable after reset."}
            })
            cpath.write_text(json.dumps(c, indent=2), encoding="utf-8")
            r = _run("run", "--design", str(design), "--backend", "symbiyosys", "--jobs", "1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            cov = next(p for p in res["properties"] if p["id"] == "COV-UNREACH-01")
            self.assertEqual(cov["status"], "NOT_REACHED_WITHIN_BOUND")

    def test_vacuous_assume(self):
        with tempfile.TemporaryDirectory() as td:
            proj = _copy_counter(Path(td))
            design = proj / "formal" / "design.json"
            cpath = proj / "formal" / "schemas" / "formal_contract.json"
            c = _load(cpath)
            c["properties"].append({
                "id": "ASM-VAC-01", "req": "REQ-001", "kind": "always", "role": "assume",
                "expr": "1'b1", "when": "i_inc && !i_inc", "title": "vacuous assume",
                "severity": "warning",
                "evidence": {"section": "5.1", "quote": "While i_rst_n is low, q shall be 4'b0000."}
            })
            c["requirements"][0]["properties"] = list(c["requirements"][0].get("properties") or []) + ["ASM-VAC-01"]
            cpath.write_text(json.dumps(c, indent=2), encoding="utf-8")
            r = _run("run", "--design", str(design), "--backend", "symbiyosys", "--jobs", "1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            asm = next(p for p in res["properties"] if p["id"] == "ASM-VAC-01")
            self.assertIn(asm["status"], ("VACUOUS", "ASSUMED_UNCHECKED"))


DUAL_CLOCK = SKILL / "examples" / "dual_clock"


@unittest.skipUnless(_has("yosys"), "yosys required")
class LiveDualClockQualification(unittest.TestCase):
    def _copy(self, dst: Path) -> Path:
        shutil.copytree(DUAL_CLOCK, dst / "dual_clock")
        return dst / "dual_clock"

    def test_yosys_multiclock_domains_and_waveforms(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._copy(Path(td))
            design = proj / "formal" / "design.json"
            r = _run("run", "--design", str(design), "--backend", "yosys", "--jobs", "1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            by = {p["id"]: p for p in res["properties"]}
            self.assertEqual(by["PRP-REQ-001-01"]["status"], "BMC_PASS")
            self.assertIn(by["PRP-REQ-002-01"]["status"], ("PROVEN", "BMC_PASS"))
            self.assertEqual(by["PRP-REQ-003-01"]["status"], "BMC_PASS")
            self.assertIn(by["PRP-REQ-004-01"]["status"], ("PROVEN", "BMC_PASS"))
            self.assertEqual(by["COV-REQ-005-01"]["status"], "COVERED")
            self.assertEqual((by["PRP-REQ-003-01"].get("config") or {}).get("reset_cycles"), 3)
            self.assertEqual((by["PRP-REQ-003-01"].get("config") or {}).get("clock"), "clk_b")
            r = _run("waveforms", "--design", str(design))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for pid in by:
                self.assertTrue((proj / "formal" / "reports" / "waveforms" / f"{pid}.vcd").is_file(), pid)
                self.assertTrue((proj / "formal" / "reports" / "waveforms" / f"{pid}.svg").is_file(), pid)

    @unittest.skipUnless(_has("sby"), "sby required")
    def test_sby_multiclock_domains(self):
        with tempfile.TemporaryDirectory() as td:
            proj = self._copy(Path(td))
            design = proj / "formal" / "design.json"
            r = _run("run", "--design", str(design), "--backend", "symbiyosys", "--jobs", "1")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = _load(proj / "formal" / "results" / "formal_results.json")
            statuses = {p["id"]: p["status"] for p in res["properties"]}
            self.assertEqual(statuses["COV-REQ-005-01"], "COVERED")
            self.assertIn(statuses["PRP-REQ-004-01"], ("PROVEN", "BMC_PASS"))
