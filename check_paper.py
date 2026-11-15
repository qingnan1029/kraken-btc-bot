"""Verify a paper-only buy, restart, order lookup and sell in an isolated database."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from freqtrade.configuration import Configuration
from freqtrade.enums import ExitCheckTuple, ExitType, RunMode
from freqtrade.exceptions import TemporaryError
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Order, Trade


PAIR = "BTC/USD"


def start_bot(database):
    args = {"config": ["config.paper.json"], "dry_run": True, "db_url": f"sqlite:///{database.as_posix()}"}
    config = Configuration(args, RunMode.DRY_RUN).get_config()
    if not config["dry_run"] or config["exchange"]["name"] != "kraken":
        raise ValueError("A Kraken paper configuration is required")
    return FreqtradeBot(config)


def stop_bot(bot):
    bot.cleanup()
    engine = Trade.session.get_bind()
    Trade.session.remove()
    engine.dispose()


def check():
    with TemporaryDirectory(prefix="paper-check-", dir=Path("results")) as directory:
        database = Path(directory).resolve() / "paper.sqlite"
        bot = start_bot(database)
        try:
            ask = bot.exchange.fetch_l2_order_book(PAIR, 5)["asks"][0][0]
            with patch.object(bot.exchange, "fetch_l2_order_book", side_effect=TemporaryError("Injected orderbook outage")):
                try:
                    bot.execute_entry(PAIR, 99.20, price=ask, enter_tag="smoke_outage")
                except TemporaryError:
                    pass
                else:
                    raise AssertionError("An entry should fail when the orderbook is unavailable")
            assert Trade.get_open_trades() == []
            assert not bot.exchange._dry_run_open_orders
            assert bot.execute_entry(PAIR, 99.20, price=ask, enter_tag="smoke_buy")
            trade, = Trade.get_open_trades()
            order, = trade.orders
            assert order.order_id.startswith("dry_run_") and order.status == "closed" and order.filled > 0
            order_id = order.order_id
            trade_id = trade.id
        finally:
            stop_bot(bot)

        bot = start_bot(database)
        try:
            bot.startup()
            trade, = Trade.get_open_trades()
            assert trade.id == trade_id and len(trade.orders) == 1
            restored = bot.exchange.fetch_dry_run_order(order_id)
            assert restored["status"] == "closed" and restored["filled"] > 0
            assert Order.order_by_id(order_id).ft_trade_id == trade_id
            bid = bot.exchange.fetch_l2_order_book(PAIR, 5)["bids"][0][0]
            assert bot.execute_trade_exit(trade, bid, ExitCheckTuple(ExitType.EXIT_SIGNAL), exit_tag="smoke_sell")
            assert Trade.get_open_trades() == []
            assert len(trade.orders) == 2 and trade.orders[-1].status == "closed"
        finally:
            stop_bot(bot)

    return {"mode": "dry_run", "market": PAIR, "isolated_database": True, "public_orderbook": True,
            "injected_orderbook_outage_blocked_entry": True, "simulated_buy_filled": True,
            "filled_order_restored_by_id_after_restart": True, "simulated_sell_filled": True}


if __name__ == "__main__":
    Path("results").mkdir(exist_ok=True)
    report = json.dumps(check(), indent=2) + "\n"
    (Path("results") / "paper_check.json").write_text(report, encoding="utf-8")
    print(report, end="")
