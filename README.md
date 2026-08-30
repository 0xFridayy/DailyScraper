# DailyScraper

Pipeline riset trading IDX: scrape data broker/harga saban hari dari NeoBDM, simpan ke SQLite, lalu diuji lewat serangkaian eksperimen fitur, model, dan strategi.

> **⚠️ Baca [`HANDOFF.md`](HANDOFF.md) sebelum menjalankan backtest apa pun.**
>
> Audit menemukan dua masalah yang membatalkan seluruh angka Sharpe yang tercatat di
> docstring repo ini:
>
> 1. **`price_history` rusak** — kontaminasi silang antar-ticker, yang juga merusak
>    netval di `broker_flow`. Per 2026-08-21: **501 / 11.267 baris (4,4%)**, turun dari
>    1.400 (12,5%) sehari sebelumnya — satu rerun scraper menyembuhkan 899 baris,
>    membuktikan ini race condition. Penyebabnya sudah diperbaiki; sisa backlog belum.
> 2. **Formula Sharpe salah** — `mean/std * √252` diterapkan pada return *per-trade*
>    yang overlapping cross-sectional, di 4 file.
>
> Konsekuensinya, kesimpulan lama "tesis broker flow gugur" **belum bisa ditarik** —
> itu diukur di atas data rusak. `HANDOFF.md` memuat urutan kerja 5 tahap,
> daftar yang jangan dikerjakan dulu, dan alasannya.

## Alur pipeline

```
01 Capture  ->  02 Storage (neobdm.db)  ->  03 Fitur & Model  ->  04 Strategi & Risiko  ->  05 Laporan & Otomasi
```

## Fungsi tiap file

### 1. Pengambilan & kesehatan data

Yang benar-benar menyentuh NeoBDM.tech dan mengisi database. Satu-satunya lapisan yang wajib jalan tiap hari tanpa gagal diam-diam.

| File | Status | Fungsi |
|---|---|---|
| `neobdm_scraper.py` | live | Inti scraper. Login ke NeoBDM via Playwright, ambil Market Summary + Broker Stalker lewat API screener internal, tulis ke `neobdm.db`, kirim ringkasan Top-2 harian ke Telegram. Juga jalan sebagai bot terjadwal (7 pagi WIB) yang merespons perintah `/scrape`. |
| `backfill_inventory.py` | live | Mengisi histori `broker_flow` & `price_history` lewat endpoint JSON `/api/inventory`; Playwright hanya dipakai untuk memperoleh sesi login. Payload wajib cocok dengan ticker yang diminta dan duplikasi seri lintas-ticker ditolak sebelum penyimpanan. |
| `check_capture_health.py` | live | Health check harian atas panel ML: cek bentuk data (jumlah baris, coverage kolom, kebaruan tanggal), bukan cuma "ada isinya atau tidak". Exit code non-nol + alert Telegram kalau capture diam-diam rusak (filter berhenti kepakai, kolom jadi null). |
| `check_signal_integrity.py` | live | Gerbang **kebenaran** data hasil scrape — bukan sekadar "datanya sampai" (itu tugas `check_capture_health.py`), tapi "nilainya benar". Membandingkan dua jalur scrape independen (API screener vs chart inventory) yang sama-sama membawa `close`, mendeteksi kontaminasi baru dalam 10 hari terakhir, dan menyapu seluruh ~340 kolom untuk regresi cakupan secara self-calibrating (kolom yang tidak pernah terisi diabaikan; kolom yang tadinya penuh lalu kosong = gagal). Exit non-nol + alert Telegram. |
| `price_audit.py` | audit | Audit + perbaikan integritas `price_history`. Tiga detektor: `limit_violation` (gerakan di luar ARA/ARB — mustahil di IDX), `cross_ticker_dup` (OHLCV identik di ≥2 ticker pada satu tanggal), `series_break` (close melompat >5x / <0,2x versus rolling median sendiri). Mode: `audit` (laporan saja), `quarantine` (tandai, tidak menghapus), `repair corrected.csv` (perbaiki harga + rescale netval backfill). |

### 2. Fitur & model dasar

Membangun panel fitur dari data mentah dan menguji apakah ada sinyal yang bisa diprediksi — pondasi yang dipakai ulang oleh hampir semua file eksperimen di bawah.

| File | Status | Fungsi |
|---|---|---|
| `walk_forward_backtest.py` | riset — inti | Backtest XGBoost walk-forward: bangun panel bersih (agregat broker_flow + momentum/volume harga), latih-uji bergulir per siklus. Modul referensi — `build_panel`, `FEATURES`, `DB_PATH` dipakai ulang oleh hampir semua file lain. Sharpe 0,81 lama **VOID**; laporan sekarang memakai pooled IC, daily cross-sectional IC, top-decile hit/base/hit-edge, dan return edge. |
| `feature_ablation.py` | riset | Membandingkan tiga set fitur (lengkap / broker-saja / harga-saja) di tiga horizon (1/3/5 hari). Klaim lama bahwa price-only menang berdasarkan Sharpe adalah **VOID** dan harus dibaca ulang dari metrik ranking/edge pada data bersih. |
| `multiday_features.py` | riset | Menguji apakah pola broker multi-hari (rolling average, streak beli beruntun) menangkap sinyal yang tak terlihat di snapshot harian tunggal. Hasil: tidak ada perbaikan. |
| `smart_money_divergence.py` | riset | Menguji tesis spesifik: broker "smart money" net-beli sementara broker ritel net-jual pada saham yang sama (absorpsi). Hasil: konsisten arahnya tapi tidak signifikan secara statistik (p=0.18). |
| `shap_analysis.py` | riset | Interpretasi feature importance model lewat SHAP + gain importance XGBoost + korelasi mentah. Hasil: `broker_concentration` peringkat 7 dari 8 — `momentum_1d` mendominasi. |
| `horizon_scan.py` | riset — baru | Scan multi-horizon (h = 1/3/5/10/20) dengan metrik **IC / hit_rate / edge top-desil**, bukan Sharpe. Tiga varian target (`ret_h` close-to-close, `max_h` puncak dalam window, `mdd_h` drawdown terburuk) × empat feature set, plus **fitur cluster konglomerat** (netval per grup bandar dinormalisasi terhadap total \|netval\|) yang belum pernah dipakai di `FEATURES`. Tunggu data bersih dulu — lihat `HANDOFF.md` tahap 4. |

### 3. Strategi, eksekusi & risiko

Mengambil sinyal dari panel di atas dan menguji cara masuk/keluar posisi yang realistis — termasuk batasan aturan bursa dan ukuran taruhan.

| File | Status | Fungsi |
|---|---|---|
| `strategy_variants.py` | riset | Grid-search 11 varian exit (ambang entry, lama holding, take-profit/stop-loss) di atas sinyal model yang sama, dengan split search/holdout. Sharpe holdout 6,95 lama **VOID**; laporan aktif hanya menyajikan statistik trade non-annualized dan base rate. |
| `ara_arb_simulation.py` | riset | Mensimulasikan aturan Auto Rejection Atas/Bawah IDX terhadap strategi pemenang di atas — entry di hari ARA dibuang, exit yang macet di ARB digeser maju ke hari berikutnya yang tidak macet. |
| `ddqn_entry_exit.py` | riset | Agen Double DQN yang belajar kapan masuk *dan* keluar posisi sekaligus (bukan model entry + aturan exit terpisah), dengan realisme ARA/ARB, biaya transaksi, dan reward shaping loss-aversion. |
| `kelly_sizing.py` | dorman | Rumus ukuran posisi Kelly Criterion (dengan fraction cap / "half Kelly"). Tetap tidak dipakai sampai Layer 1 punya edge tervalidasi; tidak ada threshold pengganti Sharpe yang ditetapkan. |

### 4. Evaluasi, laporan & tes

Mengukur sinyal produksi terhadap kenyataan, merangkum semuanya jadi laporan harian, dan menjaga logika inti tidak bocor data.

| File | Status | Fungsi |
|---|---|---|
| `evaluate_signals.py` | live | Mengukur performa sinyal Telegram bot yang sesungguhnya: bandingkan saham yang di-flag vs sisa universe pada hari yang sama, dengan entry di close H+1 (bukan H, supaya tidak look-ahead). |
| `run_ml_reports.py` | live | Orkestrator laporan harian: menjalankan `walk_forward_backtest`, `strategy_variants`, dan `ddqn_entry_exit` atas `neobdm.db` saat ini, lalu kirim ringkasan ke Telegram + tabel lengkap ke GitHub Actions job summary. Read-only. |
| `check_ml_health.py` | live | Gerbang **kode** ML (pasangan dari `check_signal_integrity.py` yang menjaga datanya). Mengimpor tiap modul (menangkap drift versi pandas/xgboost — `requirements.txt` tidak mem-pin apa pun), menjalankan `test_pipeline.py`, membangun panel asli dan menguji invariannya, lalu satu siklus walk-forward nyata. Memakai **budget cacat**: cacat yang sudah diketahui dan terjadwal dilaporkan sebagai peringatan dengan jumlah ter-pin, dan hanya gagal kalau jumlahnya bertambah — supaya bisa mendeteksi regresi tanpa merah permanen. Juga selalu melaporkan base rate di samping hit_rate. |
| `test_pipeline.py` | tes | Tes regresi ringan (assert-based, tanpa framework) untuk `walk_forward_backtest.py`, `kelly_sizing.py`, dan gap guard di `price_audit.py` — fokus khusus mendeteksi kebocoran data (leakage) dan kebenaran formula. |

### 5. Otomasi & konfigurasi

Lima GitHub Actions workflow yang menjalankan file-file di atas secara terjadwal, plus file konfigurasi pendukung.

| Workflow | Jadwal (UTC) | Menjalankan |
|---|---|---|
| `daily-scrape.yml` | `0 23 * * *` | `neobdm_scraper.py --now` |
| `price-history-topup.yml` | `30 0 * * *` | `backfill_inventory.py` |
| `capture-health.yml` | `15 1 * * *` | `check_capture_health.py --telegram` |
| `signal-integrity.yml` | `45 1 * * *` | `check_signal_integrity.py --telegram` |
| `ml-health.yml` | `30 12 * * *` + **tiap push & PR** | `check_ml_health.py` |
| `ml-daily-report.yml` | `0 13 * * *` | `run_ml_reports.py` |
| `signal-eval.yml` | `0 2 * * 0` (mingguan) | `evaluate_signals.py --telegram` |

| File | Fungsi |
|---|---|
| `requirements.txt` | Daftar dependensi Python: playwright, requests, schedule, pytz, pandas, numpy, xgboost, shap, torch. |
| `.claude/launch.json` | Konfigurasi launch lokal untuk menjalankan scraper sebagai scheduler atau sekali jalan (`--now`). |
| `neobdm.db` | Database SQLite hasil scrape: tabel `market_summary_daily`, `broker_flow`, `price_history`, `konglo_signal_watch`, dst. Sumber data untuk semua file di atas. |
