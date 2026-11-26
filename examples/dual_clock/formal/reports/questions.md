# Formal clarification questions: dual_clock

- **Overall:** **CLEAR**
- **Open:** 0 (blocking: 0)
- **Answered:** 0
- **Answers file:** `formal/spec/answers.json`

No open questions.

## How to answer

Write `formal/spec/answers.json`:

```json
{
  "schema_version": "1.0",
  "answers": {
    "Q-REQ-002-01": {
      "status": "answered",
      "answer": "The counter is free-running; no pause input exists.",
      "owner": "spec_agent",
      "spec_sha256": "<sha256 from questions.json inputs.spec_sha256>",
      "answered_at": "2026-01-01T00:00:00+00:00"
    }
  }
}
```

Answers tied to an old `spec_sha256` are treated as stale when the spec changes.
