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

    logic [2:0]                  tokens     [NUM_REQ-1:0];
    logic [$clog2(NUM_REQ)-1:0]  ptr;
    logic [3:0]                  starve_ctr [NUM_REQ-1:0];

    logic                        any_req;
    logic [NUM_REQ-1:0]          starved;
    logic [NUM_REQ-1:0]          eligible;
    logic [NUM_REQ-1:0]          starve_eligible;
    logic                        do_grant;
    logic [$clog2(NUM_REQ)-1:0]  next_ptr;

    always_comb begin
        any_req = |req;
        for (int i = 0; i < NUM_REQ; i++) begin
            starved[i]         = (starve_ctr[i] >= 4'(STARVE_T));
            eligible[i]        = req[i] & (tokens[i] > 3'b0) & ~starved[i];
            starve_eligible[i] = req[i] & starved[i];
        end
        do_grant = any_req & ~stall;
    end

    always_comb begin
        next_ptr = ptr;
        // Starvation override: lowest starved index wins
        for (int i = NUM_REQ-1; i >= 0; i--) begin
            if (starve_eligible[i])
                next_ptr = $clog2(NUM_REQ)'(i);
        end
        if (!(|starve_eligible)) begin
            // Normal WRR: scan forward from ptr+1
            for (int i = NUM_REQ-1; i >= 0; i--) begin
                if (eligible[$clog2(NUM_REQ)'((int'(ptr) + 1 + i) % NUM_REQ)])
                    next_ptr = $clog2(NUM_REQ)'((int'(ptr) + 1 + i) % NUM_REQ);
            end
            // All tokens exhausted: pick any requesting channel for reload
            if (!(|eligible)) begin
                for (int i = NUM_REQ-1; i >= 0; i--) begin
                    if (req[i]) next_ptr = $clog2(NUM_REQ)'(i);
                end
            end
        end
    end

    always_comb begin
        grant       = '0;
        grant_valid = 1'b0;
        if (do_grant && any_req) begin
            grant[next_ptr] = 1'b1;
            grant_valid     = 1'b1;
        end
    end

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ptr <= '0;
            for (int i = 0; i < NUM_REQ; i++) begin
                tokens[i]     <= weight[i];
                starve_ctr[i] <= '0;
            end
        end else if (!stall) begin
            if (do_grant && any_req) begin
                ptr <= next_ptr;

                // Token management
                if (tokens[next_ptr] == 3'd1 || starve_eligible[next_ptr]) begin
                    for (int i = 0; i < NUM_REQ; i++)
                        tokens[i] <= weight[i];
                end else begin
                    tokens[next_ptr] <= tokens[next_ptr] - 3'd1;
                end

                // Starvation counters — use next_ptr (not grant) to avoid comb read race
                for (int i = 0; i < NUM_REQ; i++) begin
                    if (i == int'(next_ptr))
                        starve_ctr[i] <= '0;
                    else if (req[i] && starve_ctr[i] < 4'(STARVE_T + 1))
                        starve_ctr[i] <= starve_ctr[i] + 4'd1;
                end
            end
        end
    end

endmodule
