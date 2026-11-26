`ifndef VERILATOR
module testbench;
  reg [4095:0] vcdfile;
  reg clock;
`else
module testbench(input clock, output reg genclock);
  initial genclock = 1;
`endif
  reg genclock = 1;
  reg [31:0] cycle = 0;
  reg [0:0] PI_clk_a;
  reg [0:0] PI_clk_b;
  reg [0:0] PI_rst_a_n;
  reg [0:0] PI_i_req;
  fv_top_cov_req_005_01_bmc UUT (
    .clk_a(PI_clk_a),
    .clk_b(PI_clk_b),
    .rst_a_n(PI_rst_a_n),
    .i_req(PI_i_req)
  );
`ifndef VERILATOR
  initial begin
    if ($value$plusargs("vcd=%s", vcdfile)) begin
      $dumpfile(vcdfile);
      $dumpvars(0, testbench);
    end
    #5 clock = 0;
    while (genclock) begin
      #5 clock = 0;
      #5 clock = 1;
    end
  end
`endif
  initial begin
`ifndef VERILATOR
    #1;
`endif
    UUT._witness_.anyinit_auto_ff_cc_337_slice_62 = 1'b0;
    UUT._witness_.anyinit_flatten_u_dut__procdff_32 = 1'b0;
    UUT._witness_.anyinit_flatten_u_dut__procdff_42 = 1'b0;
    UUT._witness_.anyinit_flatten_u_dut__procdff_47 = 1'b1;
    UUT.fv_cycle = 16'b0000000000000000;

    // state 0
    PI_clk_a = 1'b0;
    PI_clk_b = 1'b0;
    PI_rst_a_n = 1'b1;
    PI_i_req = 1'b0;
  end
  always @(posedge clock) begin
    // state 1
    if (cycle == 0) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b1;
      PI_i_req <= 1'b0;
    end

    // state 2
    if (cycle == 1) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b1;
      PI_i_req <= 1'b0;
    end

    // state 3
    if (cycle == 2) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b1;
      PI_i_req <= 1'b0;
    end

    // state 4
    if (cycle == 3) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b1;
      PI_i_req <= 1'b0;
    end

    // state 5
    if (cycle == 4) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b0;
      PI_i_req <= 1'b0;
    end

    // state 6
    if (cycle == 5) begin
      PI_clk_a <= 1'b0;
      PI_clk_b <= 1'b0;
      PI_rst_a_n <= 1'b0;
      PI_i_req <= 1'b0;
    end

    genclock <= cycle < 6;
    cycle <= cycle + 1;
  end
endmodule
