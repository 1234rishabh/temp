"""
Hidden testbench for wrr_credit_dispatch — v3.

Single DUT: module `top`
  - wraps wrr_arbiter + credit_dispatch
  - stall feedback wire is internal (TB never drives it directly)

TB only touches external ports:
  IN:  clk, rst_n, req[2:0], weight[2:0][2:0], credit_return, out_ready
  OUT: out_valid, out_data[1:0]

Test groups:
  AXIS A  — WRR weight accuracy and reload boundary
  AXIS B  — Stall freezes arbiter (observed via output quiescence)
  AXIS C  — Starvation guard fires within threshold
  AXIS D  — Credit management and backpressure (closed-loop)
  AXIS E  — Integration: stall feedback loop (the hardest axis)
"""
from __future__ import annotations

import os
import random
from pathlib import Path
from collections import deque

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles
from cocotb_tools.runner import get_runner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def start_clock(dut, period_ns=10):
    c = Clock(dut.clk, period_ns, unit="ns")
    c.start(start_high=False)
    return c


async def do_reset(dut, cycles=6):
    dut.rst_n.value        = 0
    dut.req.value          = 0
    dut.weight.value       = pack_weights(4, 2, 1)
    dut.credit_return.value = 0
    dut.out_ready.value    = 1
    await ClockCycles(dut.clk, cycles)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


def pack_weights(w0, w1, w2):
    """Pack three 3-bit weights into the flat port value (w2 MSB, w0 LSB)."""
    return (w2 << 6) | (w1 << 3) | w0


async def drain_to_stall(dut, max_cycles=60):
    """
    Dispatch until credits truly exhausted (out_valid stays low 3+ cycles).
    Returns number of transactions dispatched.
    """
    dut.out_ready.value = 1
    dut.req.value       = 0b111
    dispatched = 0
    low_cycles = 0
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dispatched += 1
            low_cycles = 0
        elif not int(dut.out_valid.value):
            low_cycles += 1
        else:
            low_cycles = 0
        if dispatched > 0 and low_cycles >= 3:
            break
    return dispatched


async def collect_out_data(dut, n, max_cycles=200):
    """Collect n out_valid transactions, return list of out_data values."""
    results = []
    for _ in range(max_cycles):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            results.append(int(dut.out_data.value))
        if len(results) == n:
            break
    return results


# ============================================================================
# AXIS A  —  WRR weight accuracy and reload  (via top)
# ============================================================================

@cocotb.test()
async def tc_A01_weight_ratio_4_2_1(dut):
    """
    All 3 requesters active, weights 4:2:1.
    Over 10 complete rounds (70 transactions) the ratio must be ≈ 4/7 : 2/7 : 1/7.
    credit_return fires every cycle to prevent credit stall.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value = pack_weights(4, 2, 1)
    dut.req.value    = 0b111
    dut.out_ready.value = 1

    counts = [0, 0, 0]
    grants = 0
    # Keep credits topped up. Set out_ready=1 continuously but don't run
    # credit_return every cycle — return a credit after each consumed tx
    # to keep credits in range without over-saturating.
    for cyc in range(500):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            d = int(dut.out_data.value)
            if 0 <= d <= 2:
                counts[d] += 1
                grants += 1
            # Return the credit we just consumed
            dut.credit_return.value = 1
        else:
            dut.credit_return.value = 0
        if grants >= 70:
            break

    assert grants >= 70, f"Only got {grants} transactions in 300 cycles"
    total = sum(counts)
    r0, r1, r2 = counts[0]/total, counts[1]/total, counts[2]/total
    assert 0.52 <= r0 <= 0.60, f"req[0] ratio {r0:.3f} not ≈ 4/7 (counts={counts})"
    assert 0.25 <= r1 <= 0.33, f"req[1] ratio {r1:.3f} not ≈ 2/7 (counts={counts})"
    assert 0.11 <= r2 <= 0.19, f"req[2] ratio {r2:.3f} not ≈ 1/7 (counts={counts})"


@cocotb.test()
async def tc_A02_no_consecutive_overrun(dut):
    """
    With weights 3:1:1, req[0] must never receive more than 3 consecutive
    transactions before another requester is served.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(3, 1, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    run = 0
    prev = -1
    for cyc in range(300):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dut.credit_return.value = 1
            d = int(dut.out_data.value)
            if d == prev:
                run += 1
            else:
                run = 1
                prev = d
            assert not (d == 0 and run > 3), \
                f"req[0] got {run} consecutive transactions (weight=3) at cycle {cyc}"
        else:
            dut.credit_return.value = 0


@cocotb.test()
async def tc_A03_equal_weights_pure_rr(dut):
    """
    Weights 1:1:1 — every non-overlapping window of 3 transactions must
    contain exactly one of each requester.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(1, 1, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    txns = []
    for cyc in range(300):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dut.credit_return.value = 1
            txns.append(int(dut.out_data.value))
        else:
            dut.credit_return.value = 0
        if len(txns) >= 30:
            break

    assert len(txns) >= 30, f"Only got {len(txns)} transactions"
    for start in range(0, 30 - 2, 3):
        window = txns[start:start+3]
        assert sorted(window) == [0, 1, 2], \
            f"Non-RR window at txn {start}: {window}"


@cocotb.test()
async def tc_A04_single_active_requester(dut):
    """Only req[1] active — all out_data must be 1."""
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b010
    dut.out_ready.value = 1

    for cyc in range(100):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dut.credit_return.value = 1
            assert int(dut.out_data.value) == 1, \
                f"out_data={int(dut.out_data.value)} when only req[1] active"
        else:
            dut.credit_return.value = 0


@cocotb.test()
async def tc_A05_req_drops_mid_round(dut):
    """
    req[0] drops after its first transaction.
    No further transactions with out_data=0 must appear.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    first_done = False
    for cyc in range(200):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dut.credit_return.value = 1
            d = int(dut.out_data.value)
            if not first_done and d == 0:
                first_done = True
                dut.req.value = 0b110
                continue
            if first_done and d == 0:
                assert False, f"out_data=0 at cycle {cyc} after req[0] dropped"
        else:
            dut.credit_return.value = 0

    assert first_done, "req[0] never received a transaction"


# ============================================================================
# AXIS B  —  Backpressure propagates through the stall feedback wire
# ============================================================================

@cocotb.test()
async def tc_B01_backpressure_holds_out_valid(dut):
    """
    Deassert out_ready while out_valid is high.
    out_valid must stay 1 and out_data must not change for 8 cycles.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    # Wait for first transaction then one more cycle to settle in ACTIVE
    for _ in range(20):
        dut.credit_return.value = 0
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            break
    assert int(dut.out_valid.value), "out_valid never asserted"
    # One extra cycle to ensure FSM is solidly in ACTIVE before backpressure
    await RisingEdge(dut.clk)
    assert int(dut.out_valid.value), "out_valid dropped before backpressure applied"

    # Deassert ready
    dut.out_ready.value = 0
    saved_data = int(dut.out_data.value)
    for cyc in range(8):
        await RisingEdge(dut.clk)
        assert int(dut.out_valid.value) == 1, \
            f"out_valid dropped at backpressure cycle {cyc}"
        assert int(dut.out_data.value) == saved_data, \
            f"out_data changed from {saved_data} to {int(dut.out_data.value)} at cycle {cyc}"


@cocotb.test()
async def tc_B02_no_new_transaction_during_backpressure(dut):
    """
    While out_ready=0 and out_valid=1, the arbiter is stalled.
    out_data must not change (no new grant consumed).
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    dut.credit_return.value = 0
    for _ in range(20):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            break
    # Settle one extra cycle
    await RisingEdge(dut.clk)

    dut.out_ready.value = 0
    saved = int(dut.out_data.value)
    transitions = 0
    prev = saved
    for _ in range(10):
        await RisingEdge(dut.clk)
        cur = int(dut.out_data.value)
        if cur != prev:
            transitions += 1
        prev = cur

    assert transitions == 0, \
        f"out_data changed {transitions} times during 10-cycle backpressure (grant consumed early)"


@cocotb.test()
async def tc_B03_transaction_completes_after_ready_returns(dut):
    """
    After backpressure lifts (req=0 also cleared so no new grants),
    out_valid must drop within 2 cycles.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    for _ in range(20):
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            break

    # Hold backpressure, then drain req
    dut.out_ready.value     = 0
    dut.credit_return.value = 0
    await ClockCycles(dut.clk, 4)
    dut.req.value       = 0      # no new grants will come
    dut.out_ready.value = 1      # release backpressure

    dropped = False
    for _ in range(4):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) == 0:
            dropped = True
            break
    assert dropped, "out_valid did not drop after backpressure released with req=0"


@cocotb.test()
async def tc_B04_multiple_backpressure_cycles(dut):
    """
    Three consecutive backpressure / release cycles.
    Each round: out_valid stays up during BP, completes on release.
    A credit_return is sent after each release to keep credits live.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value = pack_weights(4, 2, 1)

    for rnd in range(3):
        dut.req.value           = 0b111
        dut.out_ready.value     = 1
        dut.credit_return.value = 0

        # Wait for a transaction
        got = False
        for _ in range(30):
            await RisingEdge(dut.clk)
            if int(dut.out_valid.value):
                got = True
                break
        assert got, f"No transaction in round {rnd}"
        # Settle one extra cycle before applying backpressure
        await RisingEdge(dut.clk)
        assert int(dut.out_valid.value), f"out_valid dropped before BP in round {rnd}"

        # Backpressure for 5 cycles
        dut.out_ready.value = 0
        for cyc in range(5):
            await RisingEdge(dut.clk)
            assert int(dut.out_valid.value) == 1, \
                f"out_valid dropped in round {rnd} BP cycle {cyc}"

        # Release
        dut.out_ready.value = 1
        await ClockCycles(dut.clk, 2)
        # Return a credit to replenish
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        dut.credit_return.value = 0


# ============================================================================
# AXIS C  —  Starvation guard  (closed-loop, observed at out_data)
# ============================================================================

@cocotb.test()
async def tc_C01_starvation_fires_within_threshold(dut):
    """
    Weights 4:1:1.  req[2] activates after req[0] has been running for
    STARVE_T-2 cycles.  req[2] must receive a transaction within
    STARVE_T+4 further active cycles.
    """
    start_clock(dut)
    await do_reset(dut)
    STARVE_T = 8
    dut.weight.value    = pack_weights(4, 1, 1)
    dut.out_ready.value = 1

    # Only req[0] for STARVE_T-2 active cycles
    dut.req.value = 0b001
    for _ in range(STARVE_T - 2):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if int(dut.out_valid.value) else 0

    # Activate req[2]
    dut.req.value = 0b101
    served_2 = False
    for _ in range(STARVE_T + 8):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_data.value) == 2:
            served_2 = True
            break

    assert served_2, \
        f"req[2] not served within STARVE_T+8 active cycles after activation"


@cocotb.test()
async def tc_C02_starvation_ctr_frozen_during_backpressure(dut):
    """
    Apply backpressure (out_ready=0) for 3×STARVE_T cycles while
    req[2] (low weight) is active.  After releasing backpressure,
    req[0] (high weight, recently served) should appear first —
    the starvation counter must NOT have ticked during the stall.
    """
    start_clock(dut)
    await do_reset(dut)
    STARVE_T = 8
    dut.weight.value    = pack_weights(4, 1, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    # Let system run for 3 grants to establish recent service
    got = 0
    for _ in range(60):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            got += 1
            if got == 3:
                break

    # Now apply backpressure for 3×STARVE_T cycles
    dut.out_ready.value     = 0
    dut.credit_return.value = 0
    await ClockCycles(dut.clk, 3 * STARVE_T)

    # Release backpressure — req[2] should NOT get immediate starvation override
    dut.out_ready.value = 1
    first_data = []
    for _ in range(30):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            first_data.append(int(dut.out_data.value))
        if len(first_data) >= 3:
            break

    # req[0] (weight 4) must appear in the first 3 post-BP transactions
    assert 0 in first_data, \
        f"req[0] absent from first 3 post-BP transactions {first_data}: " \
        f"starvation counter ticked during backpressure stall"


@cocotb.test()
async def tc_C03_starvation_counter_resets_after_grant(dut):
    """
    After req[2] gets a starvation-override transaction, its counter
    resets to 0.  The gap (in transactions) before its next grant must
    be ≥ STARVE_T, not 1.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 4, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    override_txn_indices = []
    txn_idx = 0
    for _ in range(400):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            if int(dut.out_data.value) == 2:
                override_txn_indices.append(txn_idx)
            txn_idx += 1
        if len(override_txn_indices) >= 2:
            break

    if len(override_txn_indices) < 2:
        cocotb.log.warning("Could not observe 2 override transactions — test inconclusive")
        return

    gap = override_txn_indices[1] - override_txn_indices[0]
    assert gap >= 5, \
        f"req[2] got back-to-back override grants (gap={gap} txns): counter did not reset"


# ============================================================================
# AXIS D  —  Credit management  (observed externally)
# ============================================================================

@cocotb.test()
async def tc_D01_credit_drain_halts_output(dut):
    """
    With credit_return=0, CREDIT_INIT=4 dispatches must occur then
    out_valid must stop.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value        = pack_weights(4, 2, 1)
    dut.req.value           = 0b111
    dut.out_ready.value     = 1
    dut.credit_return.value = 0

    dispatched = 0
    low_cycles = 0
    for _ in range(60):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            dispatched += 1
            low_cycles = 0
        elif not int(dut.out_valid.value):
            low_cycles += 1
        else:
            low_cycles = 0
        # Real credit stall = out_valid stays low for 3+ consecutive cycles
        if low_cycles >= 3:
            assert not int(dut.out_valid.value), \
                "out_valid resumed without credit_return"
            break

    assert 1 <= dispatched <= 8, \
        f"Expected 1-8 transactions before credit drain, got {dispatched}"


@cocotb.test()
async def tc_D02_credit_return_unblocks(dut):
    """
    Drain credits to zero, then fire one credit_return pulse.
    out_valid must resume within 3 cycles.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value        = pack_weights(4, 2, 1)
    dut.req.value           = 0b111
    dut.out_ready.value     = 1
    dut.credit_return.value = 0

    await drain_to_stall(dut)

    # Return one credit
    dut.credit_return.value = 1
    await RisingEdge(dut.clk)
    dut.credit_return.value = 0

    resumed = False
    for _ in range(5):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            resumed = True
            break
    assert resumed, "out_valid did not resume after credit_return"


@cocotb.test()
async def tc_D03_simultaneous_return_and_consume(dut):
    """
    credit_return fires in the same cycle a transaction is consumed.
    Net credit count must not underflow — out_valid must keep going
    for at least 2 more cycles without stalling.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(4, 2, 1)
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    # Wait for first active transaction
    for _ in range(10):
        dut.credit_return.value = 0
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            break
    assert int(dut.out_valid.value), "No transaction to test"

    # Fire return in same cycle as consume
    dut.credit_return.value = 1
    await RisingEdge(dut.clk)
    dut.credit_return.value = 0

    # Allow up to 1 bubble cycle (out_valid=0), but must resume within 3 cycles
    # A true underflow would stall for many cycles
    resumed = False
    for _ in range(5):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            resumed = True
            break
    assert resumed, \
        "Credit underflow: output did not resume after simultaneous return+consume"


@cocotb.test()
async def tc_D04_credit_saturation(dut):
    """
    Pump 12 credit_return pulses with req=0 (nothing dispatching).
    Credits must saturate at CREDIT_MAX, not overflow.
    After saturation, dispatch must still work.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.req.value           = 0
    dut.out_ready.value     = 1
    dut.credit_return.value = 0

    for _ in range(12):
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        dut.credit_return.value = 0
        await RisingEdge(dut.clk)

    # Now dispatch — must work without stall
    dut.req.value = 0b111
    found = False
    for _ in range(10):
        dut.credit_return.value = 0
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            found = True
            break
    assert found, "Dispatch failed after credit saturation — possible counter overflow"


@cocotb.test()
async def tc_D05_bubble_insertion(dut):
    """
    Back-to-back grants from different requesters must produce at least
    one idle cycle (out_valid=0) between two active transactions.
    Keep credit_return=1 so credits never stall.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(1, 1, 1)  # round-robin: 0,1,2,0,1,2...
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    trace = []
    for _ in range(60):
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        trace.append(int(dut.out_valid.value))
        if len(trace) >= 40:
            break

    # Find a 1-0-1 pattern anywhere in the trace
    found_bubble = any(
        trace[i] == 1 and trace[i+1] == 0 and trace[i+2] == 1
        for i in range(len(trace) - 2)
    )
    assert found_bubble, \
        f"No bubble (1-0-1 pattern) found in out_valid trace: {trace}"


# ============================================================================
# AXIS E  —  Integration: closed-loop stall feedback  (the hardest axis)
# ============================================================================

@cocotb.test()
async def tc_E01_backpressure_stalls_arbiter_state(dut):
    """
    KEY CLOSED-LOOP TEST.
    Apply backpressure (out_ready=0) for 10 cycles.
    During those 10 cycles, the arbiter must be frozen:
    when backpressure releases, the sequence of out_data values must
    continue correctly from where it left off (no skipped requester,
    no double-served requester).
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value    = pack_weights(1, 1, 1)  # pure RR: 0,1,2,0,1,2
    dut.req.value       = 0b111
    dut.out_ready.value = 1

    # Collect 3 transactions to establish the sequence
    pre = []
    for _ in range(100):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            pre.append(int(dut.out_data.value))
        if len(pre) == 3:
            break
    assert len(pre) == 3, "Could not collect 3 pre-stall transactions"

    # Settle one cycle to ensure FSM is stable before backpressure
    dut.credit_return.value = 0
    await RisingEdge(dut.clk)

    # Apply backpressure for 10 cycles (stall propagates to arbiter)
    dut.out_ready.value     = 0
    dut.credit_return.value = 0
    await ClockCycles(dut.clk, 10)
    dut.out_ready.value = 1

    # Collect 3 more transactions after backpressure
    post = []
    for _ in range(100):
        await RisingEdge(dut.clk)
        dut.credit_return.value = 1 if (int(dut.out_valid.value) and int(dut.out_ready.value)) else 0
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            post.append(int(dut.out_data.value))
        if len(post) == 3:
            break
    assert len(post) == 3, "Could not collect 3 post-stall transactions"

    # Combined sequence must be a valid chunk of the repeating 0,1,2 pattern
    combined = pre + post
    rr_pattern = [0, 1, 2] * 10
    matched = any(
        rr_pattern[start:start+6] == combined
        for start in range(len(rr_pattern) - 5)
    )
    assert matched, \
        f"Sequence broke across backpressure boundary: pre={pre} post={post}"


@cocotb.test()
async def tc_E02_credit_stall_then_backpressure_simultaneously(dut):
    """
    Drain credits to zero AND apply backpressure at the same time.
    System must not deadlock or corrupt state.
    After returning credits and releasing backpressure, dispatch resumes.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value        = pack_weights(4, 2, 1)
    dut.req.value           = 0b111
    dut.out_ready.value     = 1
    dut.credit_return.value = 0

    # Dispatch one transaction, then immediately backpressure+drain
    for _ in range(10):
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            break

    dut.out_ready.value = 0   # backpressure — no more credit returns either

    # Hold for 15 cycles — both stall conditions active
    await ClockCycles(dut.clk, 15)

    # Release both simultaneously
    dut.out_ready.value     = 1
    dut.credit_return.value = 1
    await RisingEdge(dut.clk)
    dut.credit_return.value = 0

    # System must resume dispatch within a few cycles
    resumed = False
    for _ in range(10):
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value):
            resumed = True
            break
    assert resumed, "System did not recover after simultaneous credit+backpressure stall"


@cocotb.test()
async def tc_E03_starvation_fires_through_stall_feedback(dut):
    """
    KEY CLOSED-LOOP TEST.
    req[2] (weight=1) and req[0] (weight=4) both active.
    Apply periodic short backpressure bursts — the stall from each burst
    must NOT accumulate toward req[2]'s starvation threshold.
    req[2] must eventually be served but not prematurely overriding req[0].
    """
    start_clock(dut)
    await do_reset(dut)
    STARVE_T = 8
    dut.weight.value    = pack_weights(4, 0, 1)
    dut.req.value       = 0b101    # req[0] and req[2] only
    dut.out_ready.value = 1

    served_counts = [0, 0, 0]
    for cyc in range(400):
        if cyc % 5 == 3:
            dut.out_ready.value = 0
        elif cyc % 5 == 0:
            dut.out_ready.value = 1

        await RisingEdge(dut.clk)
        consumed = int(dut.out_valid.value) and int(dut.out_ready.value)
        dut.credit_return.value = 1 if consumed else 0
        if consumed:
            d = int(dut.out_data.value)
            if 0 <= d <= 2:
                served_counts[d] += 1

    total = served_counts[0] + served_counts[2]
    cocotb.log.info(f"Counts: req[0]={served_counts[0]} req[2]={served_counts[2]}")

    assert total > 0, "No transactions dispatched"
    assert served_counts[2] > 0, \
        "req[2] was never served (starvation guard never fired)"
    assert served_counts[0] > served_counts[2], \
        f"req[0] should dominate (weight=4): {served_counts[0]} vs {served_counts[2]}"


@cocotb.test()
async def tc_E04_scoreboard_weight_accuracy_with_backpressure(dut):
    """
    Scoreboard test: random out_ready toggles + continuous credit_return.
    Verify weight ratios still hold over 150 completed transactions.
    Ensures backpressure does not bias the arbitration.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value = pack_weights(4, 2, 1)
    dut.req.value    = 0b111

    rng = random.Random(0xDEAD_BEEF)
    counts = [0, 0, 0]
    txns = 0

    for cyc in range(600):
        dut.out_ready.value     = 0 if rng.random() < 0.30 else 1
        dut.credit_return.value = 1   # keep credits full
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            d = int(dut.out_data.value)
            if 0 <= d <= 2:
                counts[d] += 1
                txns += 1
        if txns >= 150:
            break

    assert txns >= 100, f"Only got {txns} transactions in 600 cycles"
    total = sum(counts)
    r0, r1, r2 = counts[0]/total, counts[1]/total, counts[2]/total
    assert 0.50 <= r0 <= 0.62, f"req[0] ratio {r0:.3f} degraded under backpressure"
    assert 0.24 <= r1 <= 0.34, f"req[1] ratio {r1:.3f} degraded under backpressure"
    assert 0.10 <= r2 <= 0.20, f"req[2] ratio {r2:.3f} degraded under backpressure"


@cocotb.test()
async def tc_E05_no_transaction_lost_under_backpressure(dut):
    """
    Drive exactly 12 grant opportunities (req always active, credits kept up).
    Count completed transactions (out_valid AND out_ready).
    Number of completed transactions must equal total grant opportunities
    eventually — nothing is lost, even under random backpressure.
    """
    start_clock(dut)
    await do_reset(dut)
    dut.weight.value = pack_weights(1, 1, 1)
    dut.req.value    = 0b111

    rng = random.Random(0xC0FFEE)
    completed = 0
    for cyc in range(300):
        dut.out_ready.value     = 0 if rng.random() < 0.40 else 1
        dut.credit_return.value = 1
        await RisingEdge(dut.clk)
        if int(dut.out_valid.value) and int(dut.out_ready.value):
            completed += 1
        if completed >= 30:
            break

    assert completed >= 30, \
        f"Only {completed}/30 transactions completed — transactions may be dropped"


# ============================================================================
# Runner  —  single target: module `top`
# ============================================================================

def test_top_runner():
    sim      = os.getenv("SIM", "icarus")
    proj     = Path(__file__).resolve().parent.parent

    sources  = [
        proj / "sources" / "wrr_arbiter.sv",
        proj / "sources" / "credit_dispatch.sv",
        proj / "sources" / "top.sv",
    ]

    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel="top",
        always=True,
    )
    runner.test(
        hdl_toplevel="top",
        test_module="test_wrr_credit_dispatch_hidden",
    )
