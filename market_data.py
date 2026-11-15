"""Download and check public Kraken BTC/USD four-hour candles."""

import argparse
import csv
from dataclasses import astuple, dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zipfile import ZipFile


STEP = 4 * 60 * 60
MIN_START = "2024-09-01"
ASSETS = "https://assets.kraken.com/marketing/institutions"
PUBLIC = "https://api.kraken.com/0/public"
ARCHIVE_PARTS = tuple(f"{ASSETS}/Kraken_OHLCVT_Full.zip.part{i:02d}" for i in range(5))
HEADERS = {"User-Agent": "kraken-btcusd-paper/1.0", "Accept-Encoding": "identity"}
COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "trades")


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int


def request(url, *, method="GET", headers=None, check=None):
    request_headers = HEADERS | (headers or {})
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=request_headers, method=method), timeout=25) as reply:
                if check:
                    check(reply)
                return reply.headers, reply.read()
        except (OSError, URLError) as exc:
            if attempt == 2 or isinstance(exc, HTTPError) and exc.code < 500 and exc.code != 429:
                raise
            time.sleep(2 ** attempt)


class RemoteArchive(io.RawIOBase):
    """Expose Kraken's numbered raw ZIP parts as a single seekable file."""

    def __init__(self, urls):
        self.parts = []
        self.size = 0
        self.position = 0
        for url in urls:
            headers, _ = request(url, method="HEAD")
            length = int(headers["Content-Length"])
            self.parts.append((self.size, self.size + length, url, headers.get("ETag")))
            self.size += length

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self.position, io.SEEK_END: self.size}[whence]
        self.position = origin + offset
        if self.position < 0:
            raise OSError("Cannot seek before the archive start")
        return self.position

    def read(self, size=-1):
        remaining = max(0, self.size - self.position)
        size = remaining if size < 0 else min(size, remaining)
        if size > 16 * 1024 * 1024:
            raise OSError("Archive request exceeded 16 MiB. Check the Kraken archive manifest.")
        chunks = []
        while size:
            start, end, url, etag = next(part for part in self.parts if part[0] <= self.position < part[1])
            first = self.position - start
            count = min(size, end - self.position)
            last = first + count - 1

            def check(reply):
                expected = f"bytes {first}-{last}/{end - start}"
                if reply.status != 206 or reply.headers.get("Content-Range") != expected:
                    raise ValueError(f"Invalid byte range from {url}")
                if etag and reply.headers.get("ETag") != etag:
                    raise ValueError(f"Archive changed during download: {url}")

            _, data = request(url, headers={"Range": f"bytes={first}-{last}"}, check=check)
            if len(data) != count:
                raise ValueError(f"Truncated byte range from {url}")
            chunks.append(data)
            self.position += count
            size -= count
        return b"".join(chunks)


def make_candle(row):
    return Candle(int(row[0]), *(float(value) for value in row[1:6]), int(row[6]))


def archive_candles(interval=240):
    with RemoteArchive(ARCHIVE_PARTS) as remote, ZipFile(remote) as archive:
        matches = [item for item in archive.infolist() if re.fullmatch(
            rf"(?:XBTUSD|XXBTZUSD|BTCUSD)_{interval}\.csv", Path(item.filename).name, re.IGNORECASE
        )]
        if len(matches) != 1:
            raise ValueError(f"Expected one BTC/USD {interval}-minute archive entry, found {matches}")
        entry = matches[0]
        data = archive.read(entry).decode("utf-8")
        rows = [make_candle(row) for row in csv.reader(io.StringIO(data)) if row]
        return rows, entry.filename, f"{entry.CRC:08x}"


def public_series(endpoint, params):
    _, content = request(f"{PUBLIC}/{endpoint}?{urlencode({'pair': 'XBTUSD', **params})}")
    response = json.loads(content)
    if response.get("error"):
        raise ValueError(f"Kraken {endpoint} returned {response['error']}")
    series, = (value for key, value in response["result"].items() if key != "last")
    return series


def public_ohlc(since=None):
    params = {"interval": 240}
    if since is not None:
        params["since"] = since
    series = public_series("OHLC", params)
    if not series:
        raise ValueError("Kraken OHLC returned no candles")
    # Kraken includes the current uncommitted candle at the end of every response.
    return [make_candle((*row[:5], row[6], row[7])) for row in series[:-1]], int(series[-1][0])


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def validate(candles):
    if not candles:
        raise ValueError("No candles")
    for index, candle in enumerate(candles):
        if candle.timestamp % STEP or index and candle.timestamp != candles[index - 1].timestamp + STEP:
            raise ValueError(f"Unaligned, duplicate or missing candle at {utc(candle.timestamp)}")
        prices = (candle.open, candle.high, candle.low, candle.close)
        if not all(map(math.isfinite, prices)) or not (0 < candle.low <= min(prices) <= max(prices) <= candle.high):
            raise ValueError(f"Invalid prices at {utc(candle.timestamp)}")
        empty = candle.volume == 0 and candle.trades == 0
        if not empty and not (0 < candle.volume < float('inf') and candle.trades > 0):
            raise ValueError(f"Invalid volume or count at {utc(candle.timestamp)}")
        if empty and (index == 0 or any(price != candles[index - 1].close for price in prices)):
            raise ValueError(f"Invalid no-trade carry-forward at {utc(candle.timestamp)}")


def fill_no_trade_intervals(rows):
    filled = []
    confirmed = []
    for candle in rows:
        if filled:
            previous = filled[-1]
            gap = candle.timestamp - previous.timestamp
            if not STEP <= gap <= 6 * STEP or gap % STEP:
                raise ValueError(f"Unexpected interval length {gap} seconds at {utc(candle.timestamp)}")
            for timestamp in range(previous.timestamp + STEP, candle.timestamp, STEP):
                trades = public_series("Trades", {"since": timestamp * 10**9 - 1, "count": 1})
                if not trades or float(trades[0][2]) < timestamp + STEP:
                    raise ValueError(f"Cannot verify a zero-trade interval at {utc(timestamp)}")
                confirmed.append({"open_utc": utc(timestamp), "next_trade_utc": utc(float(trades[0][2]))})
                filled.append(Candle(timestamp, previous.close, previous.close, previous.close, previous.close, 0, 0))
        filled.append(candle)
    return filled, confirmed


def cross_check(archive, public):
    recorded = {candle.timestamp: candle for candle in archive}
    overlap = [candle for candle in public if candle.timestamp in recorded]
    if len(overlap) < 3:
        raise ValueError("Need at least three overlapping archive and REST candles")
    for candle in overlap:
        other = recorded[candle.timestamp]
        if astuple(candle)[:5] != astuple(other)[:5]:
            raise ValueError(f"Archive and REST prices disagree at {utc(candle.timestamp)}")
    return len(overlap)


def write_candles(path, candles):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(map(astuple, candles))
    temp.replace(path)


def read_candles(path):
    with path.open(encoding="utf-8", newline="") as stream:
        rows = csv.reader(stream)
        if tuple(next(rows)) != COLUMNS:
            raise ValueError(f"Invalid candle header in {path}")
        candles = [make_candle(row) for row in rows]
    validate(candles)
    return candles


def download(destination, start):
    archive, entry, crc = archive_candles()
    begin = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    archive = [candle for candle in archive if candle.timestamp >= begin]
    if not archive or archive[0].timestamp != begin:
        raise ValueError(f"Archive does not begin on the requested {start} grid")
    public, current = public_ohlc(archive[-1].timestamp - 12 * STEP)
    overlap = cross_check(archive, public)
    added = [candle for candle in public if candle.timestamp > archive[-1].timestamp]
    candles, empty_intervals = fill_no_trade_intervals(archive + added)
    validate(candles)
    if candles[-1].timestamp + STEP != current:
        raise ValueError("No complete coverage through the latest committed candle")
    csv_path = destination / "BTC_USD-4h.csv"
    write_candles(csv_path, candles)
    report = {
        "symbol": "BTC/USD", "interval_seconds": STEP, "first_open_utc": utc(candles[0].timestamp),
        "last_open_utc": utc(candles[-1].timestamp), "cutoff_utc": utc(current), "bars": len(candles),
        "archive_entry": entry, "archive_crc32": crc, "archive_bars": len(archive), "archive_parts": list(ARCHIVE_PARTS),
        "rest_endpoint": f"{PUBLIC}/OHLC", "rest_bars_added": len(added), "overlap_ohlc_matched": overlap,
        "confirmed_no_trade_intervals": empty_intervals,
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    }
    (destination / "data_manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("data"))
    parser.add_argument("--start", default=MIN_START)
    args = parser.parse_args()
    print(json.dumps(download(args.destination, args.start), indent=2))
