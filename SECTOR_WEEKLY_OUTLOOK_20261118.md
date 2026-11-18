# Risk、Fed 同未來 1–4 週美股板塊展望

2026-11-18 香港時間。訊號用美股 2026-11-17 收市，最新已完成回測價格截點 2026-11-13。延續 [SectorETF02](SECTOR_ROTATION_RESEARCH.md) 研究。[Watchlist](https://pepe760.github.io/market-data-pipeline/#watchlist)

**基準情景係波動偏高、SPY 方向接近五五開。** 一至兩週嘅歷史類比均值偏弱，但中位數接近零，改用兩年債息或轉星期取樣後會改變。四週時防守相對週期有輕微優勢，預測力細，股市絕對方向不明朗。

## 歷史走勢同預測力

- **Risk：** 沿用 21 個交易日 VIX 同 Moody's Baa–10 年美債利差方向。兩項升係 Risk-Off，兩項跌係 Risk-On，其他係混合。FRED 用前兩個交易日可見值代理，VIX 用當日收市。
- **Fed：** 用 SF Fed USMPD 過去 63 個交易日累積嘅 FOMC 窗口利率路徑意外，每次平均 ED2／ED3／ED4，再加總。上升至少 5 bp 為偏鷹，下降至少 5 bp 為偏鴿，事件至少滯後一個交易日。兩年債息 21 日方向係會議間重定價嘅替代檢查。兩年債息亦包括增長、通脹及期限風險預期。
- **比較：** 2001 年 7 月起每個有交易嘅星期二收市定訊號，下一個交易日收市入場，之後持有 5／10／20 個交易日。用股息及拆合股調整價。防守組係 XLP／XLV／XLU，週期組係 XLK／XLY／XLI，兩邊等權。對沖係以訊號日以前連同當日、最近 252 個交易日超現金回報估 SPY beta，再固定對沖整個持有期。SPY 絕對回報與呢個相對價差係兩個目標。數字未計交易成本。

### Risk 同 Fed 分開測

| 星期二歷史狀態 | SPY 5／10／20 日平均 | 防守減週期，對沖後 5／10／20 日平均 | 20 日樣本 |
|---|---:|---:|---:|
| 所有狀態 | +0.22／+0.42／+0.77% | −0.01／0.00／+0.02 pp | 1,306 |
| Risk-Off | +0.15／+0.40／+0.65% | −0.01／+0.01／+0.13 pp | 320 |
| Fed 路徑偏鷹 | +0.08／+0.24／+0.47% | +0.14／+0.36／+0.71 pp | 240 |
| Risk-Off ＋ Fed 偏鷹 | −0.58／−0.66／−0.42% | −0.07／+0.06／+0.73 pp | 63 |
| Risk-Off ＋ 兩年債息升 | −0.22／+0.18／+0.72% | +0.15／+0.21／+0.78 pp | 95 |

試用相同結果嘅歷史方向機率做擴張窗口走步驗證。由 2018 年起，每次只用訊號日已完成嘅窗口訓練，少數狀態以 25 個基準偽觀測拉回無條件歷史基準。主聯合訊號喺 5／10／20 日對 SPY 升跌嘅 Brier 誤差分別高 1.14／0.53／1.11%。對防守組相對強弱，誤差低 0.22／0.25／0.69%，幅度小。Fed 單獨估 20 日防守相對方向，誤差低 1.59%。呢啲屬回溯走步檢查，持有窗口可重疊。年度區塊重抽留住同年集中事件，少量相關週期仍然會影響結果。

### 真正加息同意外偏鷹

| FOMC 事件 | 防守減週期，對沖後 5／10／20 日平均 | 20 日樣本 |
|---|---:|---:|
| 實際加息 | +0.16／+0.33／+0.64 pp | 41 |
| 定期會議維持利率 | −0.06／−0.06／−0.28 pp | 145 |
| 定期會議利率期貨路徑意外上調 ≥5 bp | +0.34／+0.23／+0.77 pp | 30 |
| 定期會議路徑意外在 ±5 bp 之內 | −0.09／−0.17／−0.26 pp | 142 |

政策變動配對公布日或次日生效嘅 Fed Funds 目標／上限。呢批事件可能重疊同一輪加息，Fed 亦可同時傳遞經濟消息。2026-10-28 會議維持利率，當次路徑意外係約 −6.17 bp。近日嘅利率壓力主要來自會議與會議之間嘅市場重定價。

移除重疊類比後，主聯合狀態 20 日仍有 30 個窗口，防守對沖後 +0.60 pp，兩年債息替代係 50 個窗口、+0.82 pp。同期 SPY 均值則分別 −0.72% 同 +0.69%。

## 11 月 17 日訊號同展望

固定與回測相同嘅發布滯後，21 個交易日窗口 VIX **18.44 → 25.46**，Baa 利差 **1.44% → 1.47%**，兩年債息 **4.77% → 4.88%**。近 63 日 FOMC 累積路徑意外 **+9.67 bp**。主測試係 **Risk-Off ＋ Fed 偏鷹**。短至 5 日，VIX 升 **6.00 點**，Baa 利差收窄 **9 bp**，兩年債息升 **37 bp**，顯示波動升溫快過廣泛信用壓力。

另外用可比固定合約核對會議間預期。Atlanta Fed 2027-03-17 開始嗰季嘅期權隱含平均 SOFR，10-30 到 11-16 由 4.325% 升到 4.580%。11-17 美國財政部實際兩年債息收 **4.80%**、十年實息 **2.89%**，兩者比 11-16 跌約 3 bp。11-16 最新 Baa 利差 **1.50%**，高收益 OAS **3.34%**，都低於 10-30。合約期平均 SOFR 係期權價格推算，同單次加息機率有分別。

### 凍結預測窗口

以美股 11-18 收市為起點。下表全期數字係星期二聯合類比嘅歷史分布，區間唔係預測保證。

| 時距 | 評估截點（美股收市） | SPY 平均／上升比例 | 防守跑贏週期，已扣 beta |
|---|---|---:|---:|
| 一週，5 日 | 11-25 | −0.58%／50%（66） | −0.07 pp／52% |
| 兩週，10 日 | 12-03 | −0.66%／48%（65） | +0.06 pp／48% |
| 四週，20 日 | 12-17 | −0.42%／52%（63） | +0.73 pp／59% |

- **波動預測較清楚。** 入場訊號 VIX 在 25 至 30 嘅 105 個星期二樣本中，一、二、四週後 VIX 仍 ≥20 嘅比例係 93%、90%、87%，四週 VIX 中位數 24.68。呢項係狀態類比，與 Risk＋Fed 聯合價格樣本分開。
- 首兩週較符合高波幅拉鋸，中位數分別 −0.03%、−0.12%。兩週平均嘅 95% 年度重抽區間約 −1.96 至 +0.46%。改用兩年債息時，兩週平均反而 +0.18%。
- 四週防守對沖後相對回報均值 +0.73 pp，95% 區間 −0.42 至 +1.97 pp。2018 年起主類比 24 次，防守跑贏 63%，平均 +1.25 pp。XLK 對沖後四週喺主測及兩年債息版本都偏弱（全期 −0.62／−0.45 pp）。XLP 兩組都輕微正數，XLV 同 XLU 單隻優勢隨樣本改變。防守相對跑贏可以同 SPY 或防守 ETF 絕對下跌同時出現。
- 21 日同 5 日嘅信用狀態有分歧。五日視窗若見 VIX 同信用利差同步升，防守情景會更有依據。如果 VIX 回落、利差持平或收窄，短線科技反彈仍然合理。四週 SPY 主類比中央 80% 歷史範圍係約 −6.3% 至 +4.7%。兩年債息版本係 −5.7% 至 +6.4%。

香港時間重要觀察點為 **11-19 03:00** Fed 會議紀錄，**11-25 21:30** PCE，**12-04 21:30** 就業，**12-10 03:00** Fed 決議及經濟預測，**12-10 21:30** CPI。價格會對公布同預期嘅差作反應。Nvidia 業績已在 11-17 美股收市後公布，下一個交易時段嘅反應亦可以影響 XLK。

## 數據及程式

- [可重跑程式](research/sector_weekly/study.py) · [歷史條件統計](research/sector_weekly/analog_summary.csv) · [走步驗證](research/sector_weekly/walkforward_summary.csv) · [Fed 事件](research/sector_weekly/fed_event_summary.csv) · [非重疊覆核](research/sector_weekly/nonoverlap_summary.csv) · [波動覆核](research/sector_weekly/volatility_summary.csv)
- [Cboe VIX](https://www.cboe.com/tradable-products/vix/vix-historical-data/) · [FRED Baa 利差](https://fred.stlouisfed.org/series/BAA10Y) · [FRED 高收益利差](https://fred.stlouisfed.org/series/BAMLH0A0HYM2) · [FRED 兩年債息](https://fred.stlouisfed.org/series/DGS2) · [SF Fed USMPD](https://www.frbsf.org/research-and-insights/data-and-indicators/us-monetary-policy-event-study-database/) · [Atlanta Fed SOFR 估算](https://www.atlantafed.org/research-and-data/data/market-probability-tracker)
- [Fed 議息日程](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) · [BEA 發布日程](https://www.bea.gov/news/schedule) · [BLS 發布日程](https://www.bls.gov/schedule/2026/12_sched.htm)

下載 USMPD.xlsx，配對上份研究嘅 SectorETF02 raw 快照及 [訊號快照](research/sector_weekly/live_snapshot.json)。星期一、五取樣及 10 bp 兩年債息門檻係敏感度覆核。額外風險因子、行業盈利、交易策略跌幅同持倉執行需要另行驗證。

```bash
python research/sector_weekly/study.py --raw research/raw --events USMPD.xlsx \
  --snapshot research/sector_weekly/live_snapshot.json --out work/sector_weekly
```
