// PRP-REQ-004-01: Domain B acknowledges a synchronized request
// kind=next req=REQ-004
module fv_mon_prp_req_004_01 (
  input logic clk_a,
  input logic rst_a_n,
  input logic clk_b,
  input logic rst_b_n,
  input logic i_req,
  input logic o_ack,
  input logic dbg_req_a,
  input logic dbg_req_b_sync,
  input logic fv_start
);
  wire fv_armed = fv_start && !(!rst_b_n);
  wire fv_rst_armed = fv_start && (!rst_b_n);
  logic fv_trig_d1;
  initial fv_trig_d1 = 1'b0;
  always_ff @(posedge clk_b) begin
    fv_trig_d1 <= (fv_armed) && ((dbg_req_b_sync && !o_ack));
  end
  always @* begin
    if (fv_armed && fv_trig_d1) assert(o_ack == 1'b1);
  end
endmodule
