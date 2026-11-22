// formal-sva end-to-end fixture: a parameterized wrapping counter.
module counter #(
  parameter int WIDTH = 4
) (
  input  logic             i_clk,
  input  logic             i_rst_n,
  input  logic             i_inc,
  output logic [WIDTH-1:0] q
);
  always_ff @(posedge i_clk or negedge i_rst_n) begin
    if (!i_rst_n) q <= '0;
    else if (i_inc) q <= q + 1'b1;
  end
endmodule
