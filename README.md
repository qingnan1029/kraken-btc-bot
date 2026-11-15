# Kraken BTC/USD 现货模拟机器人

项目包含可复现的回测、Freqtrade 双均线策略和模拟配置。所有命令从项目根目录运行。策略使用 4 小时 K 线、20/60 简单均线和一笔持仓，模拟资金为 1,000 美元。实施计划和首次验收位于 `docs/`。

## 复现回测

Python 3.11 或更高版本可直接运行回测脚本，无需安装库。首次克隆后先运行 `python market_data.py` 下载行情。已有 `data/BTC_USD-4h.csv` 的本地副本或发布包可离线运行。输出到 `results/backtest.json` 和 `results/trades.csv`。

```powershell
python backtest.py
```

需要更新公开数据时运行 `python market_data.py`。脚本从 Kraken 官方归档按字节提取 BTC/USD 条目，校验 ZIP 条目 CRC，并通过官方 REST 接口补齐。数据清单记录间隔、来源、空档核查和 CSV 的 SHA-256。Kraken REST 仅提供最近约 720 根 K 线。若当前归档和 REST 已没有重叠，脚本会报错。届时需要先获取更新的官方归档。

## 启动实时模拟盘

Windows 用 Python 3.12 建立虚拟环境。以下命令全程使用模拟模式和公开行情。`Ctrl+C` 停止进程，重新运行可继续使用同一 SQLite 数据库。运行前确认同一数据库只由一个机器人占用。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install freqtrade==2026.10
.\.venv\Scripts\python.exe -m freqtrade trade --dry-run -c config.paper.json --db-url sqlite:///user_data/paper.sqlite --logfile user_data/paper.log
```

已有 Docker Compose 可运行 `docker compose up -d`。用 `docker compose logs -f` 看状态，用 `docker compose down` 停止。也可直接查看 `user_data/paper.log`。本次验收使用 Windows 原生 Python，确认了 Docker 镜像标签可用。

```powershell
Get-Content user_data/paper.log -Tail 25
.\.venv\Scripts\python.exe -m freqtrade show-trades --db-url sqlite:///user_data/paper.sqlite
```

## 验证与限制

- `python check_paper.py` 需要安装 Freqtrade 和访问 Kraken 公共订单簿。它在临时数据库中演练订单簿请求失败、模拟买入、重启恢复订单 ID 和模拟卖出。与正常运行的模拟库相互隔离。
- `results/signal_parity.json` 记录 Freqtrade 策略与独立回测程序在数据集上逐根 K 线的信号比对。回测模型按下一根开盘价加方向性滑点成交，实时模拟盘按 Freqtrade 的订单簿模拟规则估算。
- `config.paper.json` 的 99.20 美元为不含手续费的名义订单金额，按单边 0.80% 预算接近 100 美元。实时盘口报价和模拟成交价可有差别。5% 回撤用于评估和观察，需查看记录后采取暂停操作。
- 实时模拟盘只需公共行情。实盘上线需独立审核账户资格、密钥、仓位上限、停机策略和远端订单对账，并取得账户本人的明确决定。
