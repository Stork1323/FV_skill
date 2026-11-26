// Wrapper for COV-REQ-005-01: A synchronized acknowledge is reachable
module fv_top_cov_req_005_01 (
  input logic clk_a,
  input logic rst_a_n,
  input logic clk_b,
  input logic rst_b_n,
  input logic i_req,
  input logic fv_start,
  output logic fv_cover
);
  (* keep *) logic o_ack;
  (* keep *) logic dbg_req_a;
  (* keep *) logic dbg_req_b_sync;
  (* keep *) dual_clock u_dut (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync));
  (* keep *) fv_cov_cov_req_005_01 u_mon (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync), .fv_start(fv_start), .fv_cover(fv_cover));
endmodule
