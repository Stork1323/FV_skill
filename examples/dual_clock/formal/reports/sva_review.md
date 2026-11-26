# SVA and constraint review: dual_clock

- **Top:** `dual_clock`
- **Overall:** **WARN**
- **Findings:** 2 (0 error, 2 warning, 0 info)
- **Generated:** 2026-11-26T01:58:51+00:00

## Findings

| ID | Severity | Category | Property | Code | Message | Owner |
|---|---|---|---|---|---|---|
| `REV-001` | **warning** | coverage | `PRP-REQ-002-01` | TRIGGER_UNCOVERED | PRP-REQ-002-01 asserts a trigger but no cover reaches that trigger. | verification_engineer |
| `REV-002` | **warning** | coverage | `PRP-REQ-004-01` | TRIGGER_UNCOVERED | PRP-REQ-004-01 asserts a trigger but no cover reaches that trigger. | verification_engineer |

## Recommended actions

### `REV-001` -- TRIGGER_UNCOVERED

- **Owner:** verification_engineer
- **Message:** PRP-REQ-002-01 asserts a trigger but no cover reaches that trigger.
- **Action:** Add a cover property with the same trigger; otherwise a bounded pass may be vacuous.

### `REV-002` -- TRIGGER_UNCOVERED

- **Owner:** verification_engineer
- **Message:** PRP-REQ-004-01 asserts a trigger but no cover reaches that trigger.
- **Action:** Add a cover property with the same trigger; otherwise a bounded pass may be vacuous.

## Reproduce

```sh
python3 scripts/fv.py review --design <formal/design.json>
```
