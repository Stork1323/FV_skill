# Counter — Block Specification

## 1. Overview

A small parameterized binary counter used as the end-to-end fixture of the
`formal-sva` skill. It has one clock, one asynchronous active-low reset and one
increment enable.

## 2. Key Features

| ID | Feature | Class |
|---|---|---|
| F01 | Parameterized counter with an asynchronous active-low reset | Mandatory |
| F02 | Increment enable with wrap from the maximum value to zero | Mandatory |

## 3. Parameters

| Name | Type | Default | Range | Description | Features |
|---|---|---|---|---|---|
| `WIDTH` | `int` | 4 | 1 … 8 | Width of the counter output `q`. | F01, F02 |

## 4. Interface

### Clock and reset

| Signal | Direction | Width | Clock | Reset | Description |
|---|---|---|---|---|---|
| `i_clk` | in | 1 | — | — | The single rising-edge clock. |
| `i_rst_n` | in | 1 | `i_clk` | — | Active-low asynchronous reset. |
| `i_inc` | in | 1 | `i_clk` | — | Increment enable. |
| `q` | out | `WIDTH` | `i_clk` | `i_rst_n` | Counter value. |

## 5. Functional Description

### 5.1 Reset

While `i_rst_n` is low, `q` shall be `4'b0000`. Reset assertion is asynchronous;
reset deassertion is synchronous to `i_clk` outside this block.

### 5.2 Counting

`q` shall change only on the rising edge of `i_clk`.

When `i_inc` is 1 and `q` is not 15, `q` shall increment by 1 on the next rising edge of `i_clk`.

When `i_inc` is 0, `q` shall hold its value on the next rising edge of `i_clk`.

When `i_inc` is 1 and `q` is 15, `q` shall wrap to 0 on the next rising edge of `i_clk`.

### 5.3 Verification scenarios

A cycle with `i_inc` 1 and `q` 0 shall be reachable after reset.

## 6. Notes and Open Items

The counter output `q` shall be `WIDTH` bits wide.
