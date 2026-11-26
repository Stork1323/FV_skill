// COV-REQ-005-01: A synchronized acknowledge is reachable
// role=cover kind=cover req=REQ-005
module fv_cov_cov_req_005_01 (
  input logic clk_a,
  input logic rst_a_n,
  input logic clk_b,
  input logic rst_b_n,
  input logic i_req,
  input logic o_ack,
  input logic dbg_req_a,
  input logic dbg_req_b_sync,
  input logic fv_start,
  output logic fv_cover
);
  wire fv_armed = fv_start && !(!rst_b_n);
  wire fv_rst_armed = fv_start && (!rst_b_n);
  assign fv_cover = (fv_armed) && (dbg_req_b_sync && o_ack);
endmodule
