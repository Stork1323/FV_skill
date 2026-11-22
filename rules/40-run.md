# Run rules

1. The backend is selected by `formal/tools.json` and
   `formal/design.json` `formal.backend`. The tools default is SymbiYosys;
   `yosys` is the dependency-free fallback.
2. A run executes one property per backend invocation. A single property may
   run BMC and then induction; the phases are reported separately.
3. Reset is forced for `reset_cycles` cycles; `depth` must be greater than
   `reset_cycles`.
4. `BMC_PASS` is not a proof. A property is `PROVEN` only when the induction
   phase succeeds.
5. Cover properties report reachability within depth, not exhaustive
   reachability. Assumptions report satisfiability; `VACUOUS` means the guard
   was never reached.
6. Timeouts and tool errors are `TIMEOUT` / `ERROR`, never a pass.
7. A commercial backend is trusted only after its `result_pattern` has been
   verified against a known PASS and a known FAIL log. Put that evidence in
   `formal/tools.json` `notes` and in the project's validation record.
8. The counterexample, log path and backend script path are part of the result
   document.
