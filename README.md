# DailyScraper

Pipeline riset trading IDX: scrape data broker/harga saban hari dari NeoBDM, simpan ke SQLite, lalu diuji lewat serangkaian eksperimen fitur, model, dan strategi.

Status ringkas: sinyal yang ada saat ini didorong **momentum harga**, bukan tesis akumulasi broker — Sharpe pooled masih di bawah target validasi 1.5 (lihat `walk_forward_backtest.py`).

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
| `backfill_inventory.py` | live | Mengisi histori `broker_flow` & `price_history` dari chart Plotly di halaman `/inventory/`, dengan menggerakkan date-picker ke tanggal paling awal yang tersedia (bukan cuma default 3 bulan). Dijalankan harian untuk top-up data yang terlewat. |
| `check_capture_health.py` | live | Health check harian atas panel ML: cek bentuk data (jumlah baris, coverage kolom, kebaruan tanggal), bukan cuma "ada isinya atau tidak". Exit code non-nol + alert Telegram kalau capture diam-diam rusak (filter berhenti kepakai, kolom jadi null). |

### 2. Fitur & model dasar

Membangun panel fitur dari data mentah dan menguji apakah ada sinyal yang bisa diprediksi — pondasi yang dipakai ulang oleh hampir semua file eksperimen di bawah.

| File | Status | Fungsi |
|---|---|---|
| `walk_forward_backtest.py` | riset — inti | Backtest XGBoost walk-forward: bangun panel fitur (agregat broker_flow + momentum/volume harga), latih-uji bergulir per siklus. Modul referensi — `build_panel`, `FEATURES`, `DB_PATH` dipakai ulang oleh hampir semua file lain di repo ini. Hasil terkini: Sharpe pooled 0.81 (242 hari), hit-rate 42.8%. |
| `feature_ablation.py` | riset | Membandingkan tiga set fitur (lengkap / broker-saja / harga-saja) di tiga horizon (1/3/5 hari) untuk menjawab: apakah data broker menambah sesuatu, atau sinyalnya cuma momentum harga? Hasil: fitur harga-saja justru skor Sharpe terbaik. |
| `multiday_features.py` | riset | Menguji apakah pola broker multi-hari (rolling average, streak beli beruntun) menangkap sinyal yang tak terlihat di snapshot harian tunggal. Hasil: tidak ada perbaikan. |
| `smart_money_divergence.py` | riset | Menguji tesis spesifik: broker "smart money" net-beli sementara broker ritel net-jual pada saham yang sama (absorpsi). Hasil: konsisten arahnya tapi tidak signifikan secara statistik (p=0.18). |
| `shap_analysis.py` | riset | Interpretasi feature importance model lewat SHAP + gain importance XGBoost + korelasi mentah. Hasil: `broker_concentration` peringkat 7 dari 8 — `momentum_1d` mendominasi. |

### 3. Strategi, eksekusi & risiko

Mengambil sinyal dari panel di atas dan menguji cara masuk/keluar posisi yang realistis — termasuk batasan aturan bursa dan ukuran taruhan.

| File | Status | Fungsi |
|---|---|---|
| `strategy_variants.py` | riset | Grid-search 11 varian exit (ambang entry, lama holding, take-profit/stop-loss) di atas sinyal model yang sama, dengan split search/holdout untuk menghindari overfitting. Hasil: varian terbaik Sharpe 6.95 di holdout — tapi belum memodelkan biaya transaksi & ARA/ARB. |
| `ara_arb_simulation.py` | riset | Mensimulasikan aturan Auto Rejection Atas/Bawah IDX terhadap strategi pemenang di atas — entry di hari ARA dibuang, exit yang macet di ARB digeser maju ke hari berikutnya yang tidak macet. |
| `ddqn_entry_exit.py` | riset | Agen Double DQN yang belajar kapan masuk *dan* keluar posisi sekaligus (bukan model entry + aturan exit terpisah), dengan realisme ARA/ARB, biaya transaksi, dan reward shaping loss-aversion. |
| `kelly_sizing.py` | dorman | Rumus ukuran posisi Kelly Criterion (dengan fraction cap / "half Kelly"). Formula sudah teruji tapi sengaja belum dipakai — nunggu sampai Layer 1 benar-benar punya edge tervalidasi (Sharpe > 1.5). |

### 4. Evaluasi, laporan & tes

Mengukur sinyal produksi terhadap kenyataan, merangkum semuanya jadi laporan harian, dan menjaga logika inti tidak bocor data.

| File | Status | Fungsi |
|---|---|---|
| `evaluate_signals.py` | live | Mengukur performa sinyal Telegram bot yang sesungguhnya: bandingkan saham yang di-flag vs sisa universe pada hari yang sama, dengan entry di close H+1 (bukan H, supaya tidak look-ahead). |
| `run_ml_reports.py` | live | Orkestrator laporan harian: menjalankan `walk_forward_backtest`, `strategy_variants`, dan `ddqn_entry_exit` atas `neobdm.db` saat ini, lalu kirim ringkasan ke Telegram + tabel lengkap ke GitHub Actions job summary. Read-only. |
| `test_pipeline.py` | tes | Tes regresi ringan (assert-based, tanpa framework) untuk `walk_forward_backtest.py` dan `kelly_sizing.py` — fokus khusus mendeteksi kebocoran data (leakage) dan kebenaran formula. |

### 5. Otomasi & konfigurasi

Lima GitHub Actions workflow yang menjalankan file-file di atas secara terjadwal, plus file konfigurasi pendukung.

| Workflow | Jadwal (UTC) | Menjalankan |
|---|---|---|
| `daily-scrape.yml` | `0 23 * * *` | `neobdm_scraper.py --now` |
| `price-history-topup.yml` | `30 0 * * *` | `backfill_inventory.py` |
| `capture-health.yml` | `15 1 * * *` | `check_capture_health.py --telegram` |
| `ml-daily-report.yml` | `0 13 * * *` | `run_ml_reports.py` |
| `signal-eval.yml` | `0 2 * * 0` (mingguan) | `evaluate_signals.py --telegram` |

| File | Fungsi |
|---|---|
| `requirements.txt` | Daftar dependensi Python: playwright, requests, schedule, pytz, pandas, numpy, xgboost, shap, torch. |
| `.claude/launch.json` | Konfigurasi launch lokal untuk menjalankan scraper sebagai scheduler atau sekali jalan (`--now`). |
| `neobdm.db` | Database SQLite hasil scrape: tabel `market_summary_daily`, `broker_flow`, `price_history`, `konglo_signal_watch`, dst. Sumber data untuk semua file di atas. |
