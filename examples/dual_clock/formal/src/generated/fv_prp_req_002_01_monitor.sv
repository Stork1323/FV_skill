// PRP-REQ-002-01: Domain A captures a request on the next clock edge
// kind=next req=REQ-002
module fv_mon_prp_req_002_01 (
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
  wire fv_armed = fv_start && !(!rst_a_n);
  wire fv_rst_armed = fv_start && (!rst_a_n);
  logic fv_trig_d1;
  initial fv_trig_d1 = 1'b0;
  always_ff @(posedge clk_a) begin
    fv_trig_d1 <= (fv_armed) && ((i_req && !dbg_req_a));
  end
  always @* begin
    if (fv_armed && fv_trig_d1) assert(dbg_req_a == 1'b1);
  end
endmodule
