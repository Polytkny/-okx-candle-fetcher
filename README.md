# okx-candle-fetcher

從 OKX 公開行情 API 抓永續合約歷史 K 線，存成 CSV。**純資料工具，不含任何交易邏輯。**

## 為什麼是獨立的公開 repo

GitHub Actions 對私有 repo 有每月分鐘數配額，用完之後 job 會直接無法啟動
（症狀是秒掛、零步驟、無日誌）。公開 repo 用的 GitHub-hosted runner 免費且
不限量，所以把「抓資料」這段單獨放在公開 repo，策略本身留在私有 repo。

## 用法

Actions 分頁 → **Fetch OKX candles** → Run workflow，填合約 ID、天數、基礎週期。
抓完的 CSV 會壓縮後 commit 進 `data/`，同時也會上傳成 artifact。

也可以在本機直接跑：

```bash
pip install requests
python3 okx_fetch.py --inst XRP-USDT-SWAP --days 90 --bar 3m
```

## 原理

OKX 原生只提供固定週期檔位（1/3/5/15/30 分、1/2/4/6/12 小時…），沒有 90 或 180
分鐘這種非標準週期。但只要基礎 K 棒夠細且能被目標週期整除，就能自己合併出來：

```
15分 = 5根3分 / 30分 = 10根 / 60分 = 20根 / 90分 = 30根 / 120分 = 40根
```

所以只抓一份 3 分鐘資料，就能重建以上全部週期，不用一個一個跟 OKX 要。
