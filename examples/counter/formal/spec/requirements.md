---
schema_version: 1.0
design: counter
top: counter
spec: spec/spec.md
spec_sha256: ff7caa42d3d91f43a18e4dbff0935d47b4cdf418fecf07cf7c6eea3ed3291177
generated_by: formal-sva/fv_extract.py
---

# Formal requirement extraction: counter

This file is the reviewable bridge between the specification and the
formal contract. Each requirement has a source, a verbatim evidence
quote, a clarity decision and a proposed property shape.

## Features

| ID | Class | Statement | Source |
|---|---|---|---|
| `F01` | Mandatory | Parameterized counter with an asynchronous active-low reset |  |
| `F02` | Mandatory | Increment enable with wrap from the maximum value to zero |  |

## Requirements

### REQ-001 -- While i_rst_n is low, q shall be 4'b0000.

- **Feature:** F01
- **Category:** reset
- **Statement:** While i_rst_n is low, q shall be 4'b0000.
- **Source:** L37
- **Evidence:** "While `i_rst_n` is low, `q` shall be `4'b0000`. Reset assertion is asynchronous;"
- **Clarity:** clear
- **Proposed property:** always
- **Owner:** spec_agent
- **Blocking:** no

### REQ-002 -- q shall change only on the rising edge of i_clk.

- **Feature:** F01
- **Category:** interface
- **Statement:** q shall change only on the rising edge of i_clk.
- **Source:** L42
- **Evidence:** "`q` shall change only on the rising edge of `i_clk`."
- **Clarity:** clear
- **Proposed property:** static
- **Owner:** spec_agent
- **Blocking:** no

### REQ-003 -- When i_inc is 1 and q is not 15, q shall increment by 1 on the next rising edge 

- **Feature:** F02
- **Category:** functional
- **Statement:** When i_inc is 1 and q is not 15, q shall increment by 1 on the next rising edge of i_clk.
- **Source:** L44
- **Evidence:** "When `i_inc` is 1 and `q` is not 15, `q` shall increment by 1 on the next rising edge of `i_clk`."
- **Clarity:** clear
- **Proposed property:** always
- **Owner:** spec_agent
- **Blocking:** no

### REQ-004 -- When i_inc is 0, q shall hold its value on the next rising edge of i_clk.

- **Feature:** F02
- **Category:** functional
- **Statement:** When i_inc is 0, q shall hold its value on the next rising edge of i_clk.
- **Source:** L46
- **Evidence:** "When `i_inc` is 0, `q` shall hold its value on the next rising edge of `i_clk`."
- **Clarity:** clear
- **Proposed property:** always
- **Owner:** spec_agent
- **Blocking:** no

### REQ-005 -- When i_inc is 1 and q is 15, q shall wrap to 0 on the next rising edge of i_clk.

- **Feature:** F02
- **Category:** functional
- **Statement:** When i_inc is 1 and q is 15, q shall wrap to 0 on the next rising edge of i_clk.
- **Source:** L48
- **Evidence:** "When `i_inc` is 1 and `q` is 15, `q` shall wrap to 0 on the next rising edge of `i_clk`."
- **Clarity:** clear
- **Proposed property:** always
- **Owner:** spec_agent
- **Blocking:** no

### REQ-006 -- A cycle with i_inc 1 and q 0 shall be reachable after reset.

- **Feature:** F02
- **Category:** verification
- **Statement:** A cycle with i_inc 1 and q 0 shall be reachable after reset.
- **Source:** L52
- **Evidence:** "A cycle with `i_inc` 1 and `q` 0 shall be reachable after reset."
- **Clarity:** clear
- **Proposed property:** cover
- **Owner:** spec_agent
- **Blocking:** no

### REQ-007 -- The counter output q shall be WIDTH bits wide.

- **Feature:** F01
- **Category:** interface
- **Statement:** The counter output q shall be WIDTH bits wide.
- **Source:** L56
- **Evidence:** "The counter output `q` shall be `WIDTH` bits wide."
- **Clarity:** clear
- **Proposed property:** static
- **Owner:** spec_agent
- **Blocking:** no

