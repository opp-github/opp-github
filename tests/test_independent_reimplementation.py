"""Cross-check run.py against the audit's independent second implementation.

audit/independent_reimplementation.py was written from the methodology PDF
without importing run.py. Agreement to floating-point precision on the frozen
snapshot is evidence that both implement the document's formulas and timing.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "audit"))
import run as timex
import independent_reimplementation as second


@pytest.fixture(scope="module")
def both():
    cfg = timex.Config()
    market, _ = timex.load_inputs(timex.FROZEN_DATA_DIR, second.END, cfg)
    events = timex.load_fomc(timex.FOMC_CALENDAR_PATH, second.END)
    feat = timex.controlled_weights(market, timex.generate_signals(market, events, cfg), cfg)
    m, f = market.loc[second.ANCHOR:], feat.loc[second.ANCHOR:]
    ledger = timex.accounting(m.asset_er_return, f.weight_close, m.dcf, cfg)
    policy, independent_ledger = second.reimplement()
    return market, feat, ledger, policy, independent_ledger


def test_signals_allocation_volatility_and_weights_agree(both):
    market, feat, _, policy, _ = both
    for column in ("tom_mr", "tom_long", "opex", "fomc"):
        pd.testing.assert_series_equal(policy[column], feat[column], check_names=False, check_index_type=False,
                                       check_freq=False, atol=0, rtol=0)
    for column in ("short_mr", "raw_allocation_target", "allocation", "ewma_short", "ewma_long",
                   "lookthrough_target", "weight_close"):
        assert (policy[column] - feat[column]).abs().max() < 1e-10, column
    assert ((policy.asset_er - market.asset_er) / market.asset_er).abs().max() < 1e-12


def test_quantity_ledger_and_costs_agree(both):
    _, _, ledger, _, independent = both
    assert (independent.net - ledger.net_level).abs().max() < 1e-9
    assert (independent.notional - ledger.quantity * ledger.asset_level).abs().max() < 1e-9
    for mine, theirs in (("rc", "transaction_cost"), ("sc", "servicing_cost"), ("dc", "index_deduction")):
        assert (independent[mine] - ledger[theirs]).abs().max() < 1e-12, mine
