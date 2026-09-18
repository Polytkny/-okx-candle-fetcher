#!/usr/bin/env python3
"""
OKX 永續合約歷史K線抓取工具（公開資料，無任何交易邏輯）。

抓一份夠細的基礎K棒(預設3分鐘)，往上合併成各種週期(15/30/60/90/120/180/240分)，
存成CSV。OKX原生只提供固定檔位，沒有90/180分鐘這種非標準週期，但只要基礎K棒
能被目標週期整除就能自己合併出來，不用一個一個跟OKX要。

用法：
    python3 okx_fetch.py --inst XRP-USDT-SWAP --days 90 --bar 3m

只用到 requests，打的是 OKX 公開行情端點，不需要任何 API 金鑰。
"""
from __future__ import annotations
import argparse
import csv
import time
from dataclasses import dataclass

try:
    import requests
except ImportError:  # 在只做邏輯驗證(不連網)時，requests 可能沒裝，先讓合併函式能單獨測試
    requests = None

OKX_BASE = "https://www.okx.com"
HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"
CANDLES_PATH = "/api/v5/market/candles"


@dataclass
class Candle:
    ts_ms: int  # K棒開盤時間(毫秒)
    o: float
    h: float
    l: float
    c: float
    vol: float

    @staticmethod
    def from_okx_row(row: list) -> "Candle":
        # OKX candle row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        return Candle(
            ts_ms=int(row[0]),
            o=float(row[1]),
            h=float(row[2]),
            l=float(row[3]),
            c=float(row[4]),
            vol=float(row[5]),
        )


def fetch_history_candles(inst_id: str, bar: str, before_ms: int, after_ms: int) -> list[Candle]:
    """
    分批往回抓OKX的歷史K棒。history-candles 一次最多回傳100根，用 `after`
    參數(取比這個時間戳更早的資料)一直往回翻頁，直到翻到 after_ms 之前為止。
    OKX對這支API有速率限制，這裡故意每次呼叫間隔一下，避免被擋。
    """
    if requests is None:
        raise RuntimeError("需要 requests 套件：pip install requests")
    all_rows: list[Candle] = []
    cursor = before_ms  # 從「現在」往回翻頁；每次都要求比 cursor 更早的資料
    while True:
        params = {"instId": inst_id, "bar": bar, "limit": "100", "after": str(cursor)}
        resp = requests.get(OKX_BASE + HISTORY_CANDLES_PATH, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            break
        rows = [Candle.from_okx_row(r) for r in data]
        rows.sort(key=lambda c: c.ts_ms)  # OKX 回傳新到舊，這裡先排成舊到新方便處理
        all_rows = rows + all_rows
        oldest = rows[0].ts_ms
        if oldest <= after_ms:
            break
        cursor = oldest
        time.sleep(0.25)  # 放慢一點，避免打到OKX的rate limit
    # 過濾範圍、去重（分頁邊界可能重疊）
    seen = set()
    filtered = []
    for c in all_rows:
        if after_ms <= c.ts_ms <= before_ms and c.ts_ms not in seen:
            seen.add(c.ts_ms)
            filtered.append(c)
    filtered.sort(key=lambda c: c.ts_ms)
    return filtered


def resample(base_candles: list[Candle], group_size: int) -> list[Candle]:
    """
    把基礎K棒每 group_size 根合併成一根更大週期的K棒：
    開盤=這組第一根的開盤，收盤=這組最後一根的收盤，
    高點=這組所有高點的最大值，低點=這組所有低點的最小值，量=這組成交量加總。
    只合併「湊滿一整組」的部分，最後湊不滿一組的尾巴直接丟掉——
    避免用一根不完整的K棒混進資料，造成之後回測時的假訊號。
    """
    out: list[Candle] = []
    for i in range(0, len(base_candles) - group_size + 1, group_size):
        chunk = base_candles[i : i + group_size]
        out.append(
            Candle(
                ts_ms=chunk[0].ts_ms,
                o=chunk[0].o,
                h=max(x.h for x in chunk),
                l=min(x.l for x in chunk),
                c=chunk[-1].c,
                vol=sum(x.vol for x in chunk),
            )
        )
    return out


def save_csv(path: str, candles: list[Candle]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts_ms, c.o, c.h, c.l, c.c, c.vol])


# 目標週期(分鐘) -> 需要合併幾根基礎K棒。基礎K棒週期由 --base-bar 決定，
# 這裡假設基礎是3分鐘（BASE_MINUTES=3），全部都整除，見檔案開頭說明。
# 基礎K棒週期字串 -> 分鐘數。原本這裡寫死 3，選別的 --bar 會用錯的倍數去合併，
# 而且不會報錯，只會安靜地產生錯誤的K線。
BAR_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30}
TARGET_MINUTES = [15, 30, 60, 90, 120, 180, 240]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--bar", default="3m", help="基礎K棒週期，預設3分鐘")
    ap.add_argument("--days", type=int, default=90, help="要往回抓幾天的資料")
    args = ap.parse_args()

    base_minutes = BAR_MINUTES.get(args.bar)
    if base_minutes is None:
        raise SystemExit(f"不支援的基礎週期 {args.bar}，可用：{', '.join(BAR_MINUTES)}")

    now_ms = int(time.time() * 1000)
    after_ms = now_ms - args.days * 24 * 60 * 60 * 1000
    print(f"抓取 {args.inst} {args.bar} K棒，往回 {args.days} 天...")
    base = fetch_history_candles(args.inst, args.bar, before_ms=now_ms, after_ms=after_ms)
    print(f"共抓到 {len(base)} 根基礎K棒")
    raw_path = f"ohlc_{args.bar}_raw.csv"
    save_csv(raw_path, base)
    print(f"{raw_path}：{len(base)} 根")

    for minutes in TARGET_MINUTES:
        if minutes % base_minutes != 0:
            print(f"跳過 {minutes} 分鐘：無法被基礎週期 {base_minutes} 分鐘整除")
            continue
        group_size = minutes // base_minutes
        merged = resample(base, group_size)
        path = f"ohlc_{minutes}m.csv"
        save_csv(path, merged)
        print(f"{path}：{len(merged)} 根")


if __name__ == "__main__":
    main()
