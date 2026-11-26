# Verification plan: dual_clock

- **Top:** `dual_clock`
- **Generated:** 2026-11-26T01:58:51+00:00
- **Configuration:** `{}`

## Requirement-to-checker mapping

| Requirement | Disposition | Checkers | Dependencies | Closure |
|---|---|---|---|---|
| `REQ-001` | formalized | `PRP-REQ-001-01`(BMC_PASS) |  | open |
| `REQ-002` | formalized | `PRP-REQ-002-01`(PROVEN) |  | open |
| `REQ-003` | formalized | `PRP-REQ-003-01`(BMC_PASS) |  | open |
| `REQ-004` | formalized | `PRP-REQ-004-01`(PROVEN) |  | open |
| `REQ-005` | formalized | `COV-REQ-005-01`(COVERED) |  | open |

## Reset plan

- **cold_reset**: Reset asserted for configured reset_cycles before checks arm.

## Exclusions

- (none declared)

## Closure criteria

- All formalized requirements have discharged required assertions for the declared configuration.
- All required covers are reached or explicitly proven unreachable.
- All assumptions are satisfiable and reviewed for ownership/vacuity.
- Review has no blocking ACTION_REQUIRED findings.
- Regression baseline is updated only after review and signoff decision.
