---
schema_version: 1.0
design: dual_clock
top: dual_clock
spec: spec/spec.md
spec_sha256: 05c19cf9e3f0cb921452d82bfbcc0b58f1420b6777947d671bbbdb2596b64ab4
generated_by: formal-sva/fv_extract.py
---

# Formal requirement extraction: dual_clock

This file is the reviewable bridge between the specification and the
formal contract. Each requirement has a source, a verbatim evidence
quote, a clarity decision and a proposed property shape.

## Features

| ID | Class | Statement | Source |
|---|---|---|---|
| `F01` | Mandatory | Request capture in clock domain A |  |
| `F02` | Mandatory | Request synchronization and acknowledge in clock domain B |  |
| `F03` | Mandatory | Independent asynchronous resets for both domains |  |

## Requirements

### REQ-001 -- Domain A reset clears the captured request

- **Feature:** F03
- **Category:** reset
- **Statement:** While rst_a_n is low, dbg_req_a shall be 0.
- **Source:** L36
- **Evidence:** "While `rst_a_n` is low, `dbg_req_a` shall be 0."
- **Clarity:** clear
- **Proposed property:** reset_value
- **Owner:** spec_agent
- **Blocking:** no

### REQ-002 -- Domain A captures a request on the next clock edge

- **Feature:** F01
- **Category:** functional
- **Statement:** When i_req is 1 and dbg_req_a is 0, dbg_req_a shall be 1 on the next rising edge of clk_a.
- **Source:** L38
- **Evidence:** "When `i_req` is 1 and `dbg_req_a` is 0, `dbg_req_a` shall be 1 on the next rising edge of `clk_a`."
- **Clarity:** clear
- **Proposed property:** next
- **Owner:** spec_agent
- **Blocking:** no

### REQ-003 -- Domain B reset clears the acknowledge

- **Feature:** F03
- **Category:** reset
- **Statement:** While rst_b_n is low, o_ack shall be 0.
- **Source:** L42
- **Evidence:** "While `rst_b_n` is low, `o_ack` shall be 0."
- **Clarity:** clear
- **Proposed property:** reset_value
- **Owner:** spec_agent
- **Blocking:** no

### REQ-004 -- Domain B acknowledges a synchronized request

- **Feature:** F02
- **Category:** functional
- **Statement:** When dbg_req_b_sync is 1 and o_ack is 0, o_ack shall be 1 on the next rising edge of clk_b.
- **Source:** L44
- **Evidence:** "When the synchronized request `dbg_req_b_sync` is 1 and `o_ack` is 0, `o_ack` shall be 1 on the next rising edge of `clk_b`."
- **Clarity:** clear
- **Proposed property:** next
- **Owner:** spec_agent
- **Blocking:** no

### REQ-005 -- A synchronized acknowledge is reachable

- **Feature:** F02
- **Category:** verification
- **Statement:** A cycle with dbg_req_b_sync 1 and o_ack 1 shall be reachable after reset in domain B.
- **Source:** L48
- **Evidence:** "A cycle with `dbg_req_b_sync` 1 and `o_ack` 1 shall be reachable after reset in domain B."
- **Clarity:** clear
- **Proposed property:** cover
- **Owner:** spec_agent
- **Blocking:** no
