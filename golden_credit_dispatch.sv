`timescale 1ns/1ps

module credit_dispatch #(
    parameter int CREDIT_INIT = 4,
    parameter int CREDIT_MAX  = 8
)(
    input  logic       clk,
    input  logic       rst_n,
    input  logic       grant_valid,
    input  logic [1:0] grant_id,
    input  logic       credit_return,
    output logic       out_valid,
    output logic [1:0] out_data,
    input  logic       out_ready,
    output logic       stall
);

    // States
    localparam [1:0] IDLE   = 2'd0;
    localparam [1:0] ACTIVE = 2'd1;
    localparam [1:0] BUBBLE = 2'd2;

    reg [1:0] state;
    reg [1:0] held_data;
    reg [3:0] credits;      // 4 bits fits CREDIT_MAX=8
    reg       had_tx;       // previous cycle completed a transaction

    wire credit_empty = (credits == 4'd0);

    // Outputs
    assign out_valid = (state == ACTIVE);
    assign out_data  = (state == ACTIVE) ? held_data : 2'd0;
    assign stall     = ((state == ACTIVE) && !out_ready) || credit_empty;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state     <= IDLE;
            held_data <= 2'd0;
            credits   <= CREDIT_INIT[3:0];
            had_tx    <= 1'b0;
        end else begin
            had_tx <= 1'b0;

            case (state)

                IDLE: begin
                    // Credit return with no dispatch
                    if (credit_return && credits < CREDIT_MAX[3:0])
                        credits <= credits + 4'd1;

                    if (grant_valid && !credit_empty) begin
                        held_data <= grant_id;
                        // Spend one credit; if return also arrives, net = 0 change
                        if (credit_return)
                            credits <= credits;        // +1 -1 = net 0
                        else
                            credits <= credits - 4'd1;
                        state <= ACTIVE;
                    end
                end

                ACTIVE: begin
                    if (!out_ready) begin
                        // Backpressure: hold state, just handle credit return
                        if (credit_return && credits < CREDIT_MAX[3:0])
                            credits <= credits + 4'd1;
                        // out_valid stays 1, out_data unchanged — FSM stays ACTIVE
                    end else begin
                        // Transaction accepted by downstream
                        had_tx <= 1'b1;

                        if (had_tx && grant_valid && !credit_empty) begin
                            // Back-to-back: insert bubble
                            if (credit_return && credits < CREDIT_MAX[3:0])
                                credits <= credits + 4'd1;
                            state <= BUBBLE;
                        end else if (grant_valid && !credit_empty) begin
                            // Accept next grant immediately
                            held_data <= grant_id;
                            if (credit_return)
                                credits <= credits;
                            else
                                credits <= credits - 4'd1;
                            state <= ACTIVE;
                        end else begin
                            // No new grant — go idle
                            if (credit_return && credits < CREDIT_MAX[3:0])
                                credits <= credits + 4'd1;
                            state <= IDLE;
                        end
                    end
                end

                BUBBLE: begin
                    // One idle cycle: out_valid=0
                    if (credit_return && credits < CREDIT_MAX[3:0])
                        credits <= credits + 4'd1;

                    if (grant_valid && !credit_empty) begin
                        held_data <= grant_id;
                        if (credit_return)
                            credits <= credits;
                        else
                            credits <= credits - 4'd1;
                        state <= ACTIVE;
                    end else begin
                        state <= IDLE;
                    end
                end

                default: state <= IDLE;

            endcase
        end
    end

endmodule
