`timescale 1ns/1ps

module wrr_arbiter #(
    parameter int NUM_REQ  = 3,
    parameter int MAX_W    = 4,
    parameter int STARVE_T = 8
)(
    input  logic                         clk,
    input  logic                         rst_n,
    input  logic [NUM_REQ-1:0]           req,
    input  logic [NUM_REQ-1:0][2:0]      weight,
    input  logic                         stall,
    output logic [NUM_REQ-1:0]           grant,
    output logic                         grant_valid
);

    logic [2:0] tokens     [0:NUM_REQ-1];
    logic [1:0] ptr;
    logic [3:0] starve_ctr [0:NUM_REQ-1];

    // Combinational: compute who to grant next
    logic [1:0] next_ptr;
    logic       any_req;
    logic       do_grant;
    logic [2:0] starve_eligible;
    logic [2:0] eligible;

    integer i;

    always_comb begin
        any_req = |req;
        do_grant = any_req & ~stall;

        for (i = 0; i < NUM_REQ; i = i + 1) begin
            starve_eligible[i] = req[i] & (starve_ctr[i] >= 4'(STARVE_T));
            eligible[i]        = req[i] & (tokens[i] > 3'b0) & ~(starve_ctr[i] >= 4'(STARVE_T));
        end

        // Default: keep current ptr
        next_ptr = ptr;

        // Priority 1: starvation override — lowest index wins
        if (|starve_eligible) begin
            if      (starve_eligible[0]) next_ptr = 2'd0;
            else if (starve_eligible[1]) next_ptr = 2'd1;
            else                         next_ptr = 2'd2;
        end else if (|eligible) begin
            // Priority 2: normal WRR — scan from ptr+1 forward
            next_ptr = ptr;
            if      (eligible[(ptr + 2'd1) % 3]) next_ptr = (ptr + 2'd1) % 3;
            else if (eligible[(ptr + 2'd2) % 3]) next_ptr = (ptr + 2'd2) % 3;
            else if (eligible[ptr])               next_ptr = ptr;
        end else begin
            // Priority 3: all tokens exhausted — pick any requester (reload will happen)
            if      (req[0]) next_ptr = 2'd0;
            else if (req[1]) next_ptr = 2'd1;
            else             next_ptr = 2'd2;
        end

        // Drive outputs
        grant       = 3'b0;
        grant_valid = 1'b0;
        if (do_grant && any_req) begin
            grant[next_ptr] = 1'b1;
            grant_valid     = 1'b1;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ptr <= 2'd0;
            for (i = 0; i < NUM_REQ; i = i + 1) begin
                tokens[i]     <= weight[i];
                starve_ctr[i] <= 4'd0;
            end
        end else if (!stall) begin
            if (do_grant && any_req) begin
                ptr <= next_ptr;

                // Token update
                if (tokens[next_ptr] == 3'd1 || (starve_ctr[next_ptr] >= 4'(STARVE_T))) begin
                    // End of round or starvation override: reload all
                    for (i = 0; i < NUM_REQ; i = i + 1)
                        tokens[i] <= weight[i];
                end else begin
                    tokens[next_ptr] <= tokens[next_ptr] - 3'd1;
                end

                // Starvation counter update — use next_ptr index directly
                for (i = 0; i < NUM_REQ; i = i + 1) begin
                    if (i == next_ptr) begin
                        starve_ctr[i] <= 4'd0;
                    end else if (req[i] && starve_ctr[i] < 4'(STARVE_T + 1)) begin
                        starve_ctr[i] <= starve_ctr[i] + 4'd1;
                    end
                end
            end
        end
    end

endmodule
