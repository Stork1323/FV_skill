// SymbiYosys wrapper for COV-REQ-005-01: A synchronized acknowledge is reachable
module fv_top_cov_req_005_01_bmc (
  input logic clk_a,
  input logic rst_a_n,
  input logic clk_b,
  input logic i_req,
  output logic o_ack,
  output logic dbg_req_a,
  output logic dbg_req_b_sync
);
  logic [15:0] fv_cycle;
  initial fv_cycle = 16'd0;
  always_ff @(posedge clk_b) begin
    if (fv_cycle != 16'hffff) fv_cycle <= fv_cycle + 16'd1;
  end
  wire rst_b_n = (fv_cycle < 16'd3) ? 1'b0 : 1'b1;
  wire fv_start = (fv_cycle != 16'd0);
  (* keep *) dual_clock  u_dut (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync));
  logic fv_cover;
  (* keep *) fv_cov_cov_req_005_01 u_mon (.clk_a(clk_a), .rst_a_n(rst_a_n), .clk_b(clk_b), .rst_b_n(rst_b_n), .i_req(i_req), .o_ack(o_ack), .dbg_req_a(dbg_req_a), .dbg_req_b_sync(dbg_req_b_sync), .fv_start(fv_start), .fv_cover(fv_cover));
  always @* begin
    cover(fv_cover);
  end
endmodule
