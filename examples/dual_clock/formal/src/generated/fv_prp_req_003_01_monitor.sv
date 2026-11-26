// PRP-REQ-003-01: Domain B reset clears the acknowledge
// kind=reset_value req=REQ-003
module fv_mon_prp_req_003_01 (
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
  always @* begin
    if (fv_rst_armed) assert(o_ack == 1'b0);
  end
endmodule
