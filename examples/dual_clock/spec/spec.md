# Dual-Clock Request/Ack — Block Specification

## 1. Overview

A small dual-clock request/acknowledge block. A request is captured in clock
domain A and synchronized into clock domain B through a two-flop synchronizer.
The acknowledge output is registered in domain B.

## 2. Key Features

| ID | Feature | Class |
|---|---|---|
| F01 | Request capture in clock domain A | Mandatory |
| F02 | Request synchronization and acknowledge in clock domain B | Mandatory |
| F03 | Independent asynchronous resets for both domains | Mandatory |

## 3. Interface

### Clock and reset

| Signal | Direction | Width | Clock | Reset | Description |
|---|---|---|---|---|---|
| `clk_a` | in | 1 | — | — | Domain A rising-edge clock. |
| `rst_a_n` | in | 1 | `clk_a` | — | Active-low asynchronous reset for domain A. |
| `clk_b` | in | 1 | — | — | Domain B rising-edge clock. |
| `rst_b_n` | in | 1 | `clk_b` | — | Active-low asynchronous reset for domain B. |
| `i_req` | in | 1 | `clk_a` | `rst_a_n` | Request input sampled in domain A. |
| `o_ack` | out | 1 | `clk_b` | `rst_b_n` | Acknowledge registered in domain B. |
| `dbg_req_a` | out | 1 | `clk_a` | `rst_a_n` | Observation-only copy of the domain A request state. |
| `dbg_req_b_sync` | out | 1 | `clk_b` | `rst_b_n` | Observation-only copy of the synchronized request in domain B. |

## 4. Functional Description

### 4.1 Domain A request capture

While `rst_a_n` is low, `dbg_req_a` shall be 0.

When `i_req` is 1 and `dbg_req_a` is 0, `dbg_req_a` shall be 1 on the next rising edge of `clk_a`.

### 4.2 Domain B synchronization and acknowledge

While `rst_b_n` is low, `o_ack` shall be 0.

When the synchronized request `dbg_req_b_sync` is 1 and `o_ack` is 0, `o_ack` shall be 1 on the next rising edge of `clk_b`.

### 4.3 Verification scenarios

A cycle with `dbg_req_b_sync` 1 and `o_ack` 1 shall be reachable after reset in domain B.

## 5. Notes and Open Items

The synchronizer is a two-flop synchronizer. Metastability and MTBF are out of
scope for this formal contract; only the logical transition behavior is checked.
