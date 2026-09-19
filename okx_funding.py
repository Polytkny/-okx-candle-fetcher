#!/usr/bin/env python3
"""
OKX 永續合約資金費率歷史抓取（公開資料，無任何交易邏輯）。

為什麼抓這個：價格衍生的特徵已經測到盡頭。多個期間的動量、區間位置、
波動度、成交量，在樣本外全部無法維持符號，而且它們彼此高度相關——本質上
都在量同一件事（趨勢方向）。要改變結論需要的是新的資料源，不是新的特徵。

資金費率不是從價格算出來的。它是每 8 小時多方付給空方（或反過來）的錢，
反映的是**群眾部位的擁擠程度**與持有某一邊的成本——K 線裡沒有這個資訊。

OKX 的 funding-rate-history 端點分頁方式跟 history-candles 一樣，用 `after`
往回翻，一次最多 100 筆。8 小時一筆，所以一年約 1095 筆、五年約 5475 筆。

輸出欄位：
    ts_ms        資金費率結算時間（毫秒），用來跟 K 線對齊
    funding_rate 實際結算的費率（正=多方付空方，負=空方付多方）
    realized_rate OKX 回報的已實現費率（部分歷史區間才有）

用法：
    python3 okx_funding.py --inst BTC-USDT-SWAP --days 1825
"""
from __future__ import annotations
import argparse
import csv
import time

try:
    import requests
except ImportError:
    requests = None

OKX_BASE = "https://www.okx.com"
FUNDING_HISTORY_PATH = "/api/v5/public/funding-rate-history"


def fetch_funding(inst_id: str, before_ms: int, after_ms: int) -> list[dict]:
    """
    往回翻頁抓資金費率。跟 K 線抓取同一個模式：用 `after` 要求比游標更早的
    資料，直到翻過目標起點或 OKX 不再回傳為止。

    OKX 對這支有速率限制，所以每次呼叫之間停一下。游標嚴格遞減（回傳的都
    比游標早），不會卡住。
    """
    if requests is None:
        raise RuntimeError("需要 requests 套件：pip install requests")
    rows: list[dict] = []
    cursor = before_ms
    while True:
        params = {"instId": inst_id, "limit": "100", "after": str(cursor)}
        resp = requests.get(OKX_BASE + FUNDING_HISTORY_PATH, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data:
            break
        batch = []
        for r in data:
            try:
                batch.append({
                    "ts_ms": int(r["fundingTime"]),
                    "funding_rate": float(r["fundingRate"]),
                    # realizedRate 只有部分歷史區間有，缺了就退回 fundingRate
                    "realized_rate": float(r.get("realizedRate") or r["fundingRate"]),
                })
            except (KeyError, TypeError, ValueError):
                continue          # 單筆格式異常不該中斷整批
        if not batch:
            break
        batch.sort(key=lambda x: x["ts_ms"])
        rows = batch + rows
        oldest = batch[0]["ts_ms"]
        if oldest <= after_ms:
            break
        cursor = oldest
        time.sleep(0.25)

    seen, out = set(), []
    for r in rows:
        if after_ms <= r["ts_ms"] <= before_ms and r["ts_ms"] not in seen:
            seen.add(r["ts_ms"])
            out.append(r)
    out.sort(key=lambda x: x["ts_ms"])
    return out


def save_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts_ms", "funding_rate", "realized_rate"])
        for r in rows:
            w.writerow([r["ts_ms"], r["funding_rate"], r["realized_rate"]])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inst", default="BTC-USDT-SWAP")
    ap.add_argument("--days", type=int, default=1825)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    now_ms = int(time.time() * 1000)
    after_ms = now_ms - args.days * 24 * 60 * 60 * 1000
    print(f"抓取 {args.inst} 資金費率，往回 {args.days} 天...")
    rows = fetch_funding(args.inst, before_ms=now_ms, after_ms=after_ms)
    path = args.out or "funding_raw.csv"
    save_csv(path, rows)
    if rows:
        span = (rows[-1]["ts_ms"] - rows[0]["ts_ms"]) / 86_400_000
        avg = sum(r["funding_rate"] for r in rows) / len(rows)
        print(f"{path}：{len(rows)} 筆，涵蓋 {span:.0f} 天，"
              f"平均費率 {avg * 100:.4f}%／期")
    else:
        print(f"{path}：沒有資料（標的可能不存在或上市較晚）")


if __name__ == "__main__":
    main()
