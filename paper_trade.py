#!/usr/bin/env python3
"""
紙上交易：用真實行情跑規則、記錄決策，不下單。

為什麼這件事比再跑一次回測有價值：本 repo 所有結論的天花板是「有效獨立
標的只有 1.4 個」，而那是**資料本身**的限制，不是方法的限制。再怎麼精巧
的回測都跑不出新的獨立樣本。紙上交易每過一天就產生一天真正沒被看過的
資料——這是唯一能增加有效樣本的途徑，而且不花錢。

## 設計上的三個要求

**一、絕不可能前視。** 它只看得到「現在已經收盤的 K 棒」——OKX 回傳的第一筆
是進行中的那根，它的高低收還會變，所以一律丟棄。成交價用**下單當下的即時
報價**而不是訊號 K 棒的收盤價：那個價格在下單時已經過去了，用它就是前視。
回測的「次根開盤成交」對應到實盤就是這件事。兩者的差距（滑移）逐筆記錄，
因為回測假設它是零。

**二、決策必須可事後評分。** 每次決策連同當下看到的證據一起寫進 JSONL，
格式跟 `decision_eval.py` 相容，所以推理品質可以被獨立檢驗，不只看損益。

**三、同時跑多個變體。** 單一策略的結果無從比較。這裡同時跑：

    long_only   永續 1x 只做多       回測平均 +90%、中位數 +74%
    long_short  永續 1x 多空都做     回測平均 +97%、中位數 +33%
    buy_hold    買進持有（基準）      回測平均 -4%

三個共用同一份行情，所以差異純粹來自規則。回測說 long_only 的中位數比
long_short 好一倍，這是第一個能用真實樣本外資料檢驗它的機會。

## 規則

12 小時 K｜進場突破 20 日通道｜出場跌破／突破 7 日反向通道｜全押或空手。
兩邊都不提早（margin=0）——提早進出場的好處實測只存在於樣本內。

⚠️ 規則的參數是用全部五年資料挑的，所以回測數字是樣本內的。這支存在的
意義正是要知道它們在樣本外成不成立。

## 狀態

`paper_state.json` 存目前部位與權益，`paper_decisions.jsonl` 存每次決策。
重複執行同一根 K 棒不會重複交易（用 ts_ms 去重），漏跑幾次也能接上——
它讀的是當下的完整歷史，不依賴上次跑到哪裡。

用法：
    python3 paper_trade.py --insts BTC-USDT-SWAP,ETH-USDT-SWAP --state paper_state.json
    python3 paper_trade.py --dry-run          # 只印決策，不寫檔
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from collections import deque
from dataclasses import dataclass

try:
    import requests
except ImportError:
    requests = None


# 以下三個函式與主 repo 的 okx_backtest_data.py / trend_backtest.py 相同，
# 複製進來是為了讓這支能單獨放到抓取 repo 執行（那裡的 Actions 是免費的），
# 不必搬整套回測程式。任何一邊改動都要同步。

@dataclass
class Candle:
    ts_ms: int
    o: float
    h: float
    l: float
    c: float
    vol: float


def resample(base: list["Candle"], group: int) -> list["Candle"]:
    """每 group 根合併成一根。湊不滿一組的尾巴丟掉，不讓未完成的 K 棒混入。"""
    out = []
    for i in range(0, len(base) - group + 1, group):
        ch = base[i:i + group]
        out.append(Candle(ch[0].ts_ms, ch[0].o, max(x.h for x in ch),
                          min(x.l for x in ch), ch[-1].c, sum(x.vol for x in ch)))
    return out


def bars_per_day(bars: list["Candle"]) -> int:
    if len(bars) < 2:
        return 2
    step = bars[1].ts_ms - bars[0].ts_ms
    return max(1, round(86400000 / step)) if step > 0 else 2


def rolling_extreme(vals: list[float], n: int, want_max: bool) -> list[float]:
    """每個 i 的 vals[i-n:i]（不含當根）極值，單調佇列 O(len)。"""
    dq: deque[int] = deque()
    out = [0.0] * len(vals)
    for i in range(len(vals)):
        while dq and dq[0] < i - n:
            dq.popleft()
        out[i] = vals[dq[0]] if dq else vals[max(0, i - 1)]
        v = vals[i]
        while dq and ((vals[dq[-1]] <= v) if want_max else (vals[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
    return out

OKX_BASE = "https://www.okx.com"
CANDLES_PATH = "/api/v5/market/candles"
TICKER_PATH = "/api/v5/market/ticker"

BAR_HOURS = 12
ENTRY_DAYS, EXIT_DAYS = 20, 7
TAKER_FEE = 0.0005
VARIANTS = ("long_only", "long_short", "buy_hold")


def fetch_recent(inst: str, bar: str = "1H", need: int = 800) -> list[Candle]:
    """
    抓最近 need 根 K 棒。只用已收盤的——OKX 的第一筆是進行中的那根，
    它的高低收還會變，拿來判斷訊號等於用未完成的資料下決定。
    """
    if requests is None:
        raise RuntimeError("需要 requests 套件：pip install requests")
    rows: list[Candle] = []
    cursor = int(time.time() * 1000)
    while len(rows) < need:
        r = requests.get(OKX_BASE + CANDLES_PATH, timeout=15,
                         params={"instId": inst, "bar": bar, "limit": "300",
                                 "after": str(cursor)})
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break
        batch = []
        for row in data:
            # 最後一欄 confirm：0=進行中，1=已收盤。只要已收盤的
            if len(row) > 8 and row[8] == "0":
                continue
            batch.append(Candle(ts_ms=int(row[0]), o=float(row[1]), h=float(row[2]),
                                l=float(row[3]), c=float(row[4]), vol=float(row[5])))
        if not batch:
            break
        batch.sort(key=lambda c: c.ts_ms)
        rows = batch + rows
        cursor = batch[0].ts_ms
        time.sleep(0.2)
    seen, out = set(), []
    for c in rows:
        if c.ts_ms not in seen:
            seen.add(c.ts_ms)
            out.append(c)
    out.sort(key=lambda c: c.ts_ms)
    return out


def current_price(inst: str) -> float | None:
    """
    當下的成交價。訊號用已收盤 K 棒判斷，但成交不能用那根的收盤價——
    那個價格在下單時已經過去了，用它等於偷看未來。本 repo 的回測一律
    「訊號看收盤、成交用次根開盤」，紙上交易對應的就是「現在的價格」。
    """
    if requests is None:
        return None
    try:
        r = requests.get(OKX_BASE + TICKER_PATH, params={"instId": inst}, timeout=10)
        r.raise_for_status()
        data = r.json().get("data") or []
        return float(data[0]["last"]) if data else None
    except Exception:
        return None


def signal(bars: list[Candle]) -> dict:
    """
    用最後一根「已收盤」K 棒判斷。回傳訊號與當下的證據，證據會被寫進紀錄
    供事後評分——只記結論不記證據，事後就無法檢查推理對不對。
    """
    bpd = bars_per_day(bars)
    n_in = max(2, int(ENTRY_DAYS * bpd))
    n_out = max(2, int(EXIT_DAYS * bpd))
    up = rolling_extreme([b.h for b in bars], n_in, True)
    dn = rolling_extreme([b.l for b in bars], n_in, False)
    ex_lo = rolling_extreme([b.l for b in bars], n_out, False)
    ex_hi = rolling_extreme([b.h for b in bars], n_out, True)
    i = len(bars) - 1
    b = bars[i]
    return {
        "ts_ms": b.ts_ms, "close": b.c,
        "entry_high": up[i], "entry_low": dn[i],
        "exit_low": ex_lo[i], "exit_high": ex_hi[i],
        "break_up": b.c > up[i], "break_down": b.c < dn[i],
        "lose_support": b.c < ex_lo[i], "lose_resistance": b.c > ex_hi[i],
    }


def decide(sig: dict, pos: dict | None, variant: str) -> tuple[str, str]:
    """回傳 (動作, 理由)。理由要具體到事後能檢查對錯。"""
    c = sig["close"]
    if pos:
        if pos["side"] == 1 and sig["lose_support"]:
            return "CLOSE", (f"多單：收盤 {c:.6g} 跌破 {EXIT_DAYS} 日低 "
                             f"{sig['exit_low']:.6g}，結構破壞")
        if pos["side"] == -1 and sig["lose_resistance"]:
            return "CLOSE", (f"空單：收盤 {c:.6g} 突破 {EXIT_DAYS} 日高 "
                             f"{sig['exit_high']:.6g}，結構破壞")
        return "HOLD", (f"持倉中：收盤 {c:.6g}，出場線 "
                        f"{sig['exit_low'] if pos['side'] == 1 else sig['exit_high']:.6g}"
                        f"，未觸及")
    if sig["break_up"]:
        return "OPEN_LONG", (f"收盤 {c:.6g} 突破 {ENTRY_DAYS} 日高 "
                             f"{sig['entry_high']:.6g}")
    if sig["break_down"] and variant == "long_short":
        return "OPEN_SHORT", (f"收盤 {c:.6g} 跌破 {ENTRY_DAYS} 日低 "
                              f"{sig['entry_low']:.6g}")
    return "HOLD", (f"空手：收盤 {c:.6g}，進場線 {sig['entry_high']:.6g}"
                    f"（上）／{sig['entry_low']:.6g}（下），未觸及")


def apply(state: dict, inst: str, variant: str, sig: dict,
          action: str, reason: str, fill_px: float) -> dict | None:
    """
    更新狀態。回傳這次成交的紀錄，沒成交回傳 None。

    fill_px 是「下單當下的價格」，跟訊號用的收盤價分開。兩者相差多少
    也一併記錄——那個差距就是實盤相對回測的滑移，回測假設它是零。
    """
    v = state["variants"][variant]
    book = v["positions"]
    pos = book.get(inst)
    px = fill_px

    if action == "CLOSE" and pos:
        pnl_pct = (px / pos["entry"] - 1) * pos["side"] * 100
        gross = v["cash"][inst] * (1 + pnl_pct / 100)
        v["cash"][inst] = gross * (1 - TAKER_FEE)
        del book[inst]
        return {"action": "CLOSE", "price": px, "signal_close": sig["close"],
                "slip_pct": (px / sig["close"] - 1) * 100,
                "pnl_pct": pnl_pct, "equity": v["cash"][inst]}

    if action in ("OPEN_LONG", "OPEN_SHORT") and not pos:
        side = 1 if action == "OPEN_LONG" else -1
        v["cash"][inst] *= (1 - TAKER_FEE)
        book[inst] = {"side": side, "entry": px, "ts_ms": sig["ts_ms"]}
        return {"action": action, "price": px, "signal_close": sig["close"],
                "slip_pct": (px / sig["close"] - 1) * 100,
                "equity": v["cash"][inst]}
    return None


def new_state(insts: list[str]) -> dict:
    return {
        "created": datetime.now(timezone.utc).isoformat(),
        "rules": {"bar_hours": BAR_HOURS, "entry_days": ENTRY_DAYS,
                  "exit_days": EXIT_DAYS, "fee": TAKER_FEE},
        "last_bar": {},
        "variants": {v: {"cash": {i: 100.0 for i in insts}, "positions": {},
                         "first_price": {}}
                     for v in VARIANTS},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--insts", default="BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,"
                                       "XRP-USDT-SWAP,DOGE-USDT-SWAP")
    ap.add_argument("--state", default="paper_state.json")
    ap.add_argument("--log", default="paper_decisions.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    insts = [x.strip() for x in args.insts.split(",") if x.strip()]
    state = (json.load(open(args.state, encoding="utf-8"))
             if os.path.exists(args.state) else new_state(insts))
    for v in VARIANTS:                       # 新增標的時補齊，不重置既有的
        state["variants"][v]["cash"].setdefault
        for i in insts:
            state["variants"][v]["cash"].setdefault(i, 100.0)
            state["variants"][v].setdefault("first_price", {})

    now = datetime.now(timezone.utc)
    print(f"紙上交易｜{now:%Y-%m-%d %H:%M UTC}｜{len(insts)} 標的｜"
          f"{BAR_HOURS}h {ENTRY_DAYS}/{EXIT_DAYS}\n")

    log_rows = []
    for inst in insts:
        try:
            h1 = fetch_recent(inst)
        except Exception as e:
            print(f"  ⚠️ {inst} 抓取失敗（{type(e).__name__}），跳過這一輪")
            continue
        bars = resample(h1, BAR_HOURS)
        if len(bars) < ENTRY_DAYS * 2 + 10:
            print(f"  ⚠️ {inst} K 棒不足（{len(bars)}），跳過")
            continue
        sig = signal(bars)

        # 同一根 K 棒只處理一次，避免補跑或重跑造成重複交易
        if state["last_bar"].get(inst) == sig["ts_ms"]:
            print(f"  {inst:<18} 這根 K 棒已處理過，略過")
            continue
        state["last_bar"][inst] = sig["ts_ms"]

        bar_t = datetime.fromtimestamp(sig["ts_ms"] / 1000, timezone.utc)
        fill_px = current_price(inst) or sig["close"]
        slip = (fill_px / sig["close"] - 1) * 100
        print(f"  {inst:<18} K棒 {bar_t:%m-%d %H:%M}  收 {sig['close']:.6g}"
              f"  現價 {fill_px:.6g}（{slip:+.2f}%）")
        for variant in VARIANTS:
            v = state["variants"][variant]
            v["first_price"].setdefault(inst, fill_px)
            if variant == "buy_hold":
                v["cash"][inst] = 100.0 * fill_px / v["first_price"][inst]
                continue
            pos = v["positions"].get(inst)
            action, reason = decide(sig, pos, variant)
            fill = apply(state, inst, variant, sig, action, reason, fill_px)
            tag = "→ " + fill["action"] if fill else "   " + action
            print(f"      {variant:<11}{tag:<14}{reason[:56]}")
            log_rows.append({
                "ts_ms": sig["ts_ms"], "wall_clock": now.isoformat(),
                "symbol": inst, "variant": variant,
                "action": action, "reason": reason,
                "position": {"entry": pos["entry"]} if pos else None,
                "evidence": [
                    {"tool": "get_channel", "args": str(ENTRY_DAYS),
                     "result": f"{ENTRY_DAYS}日通道 {sig['entry_low']:.6g} ~ "
                               f"{sig['entry_high']:.6g}，現價 {sig['close']:.6g}"},
                    {"tool": "get_channel", "args": str(EXIT_DAYS),
                     "result": f"{EXIT_DAYS}日通道 {sig['exit_low']:.6g} ~ "
                               f"{sig['exit_high']:.6g}，現價 {sig['close']:.6g}"},
                ],
                "fill": fill,
            })

    print(f"\n{'變體':<13}{'權益':>9}{'報酬':>9}{'持倉':>7}")
    print("-" * 40)
    for variant in VARIANTS:
        v = state["variants"][variant]
        vals = [v["cash"][i] for i in insts if i in v["cash"]]
        eq = sum(vals) / len(vals) if vals else 100.0
        print(f"{variant:<13}{eq:>8.2f}{eq - 100:>8.1f}%"
              f"{len(v['positions']):>7}")

    if args.dry_run:
        print("\n（dry-run：沒有寫入任何檔案）")
        return
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    if log_rows:
        with open(args.log, "a", encoding="utf-8") as f:
            for r in log_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n狀態 → {args.state}｜決策 +{len(log_rows)} 筆 → {args.log}")


if __name__ == "__main__":
    main()
