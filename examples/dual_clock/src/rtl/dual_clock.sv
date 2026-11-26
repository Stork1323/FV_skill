// Dual-clock request/acknowledge fixture.
module dual_clock (
  input  logic clk_a,
  input  logic rst_a_n,
  input  logic clk_b,
  input  logic rst_b_n,
  input  logic i_req,
  output logic o_ack,
  output logic dbg_req_a,
  output logic dbg_req_b_sync
);
  logic req_a;
  logic req_b_meta;
  logic req_b_sync;
  logic ack_b;

  // Domain A: capture the request until reset.
  always_ff @(posedge clk_a or negedge rst_a_n) begin
    if (!rst_a_n) req_a <= 1'b0;
    else if (i_req) req_a <= 1'b1;
  end

  // Domain B: two-flop synchronizer and acknowledge register.
  always_ff @(posedge clk_b or negedge rst_b_n) begin
    if (!rst_b_n) begin
      req_b_meta <= 1'b0;
      req_b_sync <= 1'b0;
      ack_b <= 1'b0;
    end else begin
      req_b_meta <= req_a;
      req_b_sync <= req_b_meta;
      if (req_b_sync) ack_b <= 1'b1;
    end
  end

  assign o_ack = ack_b;
  assign dbg_req_a = req_a;
  assign dbg_req_b_sync = req_b_sync;
endmodule
