// Wrapper for PRP-REQ-004-01: Domain B acknowledges a synchronized request
module fv_top_prp_req_004_01 (
  input logic clk_a,
  input logic rst_a_n,
  input logic clk_b,
  input logic rst_b_n,
  input logic i_req,
  input logic fv_start
);
  (* keep *) logic o_ack;
  (* keep *) logic dbg_req_a;
  (* keep *) logic dbg_req_b_sync;
  (* keep *) dual_clock u_dut (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync));
  (* keep *) fv_mon_prp_req_004_01 u_mon (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync), .fv_start(fv_start));
endmodule
