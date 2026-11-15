"""Backtest BTC/USD crosses with explicit fees, next-open fills and slippage."""

import argparse
import csv
from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from market_data import STEP, read_candles, utc


FAST = 20
SLOW = 60
CAPITAL = 1000.0
STAKE = 100.0
FEE = 0.008
SLIPPAGES = (0.0005, 0.001)
ACCEPTABLE_DRAWDOWN = 0.05


@dataclass(frozen=True)
class Fill:
    timestamp_utc: str
    cause: str
    side: str
    price: float
    btc: float
    fee_usd: float
    cash_usd: float


def signals(candles):
    fast = deque(maxlen=FAST)
    slow = deque(maxlen=SLOW)
    previous = None
    for candle in candles:
        fast.append(candle.close)
        slow.append(candle.close)
        signal = 0
        if len(slow) == SLOW:
            difference = sum(fast) / FAST - sum(slow) / SLOW
            if previous is not None and candle.volume > 0:
                if previous <= 0 < difference:
                    signal = 1
                elif previous >= 0 > difference:
                    signal = -1
            previous = difference
        yield signal


def drawdown(equity):
    peak = CAPITAL
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak)
    return worst


def simulate(candles, indications, start, end, slippage, *, buy_and_hold=False):
    cash = CAPITAL
    position = 0.0
    fills = []
    equity = []
    pending_exit = False
    baseline_entered = False
    for index in range(start, end):
        candle = candles[index]
        # The previous candle has just closed when this candle opens.
        action = int(not baseline_entered) if buy_and_hold else indications[index - 1]
        pending_exit = bool(position and (pending_exit or action == -1))
        if candle.volume > 0 and action == 1 and cash >= STAKE and not position:
            price = candle.open * (1 + slippage)
            notional = STAKE / (1 + FEE)
            position = notional / price
            cash -= STAKE
            baseline_entered = buy_and_hold
            fills.append(Fill(utc(candle.timestamp), "buy_and_hold" if buy_and_hold else "cross_up",
                              "buy", price, position, notional * FEE, cash))
        elif candle.volume > 0 and pending_exit:
            price = candle.open * (1 - slippage)
            proceeds = position * price
            fee = proceeds * FEE
            cash += proceeds - fee
            fills.append(Fill(utc(candle.timestamp), "cross_down", "sell", price, position, fee, cash))
            position = 0.0
            pending_exit = False
        equity.append(cash + position * candle.close * (1 - slippage) * (1 - FEE))
    return {
        "end_equity_usd": equity[-1],
        "net_return_pct": 100 * (equity[-1] / CAPITAL - 1),
        "max_drawdown_pct": 100 * drawdown(equity),
        "buy_count": sum(fill.side == "buy" for fill in fills),
        "closed_round_trips": sum(fill.side == "sell" for fill in fills),
        "fees_paid_usd": sum(fill.fee_usd for fill in fills),
        "open_position_btc": position,
    }, fills


def run(source, destination):
    candles = read_candles(source)
    if len(candles) < 2 * 365 * 24 * 60 * 60 // STEP:
        raise ValueError("At least two years of continuous candles are required")
    indications = list(signals(candles))
    first = SLOW + 1
    cut = first + (len(candles) - first) * 7 // 10
    windows = {"full": (first, len(candles)), "development": (first, cut), "holdout": (cut, len(candles))}
    report = {
        "pair": "BTC/USD", "interval": "4h", "sma_periods": [FAST, SLOW],
        "capital_usd": CAPITAL, "stake_fee_inclusive_usd": STAKE, "fee_per_side": FEE,
        "drawdown_limit_pct": 100 * ACCEPTABLE_DRAWDOWN, "candle_count": len(candles), "windows": {},
    }
    records = []
    for name, (start, end) in windows.items():
        window = {
            "start_utc": utc(candles[start].timestamp), "end_exclusive_utc": utc(candles[end - 1].timestamp + STEP),
            "bars": end - start, "scenarios": [],
        }
        for slippage in SLIPPAGES:
            result, fills = simulate(candles, indications, start, end, slippage)
            baseline, _ = simulate(candles, indications, start, end, slippage, buy_and_hold=True)
            window["scenarios"].append({"slippage_per_side": slippage, "strategy": result, "buy_and_hold": baseline})
            records.extend({"window": name, "slippage_per_side": slippage, **asdict(fill)} for fill in fills)
        report["windows"][name] = window
    holdout = [item["strategy"] for item in report["windows"]["holdout"]["scenarios"]]
    report["holdout_gate_passed"] = all(stats["net_return_pct"] > 0
                                        and stats["max_drawdown_pct"] <= 100 * ACCEPTABLE_DRAWDOWN
                                        for stats in holdout)
    destination.mkdir(parents=True, exist_ok=True)
    summary = json.dumps(report, indent=2) + "\n"
    (destination / "backtest.json").write_text(summary, encoding="utf-8")
    with (destination / "trades.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["window", "slippage_per_side", *Fill.__dataclass_fields__])
        writer.writeheader()
        writer.writerows(records)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candles", type=Path, default=Path("data/BTC_USD-4h.csv"))
    parser.add_argument("--destination", type=Path, default=Path("results"))
    args = parser.parse_args()
    print(run(args.candles, args.destination), end="")
