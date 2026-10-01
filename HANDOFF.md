# DailyScraper — Handoff Brief

Konteks untuk sesi Claude Code berikutnya. Repo: `github.com/0xFridayy/DailyScraper`.
Semua temuan di bawah diverifikasi langsung terhadap `neobdm.db` (11.223 baris
price_history, 218.988 baris broker_flow, 45 ticker, 2025-08-01 → 2026-08-19).

**Baca ini sebelum menjalankan backtest apa pun.** Semua angka Sharpe yang
tercatat di docstring repo dibangun di atas data yang rusak dan formula yang
salah. Dua-duanya dijelaskan di bawah.

---

## TEMUAN 1 — price_history rusak: ticker cross-contamination (BLOCKER)

Baris OHLCV identik (open, high, low, close, volume persis sama) tersimpan
di bawah ticker berbeda pada tanggal yang sama.

```
SUSPECT: 1.400 / 11.223 baris (12,5%) — 231 tanggal, 22 ticker
  cross_ticker_dup   1.352
  series_break         107
  limit_violation       91

Rusak hampir total:
  COIN 93,5% | CDIA 92,0% | DOOH 92,0% | ELTY 91,2% | RSGK 58,4% | KIOS 56,6%

Rusak sesekali:
  BREN 15,5% | SINI 11,6% | MDIA/JARR/PGUN/BUVA/TEBE/FAST/PSKT ~5%
```

CDIA ≡ COIN identik di **94% tanggal**. DOOH ≡ ELTY identik di **88% tanggal**.
Contoh: ELTY tercatat 36 → 360 → 42 dalam tiga hari (harga asli ~40).
Return harian maksimum di dataset: **+21.089%**.

### Penyebab (hipotesis kuat, perlu dikonfirmasi)

`backfill_inventory.py::scrape_ticker()`:

```python
page.keyboard.type(ticker)
page.wait_for_timeout(1500)
page.keyboard.press("Enter")     # ← memilih opsi TERSOROT, belum tentu yang diketik
...
page.click("#submit-button")
page.wait_for_timeout(15000)     # ← timeout tetap, bukan menunggu kondisi
return page.evaluate(EXTRACT_JS)
```

> **KOREKSI 2026-08-21.** Paragraf di bawah ini keliru — lihat Lampiran G.
> Rerun tanpa perubahan kode apa pun menyembuhkan CDIA dan COIN dari 231 baris
> rusak menjadi 0. Kalau ini salah-pilih deterministik, itu mustahil terjadi.
> Ini memang **race condition**; kemarin race-nya kalah, semalam menang.
> Re-scrape TIDAK otomatis mereproduksi kesalahan yang sama — tapi juga tidak
> otomatis memperbaikinya, jadi tahap 2 tetap perbaikan yang benar. Yang
> berubah adalah alasannya: bukan "re-scrape percuma", melainkan "re-scrape
> adalah undian, dan tahap 2 menghentikan undiannya".

~~Tingkat kontaminasi 92% (bukan ~5%) menunjukkan ini **bukan** race condition
acak, melainkan salah pilih yang konsisten: untuk pasangan ticker yang
sama-sama muncul di hasil filter dropdown, `Enter` selalu mengambil yang
salah.~~ Pola 5% pada BREN/JARR/PGUN kemungkinan memang race yang berbeda.

Bukti pendukung yang tetap berlaku: dua pasangan terparah **bersebelahan
persis** di urutan scrape alfabetis (CDIA→COIN indeks 9→10, DOOH→ELTY 13→14).
Itu justru konsisten dengan hipotesis chart-basi: ticker ke-N membaca chart
ticker ke-(N−1) kalau render belum selesai.

### Penularan ke broker_flow

`insert_ticker_data()` menurunkan netval dari payload yang sama:

```python
netval = (lot_diff * 100 * close) / 1e9   # close dari price_by_date payload
```

→ **26.461 / 218.988 baris broker_flow (12,1%) punya netval salah.**

Bisa diperbaiki tanpa scrape ulang, karena `lot_diff` tidak terpengaruh:

```
netval_benar = netval_salah × (close_benar / close_salah)
```

Hanya berlaku untuk baris backfill (`bval IS NULL`). Baris live
(`bval IS NOT NULL`, 10.950 baris) di-scrape langsung — jangan disentuh.

---

## TEMUAN 2 — Formula Sharpe salah di seluruh repo

Di `strategy_variants.py::sharpe_from_returns()`,
`ddqn_entry_exit.py::sharpe_from_returns()`, dan
`walk_forward_backtest.py::sharpe_stats()`:

```python
sharpe = returns.mean() / returns.std() * np.sqrt(252)
```

Dua kesalahan:

1. `returns` adalah **return per-trade**, bukan return harian. Faktor √252
   hanya valid untuk observasi harian. Untuk hold 3 hari, faktornya ~√84.
2. Observasinya **overlapping secara cross-sectional** — 45 ticker di hari
   yang sama dihitung sebagai 45 observasi independen, padahal digerakkan
   IHSG yang sama. Ini menggelembungkan n dan menekan std.

Tambahan di `sharpe_stats()`: Sharpe dihitung hanya atas baris yang triggered,
hari tanpa posisi hilang dari denominator.

Ini penjelasan yang jauh lebih sederhana untuk **Sharpe 5,36 / 6,95** di
`strategy_variants.py` daripada "variant-nya bagus". Docstring-nya sudah
mencurigai angkanya tapi menduga penyebabnya biaya transaksi — dugaan itu
masuk akal namun bukan penyebab utamanya.

**Ganti metrik, jangan tambal formula.** Untuk menilai *sinyal* (bukan
portofolio — user secara eksplisit tidak mau portfolio/sizing model):

- **IC** — Spearman rank corr antara prediksi dan realized forward return
- **hit_rate top-decile** — fraksi desil teratas yang positif
- **edge** = `mean(return | top decile) − mean(return | semua)`

`edge` adalah angka yang menjawab "sinyalnya akurat atau tidak", tanpa
asumsi sizing sama sekali.

---

## TEMUAN 3 — Fitur broker flow tidak berkontribusi (perlu diuji ulang)

Sudah terdokumentasi jujur di docstring repo: `momentum_1d` dominan ~3x di
SHAP, `broker_concentration` peringkat 7/8, price-only (0,76) mengalahkan
full set (0,54), MAE lebih buruk dari baseline "tebak nol".

**Tapi semua itu diukur dengan harga yang 12,5%-nya rusak.** Kesimpulan
"tesis broker flow gugur" belum bisa ditarik sampai diuji ulang di data
bersih. Jangan buang tesisnya berdasarkan angka-angka lama.

---

## TEMUAN 4 — Universe sinyal harian ≠ universe training

Sinyal Telegram harian mengeluarkan TUGU, HOPE, INET, AADI, DPUM, ICBP,
TOWR, ARCI. **Tidak satu pun ada di 45 ticker `neobdm.db`.**

Artinya: model ML tidak pernah dilatih pada saham yang sinyal harian
keluarkan, dan tidak ada cara mengukur akurasi sinyal harian karena forward
return-nya tidak pernah tercatat.

---

## FILE YANG SUDAH DIBUAT

Dua file baru, taruh di root repo:

### `price_audit.py` — sudah dijalankan, output di atas berasal dari sini

```
py price_audit.py audit        # laporan saja, tanpa menulis. → price_audit_suspects.csv
py price_audit.py quarantine   # tandai ke tabel price_quarantine, TIDAK menghapus apa pun
py price_audit.py repair corrected.csv   # perbaiki harga + rescale netval
```

Tiga detektor:
- `limit_violation` — gerakan di luar ARA (+35/25/20% bertingkat) atau ARB
  (−15%). Secara fisik mustahil di IDX → **ground truth**. Caveat: corporate
  action (split/reverse split) juga memicu ini, jadi review, jangan auto-delete.
- `cross_ticker_dup` — OHLCV identik di ≥2 ticker pada satu tanggal.
- `series_break` — close melompat >5x atau <0,2x versus rolling median sendiri.

### `horizon_scan.py` — sudah jalan, output belum bermakna (data rusak)

Scan multi-horizon: target h = 1/3/5/10/20 hari, tiga varian target
(`ret_h` close-to-close, `max_h` puncak dalam window, `mdd_h` drawdown
terburuk), empat feature set (price_only / broker_only / broker+cluster /
full). Metrik IC + edge, bukan Sharpe.

Menambahkan **fitur cluster konglomerat** yang selama ini tidak dipakai di
`FEATURES` — netval per grup bandar (Prajogo DX+NI, Bakrie LG+DH, Hengky,
Hapsoro, Hashim, Haji Isam, Sinarmas, Lippo) dinormalisasi terhadap total
|netval|, plus `cl_foreign` dan `cl_max`.

Rasional: `net_flow_total` menjumlahkan semua broker, sehingga pembelian
bandar dan penjualan retail saling meniadakan jadi satu angka. Flow
per-cluster kemungkinan jauh lebih informatif daripada agregat.

---

## URUTAN KERJA

### 1. Bersihkan data (BLOCKER — jangan lewati)

- [x] `py price_audit.py audit`, konfirmasi angkanya cocok dengan brief ini
      → **cocok persis**, lihat Lampiran A
- [x] `py price_audit.py quarantine` → **501 baris ter-quarantine 2026-08-23**
      (KIOS 142, RSGK 135, BREN 39, SINI 29, sisanya ekor). `price_history`
      utuh — tidak ada yang dihapus.
      **Quarantine itu SNAPSHOT, bukan kebenaran permanen.** Baris bisa sembuh
      (Lampiran G: 899 sembuh dalam semalam). Karena `load_clean()` mengutamakan
      tabel ini begitu ada, quarantine yang basi akan membuang baris yang
      sebenarnya sudah bersih — konservatif, tapi memboroskan data.
      **Jalankan ulang setiap kali scrape berhasil menyembuhkan baris**, dan
      pasti setelah tahap 2 selesai.
- [ ] ~~Buang COIN, CDIA, DOOH, ELTY dari universe training (rusak >90%,
      re-fetch tidak sepadan).~~ **DIBATALKAN 2026-08-21 — lihat Lampiran G.**
      Re-fetch ternyata SANGAT sepadan: satu rerun `backfill_inventory.py`
      tanpa perubahan kode apa pun menyembuhkan 899 dari 1.400 baris semalam
      dan tidak merusak satu pun yang baru. CDIA dan COIN turun dari 231 baris
      rusak menjadi **0**; ELTY 228→9, DOOH 230→12. Membuang keempatnya
      sekarang justru membuang data bersih.
- [ ] Yang masih perlu ditangani: **KIOS (142) dan RSGK (135)** — nol
      perubahan setelah rerun, jadi jalur kegagalan yang berbeda. Keduanya
      berbagi OHLCV *beserta volume persis sampai lembar* di 134 tanggal, jadi
      bukan false positive detektor. Kalau tetap membandel setelah tahap 2,
      barulah pertimbangkan membuangnya.
- [ ] **Jangan** pakai `LEFT JOIN price_quarantine ... WHERE IS NULL` sendirian.
      Filter itu perlu tapi **tidak cukup**: `groupby.shift(-h)` tidak tahu ada
      baris yang dibuang, jadi ia menyambung baris bersih terakhir ke baris
      bersih berikutnya dan **mengarang return yang tidak pernah terjadi**
      (terukur: 50 target palsu, di antaranya ELTY +92% dan TEBE +51%; kurtosis
      target 4,4 → 12,1; 7 pelanggaran ARA/ARB tersisa).
      Pakai `price_audit.clean_panel()` — filter + gap guard sekaligus:

      ```python
      from price_audit import clean_panel
      px = clean_panel(conn, horizons=(1, 3, 5), lags=(1, 5), extremes=True)
      # -> fwd_1/fwd_3/fwd_5, lag_1/lag_5, max_h/mdd_h; semua NaN kalau
      #    jendelanya melompati baris yang dibuang
      ```

      Sudah dipasang di `horizon_scan.py`. Masih perlu dipasang di
      `walk_forward_backtest.build_panel()` dan
      `ddqn_entry_exit.build_episode_frame()`.
      Regresi: 3 tes di `test_pipeline.py` mengunci perilaku ini.
- [ ] Pertimbangkan menjalankan `py backfill_inventory.py` beberapa kali
      **setelah tahap 2 selesai**. Karena `insert_ticker_data()` menulis
      `price_history` dengan `INSERT OR REPLACE` untuk seluruh rentang chart,
      tiap rerun menimpa ulang sejarah — dengan scraper yang sudah benar, itu
      jalur pembersihan paling murah, jauh lebih baik daripada membuang ticker.

### 2. Perbaiki scraper agar tidak berulang

- [x] Ganti `keyboard.type()` + `Enter` dengan pemilihan opsi eksplisit
      → `select_ticker()` mencocokkan teks opsi, mengklik lewat Playwright
      (bukan `el.click()`, karena react-select v1 bereaksi pada `mousedown`),
      lalu memastikan label kontrol benar-benar menampilkan ticker itu
- [x] Ganti `wait_for_timeout(15000)` dengan `wait_for_function`
      → chart di-fingerprint sebelum submit; menunggu fingerprint **berubah**
      lalu **berhenti berubah**. Ini tidak bergantung pada markup judul yang
      tidak bisa kami periksa dari sini, dan langsung menyasar kegagalannya:
      chart ticker sebelumnya masih di layar saat ekstraksi
- [x] Assertion keras di `insert_ticker_data()`
      → `ticker_from_title()`; sengaja bersyarat: judul yang tidak memuat kode
      4 huruf tidak dianggap mismatch (format judul tidak dijamin), tapi kalau
      kodenya ADA dan berbeda, payload ditolak
- [x] Guard ketiga yang tidak butuh pengetahuan halaman sama sekali: tolak
      payload kalau seri OHLCV-nya identik dengan ticker sebelumnya
      (`series_signature()`) — dua saham berbeda tidak mungkin identik
- [x] Gerbang audit di workflow → `price-history-topup.yml` mencatat jumlah
      kontaminasi sebelum & sesudah scrape dan **gagal sebelum commit** kalau
      bertambah. Dipasang di sana, bukan `daily-scrape.yml`, karena yang
      menulis `price_history` adalah `backfill_inventory.py`
      > **KOREKSI 2026-08-26 — lihat Lampiran O.** Gerbang ini semula menghitung
      > **total** ketiga detektor, dan itu **membekukan** `price_history` di
      > 2026-08-21: scraper API yang sudah benar tetap menambah ~1 baris
      > `limit_violation`/`series_break` sah tiap malam (hari ARA +25%, aksi
      > korporasi, geser rolling-median di ujung seri), jadi `AFTER > BEFORE`
      > selalu benar dan commit selalu di-skip. Sekarang gerbang menghitung
      > **`cross_ticker_dup` saja** — satu-satunya detektor yang naik hanya kalau
      > scraper benar-benar regres (OHLCV satu ticker tersimpan atas nama ticker
      > lain). `py price_audit.py count cross_ticker_dup`.
- [x] Selector yang belum terbukti kini di-hedge → `OPTION_SELECTORS` dan
      `VALUE_SELECTORS` menguji beberapa kandidat dalam **satu** predikat JS.
      Bukan probe berurutan: 4 kandidat × 20 dtk × 45 ticker jauh melewati
      batas 35 menit workflow, jadi tebakan pertama yang meleset akan jadi
      timeout, bukan fallback
- [x] Kegagalan massal kini exit non-nol → `should_fail_run()`. Sebelumnya
      `run_backfill()` selalu exit 0, sehingga **kegagalan total tampak
      seperti sukses**: tidak ada ticker ter-scrape → `price_history` tidak
      berubah → gerbang kontaminasi lolos → tidak ada yang di-commit → hijau
- [x] Bukti otomatis saat gagal → screenshot + HTML halaman disimpan pada
      kegagalan **pertama** saja, diunggah sebagai artifact CI
- [ ] **Belum diuji terhadap situs live** — tidak ada kredensial NeoBDM di
      sesi ini. Yang sudah diuji: 16 tes perilaku JS terhadap DOM palsu
      (termasuk skenario chart-basi), `node --check` atas seluruh snippet JS,
      dan 3 tes regresi Python untuk guard-nya. Jalan pertama di produksi
      perlu dilihat; kalau `select_ticker` gagal, kemungkinan besar selector
      `.Select-option` berbeda di halaman itu.

### 3. Ganti metrik

- [x] Ganti `sharpe_from_returns()` di **empat** file → `signal_metrics.py`.
      Ternyata ada **tiga konsumen lagi** yang tidak terdaftar di Temuan 2:
      `feature_ablation.py`, `multiday_features.py`, `smart_money_divergence.py`
      semuanya mengimpor `sharpe_stats`. `check_ml_health.py` yang menangkapnya
      (jumlah modul yang bisa diimpor turun 10 → 8).
- [x] Laporkan **base rate** di samping setiap hit_rate → `format_trade_stats()`
      dan `format_signal_stats()` menolak mencetak hit rate tanpanya.
- [x] `SQRT252_BUDGET` diturunkan 4 → **0**.
- [ ] **Tetapkan bar pengganti `SATISFACTION_SHARPE = 1.5`.** Sengaja tidak saya
      putuskan: 1,5 adalah target yang *kamu* set untuk statistik yang benar,
      jadi penggantinya keputusanmu. Laporan sekarang tidak menyatakan pemenang.
- [ ] Tandai semua angka Sharpe di docstring repo sebagai **VOID** —
      jangan dihapus, beri catatan kenapa (dua alasan di Temuan 1 & 2)

### 4. Uji ulang tesis broker flow

- [ ] `py horizon_scan.py` di data bersih
- [ ] Baca: apakah `broker_only` / `broker+cluster` menunjukkan edge positif
      di horizon manapun? Apakah target `max_h` lebih baik dari `ret_h`?
- [ ] Kalau broker flow tetap nol di semua horizon **di data bersih**, barulah
      tesisnya betul-betul gugur

### 5. Catat sinyal harian — PRASYARAT, bukan pekerjaan paralel

> **Reprioritisasi (lihat Lampiran C).** Semula ditulis "paralel dengan di
> atas". Verifikasi menunjukkan hanya **19 dari 198** ticker
> `market_summary_daily` yang pernah ada di universe latih (**9,6%**), dan 3
> di antaranya justru yang rusak. Model sebagus apa pun di 45 nama konglo
> tidak punya jalur ke tempat sinyal harian menembak. Tahap 4 tanpa tahap 5
> paling banter menjawab pertanyaan akademis tentang saham yang bukan tempat
> kamu trading. Kerjakan tahap 5 **sebelum atau bersamaan** dengan tahap 4,
> jangan sesudahnya.


- [ ] Tabel baru `daily_signals(date, ticker, source, rank, dn0, dn3, price)`
- [ ] Parse output Telegram harian (Top 2 Akum Bandar, Dashboard EOD,
      Broker Stalker) ke tabel itu
- [ ] Job evaluator: untuk setiap sinyal, hitung forward return T+3/T+5/T+10
      begitu harganya tersedia
- [ ] Tambahkan ticker sinyal harian ke `TRACKED_TICKERS` supaya harganya
      ikut ter-capture
- [ ] **Hati-hati saat men-join dua tabel ini.** `market_summary_daily.date`
      adalah tanggal SCRAPE, bukan tanggal data — lihat Lampiran E. Begitu
      ticker sinyal masuk `price_history`, join naif berdasarkan `date` akan
      meleset satu hari. `check_signal_integrity.py` menegaskan offset ini
      supaya perubahannya gagal keras, bukan diam-diam menggeser semua label.

Ini yang paling langsung menjawab pertanyaan sebenarnya — **apakah sinyal
harian ini akurat** — dan dalam 2–3 bulan sudah punya jawaban empiris.

---

## JANGAN DIKERJAKAN DULU

- **DDQN.** `ddqn_entry_exit.py` overfit berat (search Sharpe 3,37 →
  holdout −1,90), dilatih di data rusak, dengan single seed (`seed=0`),
  tanpa model selection, ~277k gradient step untuk ~7k transisi unik.
  Parkir sampai Layer 1 menunjukkan edge nyata di data bersih. Kalau
  nanti dijalankan lagi: rata-ratakan 5–10 seed, tambahkan validation
  split di dalam search period untuk memilih checkpoint, turunkan epoch.
- **Portfolio / sizing model.** User eksplisit tidak mau. `kelly_sizing.py`
  tetap inert.
- **Intraday.** Semua data resolusi harian. Tidak ada tick, bid/ask, atau
  bar menit. Butuh sumber data yang sama sekali berbeda. Yang realistis
  dari data ini: swing 3–20 hari.

---

## CATATAN LAIN

- Lebar stop realistis (median worst-drawdown dalam window, dari data —
  perlu dihitung ulang setelah dibersihkan): h=3d ~−4,3%, h=5d ~−5,7%,
  h=10d ~−8,6%, h=20d ~−13,5%. Berguna untuk kalibrasi trailing stop.
- `ddqn_entry_exit.py` mengecualikan `at_ara` dari state dengan alasan
  "lookahead" — itu keliru. Pada penutupan hari t, kunci ARA hari itu sudah
  diketahui. Mengecualikannya konservatif, bukan wajib.
- `LOSS_AVERSION = 1.5` membuat Q-value bukan lagi ekspektasi P&L. Tidak
  salah, tapi `margin` di trade log tidak bisa dibaca sebagai perkiraan untung.
- Windows: pakai `py`, bukan `python` (konflik Microsoft Store stub).
- Kualitas docstring repo ini di atas rata-rata — hasil negatif dicatat
  jujur, lead yang gugur direkam supaya tidak diulang. Pertahankan
  kebiasaan itu saat menulis hasil run yang baru.

---

# LAMPIRAN — VERIFIKASI 2026-08-20

Dijalankan terhadap `neobdm.db` yang sama. Semua angka di bawah reproducible.

## A. Temuan brief ini terkonfirmasi

`py price_audit.py audit` mereproduksi angka brief **persis**: 1.400/11.223
baris suspect (12,5%), 91 `limit_violation` / 1.352 `cross_ticker_dup` / 107
`series_break`, 26.461/218.988 baris broker_flow (12,1%).

Mekanisme bug scraper juga terkonfirmasi lewat pola baru: **dua pasangan
terparah bersebelahan persis di urutan scrape alfabetis** —
CDIA→COIN (indeks 9→10, 231 tanggal identik) dan DOOH→ELTY (13→14, 221
tanggal). Ini persis pola "chart ticker sebelumnya belum re-render". KIOS+RSGK
(jarak 11) tidak cocok pola itu dan kolisinya mulai tepat 2025-09-01, hari
pertama RSGK masuk universe — kemungkinan jalur kegagalan kedua. `wait_for_function`
yang memverifikasi identitas chart menutup keduanya.

## B. Kelayakan universe untuk training

**Bisa diperbaiki pembersihan.** Distribusi target next-day:

| | mean | std | skew | kurtosis | max |
|---|---|---|---|---|---|
| tanpa filter | +14,9% | 379% | +36,5 | +1568 | +21.190% |
| filter quarantine saja | +0,35% | 6,14% | +1,20 | +12,1 | +91,7% |
| + gap guard | +0,32% | 5,94% | +0,9 | **+4,4** | **+34,9%** |

Setelah gap guard, pelanggaran ARA/ARB di target = **0**.

**Tidak bisa diperbaiki pembersihan:**

1. **Sampel efektif ~11x lebih kecil dari nominal.** Korelasi pairwise
   rata-rata antar-ticker di data bersih **+0,275** (dalam grup konglomerat
   +0,399; RAJA+RATU 0,83, JARR+TEBE 0,81, BRPT+PTRO 0,77). Itu setara
   **~3,4 ticker independen** dari 39 → **~850 baris independen**, bukan 9.728.
   Catatan penting: di data rusak korelasi terukur hanya +0,112 — kontaminasi
   *menyamarkan* ketergantungan ini. Jadi kondisi sebenarnya lebih ketat
   daripada yang terlihat sebelum dibersihkan, bukan lebih longgar.

2. **`hit_rate` yang dilaporkan repo = base rate universe, selisih 0,00 pp.**

   ```
   base rate target positif (tanpa model apa pun) : 42,8%
   hit_rate model di docstring repo               : 42,8%
   ```

   Model tidak menambah informasi arah sama sekali. Sharpe positif yang
   tercatat murni berasal dari pemenang lebih besar dari pecundang.
   Sebagai pembanding, desil momentum teratas mencapai 52,4% positif — sinyal
   arah *ada* di data, modelnya yang tidak menangkapnya.

3. **Drift arah +124%/tahun** (mean target harian +0,32%). Strategi long-only
   apa pun terlihat bagus di periode ini. Setiap backtest wajib dibandingkan
   terhadap baseline long-only, bukan terhadap nol.

4. **95% broker_flow hanya punya `netval`.** Baris live (`bval` terisi) cuma
   10.950/218.988, mulai 2026-07-05 (~30 hari bursa). Fitur apa pun yang
   butuh pemisahan beli/jual praktis tidak punya data.

**Implikasi untuk fitur cluster di `horizon_scan.py`:** dengan korelasi
dalam-grup +0,399, "grup ini bergerak" hampir sama artinya dengan "pasarnya
bergerak". Baca hasil cluster dengan diskon itu.

## C. Universe latih vs universe sinyal

| | |
|---|---|
| `market_summary_daily` (tempat sinyal harian menembak) | 198 ticker |
| irisan dengan 45 ticker latih | **19 (9,6%)** |
| di antaranya yang rusak berat | CDIA, COIN, ELTY |

Ini alasan tahap 5 dinaikkan jadi prasyarat.

## D. Status setelah sesi ini

- `price_audit.py` — ditambah `clean_panel()` / `load_clean()` /
  `add_forward_returns()` / `add_lagged_returns()`; `DB_PATH` jadi absolut
  supaya bisa dipanggil dari mana saja
- `horizon_scan.py` — sudah memakai `clean_panel()`; `build()` terverifikasi
  jalan (9.234 baris, 249 tanggal; `ret_1` dalam [−15,0%, +34,9%] = tepat di
  dalam pita ARB/ARA)
- `test_pipeline.py` — 3 tes regresi gap guard; 11/11 lulus
- **Belum dikerjakan**: quarantine belum dijalankan ke DB (menulis tabel baru
  ke `neobdm.db` yang di-commit harian oleh workflow — jalankan lokal, jangan
  lewat PR), scraper belum diperbaiki, formula Sharpe belum diganti,
  `walk_forward_backtest.py` / `ddqn_entry_exit.py` belum pakai `clean_panel()`

## E. `market_summary_daily.date` adalah tanggal SCRAPE, bukan tanggal data

Ditemukan saat membangun `check_signal_integrity.py`. Scrape berjalan 23:00 UTC
= 07:00 WIB **sebelum pasar buka**, jadi data screener paling segar adalah close
sesi sebelumnya.

```
market_summary_daily.date - 1 hari  vs price_history.date : 34/37 cocok PERSIS (median deviasi 0,00%)
market_summary_daily.date           vs price_history.date :  4/36 cocok        (median deviasi 2,19%)
```

Ketiga sisa yang tidak cocok setelah geser 1 hari **semuanya** ticker yang
memang terkontaminasi (COIN, ELTY) — jadi offset itu memang alignment-nya, dan
sisanya adalah cacat sungguhan. Kolom `last_date`, yang semestinya membawa
tanggal data, **NULL di semua baris**.

Saat ini belum ada kode yang men-join kedua tabel, jadi ini **bukan bug aktif** —
`evaluate_signals.py` hanya memakai `market_summary_daily` secara konsisten, dan
horizonnya tetap benar karena tiap tanggal scrape memetakan 1:1 ke satu hari
bursa. Ini ranjau untuk tahap 5. `EXPECTED_DATE_OFFSET` di
`check_signal_integrity.py` menegaskannya, dan checker itu menurunkan ulang
offset-nya tiap hari — kalau NeoBDM mengubah waktu publikasi atau jadwal
workflow bergeser, ia gagal dengan pesan sendiri alih-alih menyamar jadi
kontaminasi massal.

## F. Dua checker otomatis

| | `check_signal_integrity.py` | `check_ml_health.py` |
|---|---|---|
| menjaga | **data** hasil scrape | **kode** yang mengonsumsinya |
| jadwal | harian 01:45 UTC | harian 12:30 UTC + tiap push & PR |
| gagal kalau | kontaminasi baru, dua jalur scrape tidak sepakat, offset tanggal berubah, kolom regresi | modul tidak import, tes gagal, panel kolaps, cacat melebihi budget |

`check_ml_health.py` memakai **budget cacat**: `SQRT252_BUDGET = 4` dan
`IMPOSSIBLE_TARGET_BUDGET = 88` di-pin ke kondisi sekarang. Cacat yang sudah
diketahui dilaporkan sebagai peringatan; build hanya gagal kalau jumlahnya
**bertambah**. Turunkan angkanya sambil tahap 1 dan 3 dikerjakan — keduanya
seharusnya berakhir di 0. Ini disengaja: checker yang merah permanen melatih
orang mengabaikannya, sementara budget tetap bisa menangkap regresi.

## G. Kontaminasi sembuh sendiri semalam — ini race, bukan salah-pilih tetap

Diukur 2026-08-21, membandingkan `neobdm.db` sebelum dan sesudah satu jalannya
`price-history-topup.yml` (yang menjalankan `backfill_inventory.py` **tanpa
perubahan kode apa pun**):

| | 20 Agu | 21 Agu |
|---|---|---|
| baris suspect | 1.400 (12,5%) | **501 (4,4%)** |
| sembuh (rusak→benar) | — | **899** |
| baru rusak (benar→rusak) | — | **0** |

Per ticker: **CDIA 231→0**, **COIN 231→0**, ELTY 228→9, DOOH 230→12.
Yang **nol perubahan**: KIOS 142, RSGK 135, BREN 39, SINI 29, dan seluruh
kelompok ~5%.

### Ini benar-benar sembuh, bukan sekadar berhenti bertabrakan

Kekhawatiran yang wajar: `cross_ticker_dup` berhenti menyala bisa saja berarti
backfill menulis nilai salah yang *berbeda*, bukan nilai yang benar. Diuji
terhadap arbiter independen (`market_summary_daily`, jalur scrape terpisah,
offset 1 hari per Lampiran E): **5 dari 5** baris yang kemarin rusak dan bisa
diverifikasi kini cocok persis dengan screener. Yang paling telak:

```
ELTY 2026-08-18   kemarin inventory=326 (harga DOOH)   hari ini inventory=39 = screener  ✓
```

Cek silang keseluruhan naik ke **56/56 (100%)**, dari 37 pasangan/92% sehari
sebelumnya.

### Kenapa rerun bisa menyembuhkan

`insert_ticker_data()` menulis `price_history` dengan `INSERT OR REPLACE` untuk
**seluruh rentang chart**, bukan hanya hari terakhir. Jadi tiap kali
`backfill_inventory.py` jalan, ia menimpa ulang berbulan-bulan sejarah. Dengan
scraper yang masih rusak itu berarti undian tiap malam; dengan scraper yang
sudah diperbaiki (tahap 2), itu menjadi mekanisme pembersihan yang efektif dan
gratis — jauh lebih baik daripada membuang ticker dari universe.

### Konsekuensi

1. Tahap 1 tidak lagi perlu membuang CDIA/COIN/DOOH/ELTY — sudah bersih.
2. Tahap 2 naik prioritas: selama race-nya masih ada, tiap malam bisa merusak
   ulang apa yang semalam sembuh.
3. `IMPOSSIBLE_TARGET_BUDGET` di `check_ml_health.py` diturunkan 88 → 82.
   Turunkan lagi tiap kali bisa; ada catatan ratchet di file itu.
4. KIOS/RSGK yang nol perubahan menguatkan dugaan jalur kegagalan kedua —
   kolisinya juga mulai tepat 2025-09-01, hari pertama RSGK masuk universe.

## H. NeoBDM punya UI kedua: `/inventory-chart/`

Screenshot 2026-08-21 menunjukkan halaman `neobdm.tech/inventory-chart/` dengan
desain berbeda total dari yang di-scrape:

| | `/inventory/` (dipakai scraper) | `/inventory-chart/` (baru) |
|---|---|---|
| pilih ticker | dropdown react-select | input teks biasa `Search ticker...` |
| rentang tanggal | `DateRangePicker` (Start/End Date) | tombol preset 2W/1M/3M/6M/YTD/1Y |
| memuat data | tombol `#submit-button` | tidak ada tombol submit |

**Bukan keadaan darurat.** `/inventory/` masih hidup dan masih memberi data
sampai hari ini (dikonfirmasi user), jadi scraper menyasar halaman yang benar.

Dicatat karena dua hal:

1. Ini bukti langsung NeoBDM sedang aktif mendesain ulang area ini — persis
   kondisi yang di-hedge oleh `OPTION_SELECTORS`.
2. Kalau suatu saat `/inventory/` dialihkan ke UI baru, yang dibutuhkan adalah
   **penulisan ulang**, bukan tambal selector. Tidak ada tombol submit berarti
   alur "pilih ticker → klik submit → tunggu chart" tidak berlaku lagi;
   kemungkinan besar chart dimuat reaktif begitu ticker dipilih. Kalau gagal
   nanti, diagnosis harus mulai dari sini, bukan dari daftar selector.

Checker selector berkala sempat dipertimbangkan dan **ditolak user** — NeoBDM
jarang mengubah hal yang merusak, dan kegagalannya sekarang sudah keras
(exit non-nol + artifact). Kalau frekuensi perubahan naik, ini yang pertama
perlu ditinjau ulang.

## I. Mekanisme kontaminasi TERBUKTI — run #15, 2026-08-22

Run pertama `price-history-topup.yml` dengan scraper hasil perbaikan
(PR #18 + #19) **gagal, dan gagal persis seperti yang dirancang.**

```
=== DEWA ===  FAILED: chart is showing CUAN but we asked for DEWA
=== DOOH ===  FAILED: chart is showing DEWA but we asked for DOOH
=== ELTY ===  FAILED: chart is showing DOOH but we asked for ELTY
```

**Chart tertinggal tepat satu permintaan.** BBHI — pertama di daftar alfabetis,
jadi tidak punya pendahulu — satu-satunya yang berhasil di awal. 40 dari 45
ticker ditolak dengan pola ini.

Ini menaikkan Temuan 1 dari **inferensi menjadi observasi langsung**. Dugaan
"ticker ke-N membaca chart ticker ke-(N−1)" yang dulu ditarik dari baris
duplikat kini terlihat apa adanya di log.

### Guard-nya bekerja

| | |
|---|---|
| baris terkontaminasi dicegah | ~8.000 (40 ticker × ~200 baris) |
| `should_fail_run` | memicu exit 1 di 40/45 = 89% |
| langkah commit | **di-skip** — tidak ada yang masuk master |
| `CONTAM_BEFORE` | 501, tidak berubah |

### Kenapa wait di PR #18 tidak menangkapnya

`CHART_CHANGED_JS` menunggu "fingerprint berbeda dari sebelumnya". Syarat itu
**terlalu lemah**: chart memang berubah — hanya saja ke ticker yang salah.
Setelah menolak ticker N−1, blok `except` membaca ulang fingerprint, lalu pada
ticker N chart bergerak dari N−2 ke N−1, fingerprint berbeda, wait lolos.
Siklusnya menopang dirinya sendiri.

Akar masalah: propagasi state Dash. `#submit-button` memicu callback yang
membaca nilai dropdown sebagai State, dan klik mendarat sebelum pilihan baru
sampai ke store Dash — jadi callback merender nilai sebelumnya.
`VALUE_SETTLED_JS` memastikan label DOM sudah berubah, yang ternyata hal
berbeda dari Dash sudah mencatatnya.

### Perbaikan

Wait sekarang mengecek **identitas** chart, bukan sekadar perubahannya, dan
klik submit diulang (maks 3x) kalau chart kembali salah. Ini bisa dilakukan
karena run #15 membuktikan judul chart selalu membawa kode 4 huruf —
`ticker_from_title()` dulu dibuat bersyarat justru karena format judul belum
bisa diverifikasi tanpa akses situs. Sekarang sudah.

### Dua hal untuk diawasi di run #16

1. **VIVA, VKTR, WIFI timeout berbeda** — `wait_for_function: Timeout 60000ms`,
   chart berhenti berubah sama sekali, bukan menampilkan yang salah. Ketiganya
   di ujung run 45 ticker, dan `neobdm_scraper.py` mencatat NeoBDM memicu
   anti-abuse di sekitar 50 permintaan cepat. Kemungkinan rate limiting.
   Sengaja belum disentuh: kegagalannya sudah keras, dan satu perubahan pada
   satu waktu lebih mudah diatribusikan.
2. **Lantai rentang tanggal bergeser** — ticker yang berhasil melaporkan
   `2025-09-01 to 2026-08-21`, bukan `2025-08-01` yang dulu dicapai backfill.
   Konsisten dengan jendela bergulir ~12 bulan yang docstring
   `backfill_inventory.py` tandai belum terverifikasi. Artinya lantai historis
   merayap maju dan baris lama tidak pernah disegarkan.

## J. Run #16 — teori retry gugur, dan kita ternyata buta

Run #16 (2026-08-23), pertama dengan wait-identitas dari PR #21,
**di-cancel di batas 45 menit.** Kegagalan ketiga berturut-turut, dan
masing-masing memakan satu hari penuh untuk mempelajari satu fakta.

### Kita buta

Log-nya **tidak memuat satu pun baris `=== TICKER ===`** — kosong antara
`02:09:37 Login successful!` dan `02:52:40 The operation was canceled`.

Penyebabnya: stdout Python di-buffer blok saat bukan TTY. Run #15 sempat
ter-flush waktu keluar — cirinya, semua baris ticker-nya bertimestamp sama
(`02:05:15`). Run #16 dibunuh sebelum flush, jadi **seluruh output hilang.**
Setiap timeout berikutnya akan sama tidak terdiagnosisnya.

Sudah diperbaiki: `PYTHONUNBUFFERED: "1"` di `price-history-topup.yml`.

### Retry submit tidak bekerja

Dari aritmetika waktu:

| | run #15 | run #16 |
|---|---|---|
| durasi scrape | ~6 menit | **43 menit** (kena batas) |
| per ticker | ~8 dtk | ~61 dtk |

61 dtk ≈ `SUBMIT_SETTLE_PAUSE 0,7 + 3 × CHART_ATTEMPT_TIMEOUT 20`. Artinya
**hampir setiap ticker menghabiskan ketiga percobaan.** Kalau penyebabnya
sekadar propagasi state Dash yang lambat, percobaan kedua pasti berhasil.
Klik submit ulang memakai nilai basi yang sama.

**Diagnosis di balik PR #21 salah.** Dicatat supaya tidak diulang.

### Hipotesis yang tersisa — sengaja BELUM diperbaiki

react-select v1 menyimpan teks ketikan dan nilai terpilih di elemen berbeda.
Kalau `VALUE_SETTLED_JS` mencocokkan **teks ketikan** alih-alih **nilai
terpilih**, maka konfirmasi "sudah terpilih" itu false positive: submit jalan
dengan nilai lama, chart merender ticker lama, lalu klik opsi mendarat
sesudahnya — persis menghasilkan pola tertinggal satu.

Cocok dengan semua bukti. Tapi ini hipotesis ketiga dalam tiga hari, dan dua
sebelumnya baru terbantah setelah membakar satu hari masing-masing.
Hambatannya bukan kekurangan ide, melainkan **latensi observasi**.

Jadi yang dikerjakan adalah alat observasi, bukan tebakan ketiga:

- `DIAGNOSE_JS` membuang teks tiap selector kandidat + judul chart
- `select_ticker()` mencetak selector mana yang memuaskan `VALUE_SETTLED_JS`
  dan teks apa yang dikandungnya — **inilah datum yang menentukan**
- `scrape_ticker()` mencetak satu baris per percobaan submit
- Input `tickers` di `workflow_dispatch`: 3 ticker selesai ~2 menit

### Langkah berikutnya

Jalankan `price-history-topup.yml` manual dengan `tickers: BBHI BNBR BREN`.
BBHI berhasil di run #15, BNBR gagal menampilkan BBHI — jadi tiga ticker itu
mereproduksi bug-nya. Baris `selected via ...` untuk BNBR menentukan perbaikan
berikutnya, dalam hitungan menit, bukan sehari.

## K. Dua alert diperbaiki supaya berhenti menyerigala — 2026-08-23

### Alert kontaminasi menyalahkan hal yang salah

Pagi 2026-08-23 `check_signal_integrity.py` melaporkan baris tanggal 08-06
sampai 08-13 sebagai *"the scraper's ticker-selection defect is writing bad rows
again"* — padahal `price_history` **beku di 08-20** karena topup gagal sejak
run #15. Scraper tidak menulis apa pun. Itu true positive soal kondisi data,
tapi false alarm soal penyebabnya.

Akar masalahnya: kode menyamakan **"belum di-quarantine"** dengan **"baru
ditulis"**. Sekarang dibedakan tiga kondisi:

| kondisi | pelaporan |
|---|---|
| tabel `price_quarantine` belum ada | peringatan + perintah persisnya |
| scrape tidak maju (`price_history` basi) | peringatan: **backlog**, bukan kerusakan baru |
| scrape maju tapi tetap ada suspect | **gagal** — cacatnya memang aktif lagi |

### Sinyal tak terukur diberi ambang, bukan dihapus

`1 of 13 signalled ticker(s) ... have no captured close (TPIA)` dulu gagal
keras. TPIA ada di `price_history` tapi hari itu tidak lolos filter likuiditas
panel screener — churn struktural, bukan bug. Diukur atas 115 sinyal:

```
2026-08-16..08-21  0%      2026-08-22  8%      2026-08-23  8%
total 8/115 = 7,0%
```

(08-12 dan 08-13 tercatat 100% hanya karena `market_summary_daily` baru mulai
terisi 08-16.)

Gagal karena satu nama akan membuat checker merah hampir tiap pagi untuk hal
yang tidak bisa ditindaklanjuti siapa pun. Ambang `MAX_UNMEASURABLE_SIGNALS =
0.30` tetap menangkap panel yang menyusut atau sumber sinyal yang melenceng
dari universe.

### Prinsipnya

Sama dengan budget cacat di `check_ml_health.py`: **checker yang merah permanen
untuk kondisi yang sudah diketahui melatih orang mengabaikannya.** Yang dijaga
adalah kemampuan mendeteksi perubahan, bukan jumlah alarm.

Status sesudahnya: `signal integrity OK` dengan dua peringatan jujur —
kontaminasi 0/445 di jendela, dan TPIA sebagai churn normal.

## L. Tahap 3 selesai — pembacaan jujur pertama, 2026-08-23

Keempat situs `sqrt(252)` dihapus, diganti `signal_metrics.py`. Nol tersisa
(dijaga tes AST di `test_pipeline.py`).

### Hasilnya

Panel yang sama, 249 hari:

```
IC                       -0,025      (tak bisa dibedakan dari nol)
top-desil hit            43,5%  vs base 42,3%   → edge +1,2 pp
aturan ambang (>0,5%)    42,4%  vs base 42,3%   → edge +0,1 pp
```

**Model tidak menambah informasi arah.** Persis seperti yang diprediksi temuan
base-rate di Lampiran B. Sekarang angkanya terlihat langsung di laporan, bukan
tersembunyi di balik Sharpe yang salah.

### Metrik berbasis rata-rata BELUM bisa dipakai

Run yang sama melaporkan `return +97,79% vs +15,80%`. Itu bukan hasil — itu
kontaminasi:

```
mean target panel        +14,38%
tanpa 82 baris mustahil   +0,32%     ← distorsi 45×
```

82 baris (0,77% panel) menguasai rata-ratanya. Jadi `edge`, `top_mean`,
`mean_ret` **tidak bermakna sampai tahap 1 selesai** (`build_panel()` →
`clean_panel()`).

Ini justru membenarkan pilihan metriknya: IC dan hit_rate berbasis peringkat dan
tanda, jadi kebal terhadap outlier itu; edge berbasis rata-rata hancur. Baca dua
yang pertama sekarang, yang ketiga setelah tahap 1.

### Yang ditemukan sambil jalan

- **`scipy` tidak ada di `requirements.txt`** padahal diimpor `horizon_scan.py`
  dan `smart_money_divergence.py` — instalasi bersih tidak bisa menjalankan
  keduanya. Sudah ditambahkan.
- `signal_metrics.py` sengaja hanya bergantung numpy + pandas (Spearman lewat
  `pandas.rank()` yang merata-ratakan ties) supaya `check_ml_health.py` bisa
  mengimpornya di CI tanpa stack ML.
- Angka Sharpe lama **ditandai VOID di lima docstring**, tidak dihapus — supaya
  tidak diturunkan ulang lalu dipercaya untuk kedua kalinya.


## Lampiran M — TEROBOSAN: /inventory-chart/ punya API JSON (2026-08-23)

`/inventory/` sudah dipensiunkan NeoBDM (halaman notice, lihat Lampiran H/I).
Penggantinya `/inventory-chart/` ** tidak** perlu di-scrape DOM — ia dibangun di
atas API JSON bersih, di `API_BASE` yang SAMA dengan screener
(`https://neobdm.tech/api`). Auth-nya cookie sesi (csrftoken + sessionid) yang
SUDAH ditangani `_api_session(page)` di neobdm_scraper.py:360. GET tidak butuh
CSRF.

Endpoint yang sudah terlihat (via DevTools Network, 2026-08-23):

| endpoint | isi |
|---|---|
| `GET /api/stock-universe` | seluruh universe. Memuat custom universe **`DAILY_SCRAPER_TICKERS`** (id 01a0097c-...) berisi PERSIS 45 ticker yang kita lacak. Helper `get_stock_universe()` di neobdm_scraper.py:387 SUDAH memanggil ini. |
| `GET /api/brokers/inventory` | daftar kode broker ("AD","AF","AG",... "BB","BK","BQ","BR",...). |
| `GET /api/inventory?symbol=ENRG&start_date=YYYY-...` | **DATA UTAMA** — OHLCV harga + net-flow kumulatif per broker + volume (semua yang ada di chart). BENTUK RESPONS BELUM TERLIHAT — ini satu-satunya yang tersisa untuk ditangkap sebelum menulis parser. |

### Implikasi

`backfill_inventory.py` ditulis ULANG sebagai panggilan API murni mengikuti pola
`scrape_market_summary` — Playwright HANYA untuk login (menegakkan cookie sesi),
lalu `req, headers = _api_session(page)` dan GET JSON. TIDAK ada ekstraksi
Plotly, dropdown, tunggu-render, atau race chart-basi. Seluruh kelas bug minggu
ini (run #15-#17: seleksi ticker, chart tertinggal satu, timeout) LENYAP.

### PERINGATAN kritis: VALUE vs Lot

Chart baru punya toggle VALUE / Lot (default Value/Rp). `insert_ticker_data()`
menurunkan netval dari **cum_lot** (bukan Rp) SECARA SENGAJA — string Rp
truncate 2 desimal dan menihilkan diam-diam flow miliaran (lihat docstring
walk_forward_backtest.py). Jadi endpoint HARUS mengembalikan data LOT, bukan
hanya Rp. Konfirmasi ini dari bentuk respons sebelum menulis parser.

### Yang tersisa untuk ditangkap

Response + Request URL lengkap dari `GET /api/inventory?symbol=...&start_date=...`.
Setelah itu scraper baru bisa ditulis penuh tanpa menebak.

## Lampiran N — RESPONS API tertangkap + scraper DITULIS ULANG (2026-08-23)

Bentuk respons sudah dikonfirmasi dari sampel penuh (ENRG, jendela 1Y). `data`:

| key | isi |
|---|---|
| `date[]` | hari bursa, **paralel** dengan `ohlc[]` dan setiap seri `nlot`/dst |
| `blot / slot / nlot` | LOT beli / jual / **NET** per kode broker `{ "AK":[...], ... }` |
| `bval / sval / nval` | nilai beli / jual / net dalam **Rupiah penuh** (5492217500, TIDAK truncate) |
| `ohlc[]` | `{date, open, high, low, close, volume, volume_sma20}` |
| `meta` | `{symbol, brokers, start_date, end_date, investor_type}` |

Dua temuan yang mengubah desain:

1. **`nlot` itu NET HARIAN, bukan kumulatif.** Terverifikasi: `nlot[0]` = `blot[0] − slot[0]` (128896 − 33790 = 95106) dan tandanya bolak-balik hari ke
   hari. Jadi TIDAK perlu diff kumulatif seperti chart Plotly lama — langsung
   `netval = nlot[hari] * 100 * close[hari] / 1e9`.
2. **`nval` presisi penuh** (bukan display truncate), jadi peringatan VALUE/Lot di
   Lampiran M sebetulnya moot untuk JSON ini. **Tetapi** netval tetap diturunkan
   dari LOT — bukan pakai `nval` — supaya (a) patuh aturan "selalu dari lot", dan
   (b) satuannya (miliar) SAMA dengan baris yang sudah ditulis backfill lama,
   sehingga re-fetch MENYEMBUHKAN baris di tempat, bukan mencampur dua konvensi.

`backfill_inventory.py` sudah ditulis ulang: SATU GET terautentikasi per ticker
(login Playwright hanya untuk cookie sesi, lalu `page.context.request.get`).
Seluruh mesin DOM/Plotly (EXTRACT_JS, select_ticker, date-picker, fingerprint,
race chart-basi) DIHAPUS. `netval` lot-derived miliar; `bval/sval/bavg/savg`
dibiarkan NULL persis seperti backfill lama. Workflow `price-history-topup.yml`
disesuaikan (artifact `topup-failure.json`, timeout 45→20).

### Tindak lanjut TERBUKA (belum dikerjakan)

1. **Cakupan broker berubah.** Backfill lama membaca SEMUA broker di chart lalu
   filter ke `BROKER_FLOW_CODES` (~30 kode). API butuh selector; sekarang dipakai
   `INVENTORY_BROKERS = ["TOP_5_NB_LOT_C20","TOP_5_NS_LOT_C20"]` (tata bahasa
   permintaan SITUS SENDIRI — dijamin diterima, tak berisiko 400 yang membakar
   satu run) → 10 penggerak terbesar per lot, difilter ke `BROKER_FLOW_CODES`.
   Untuk ENRG itu 8 dari 10 kode masuk. Tiap run mencetak `returned=` vs `kept=`
   supaya cakupan terlihat. **Keputusan user:** apakah cukup, atau perlebar
   (`TOP_8_*_ALL`), atau uji apakah kode broker eksplisit (`brokers=AK`) diterima
   endpoint (satu tes URL 30 detik) untuk memulihkan semantik kurasi lama.
2. ~~**`get_inventory_bagholders` (neobdm_scraper.py:740) MASIH pakai `/inventory/`
   Plotly yang pensiun** — fitur broker-stalker Telegram (bag-holder) juga rusak,
   perlu migrasi API yang sama. Di luar cakupan rewrite backfill ini.~~
   **SELESAI 2026-08-27 — lihat Lampiran P.**
3. **`bval/sval` kini tersedia dari API** tapi dibiarkan NULL sampai konvensi
   satuan live-vs-backfill direkonsiliasi (live simpan angka page-derived via
   `parse_num`, backfill simpan miliar). Mengisinya jadi perubahan satu baris.

## Lampiran O — gerbang kontaminasi membekukan `price_history` (2026-08-26)

Gejala yang dilaporkan `check_signal_integrity.py` pagi ini:

```
🔴 SIGNAL INTEGRITY FAILED
2026-08-26: 204 panel rows | 15 signals | offset=1d n=75 agree=100%
new contamination in last 10d: 0 of 443 rows
❌ price_history STALE — newest 2026-08-21 (3 weekdays ago)
```

Satu-satunya kegagalan adalah **staleness** — bukan kontaminasi. Cross-source
`agree=100%`, contamination `0 of 443`. Jadi jalur scrape lain (screener,
broker live) segar sampai 08-26; hanya `price_history` yang macet di 08-21.

### Akar masalah — bukan scraper, tapi GERBANG-nya

Scraper API baru (Lampiran N) **berhasil** tiap malam. Log run #19 (08-25) dan
#20 (08-26) sama persis: `Failed tickers: []`, semua 45 ticker menarik data
sampai 08-24. Yang gagal adalah langkah gerbang:

```
cross_ticker_dup? BUKAN — kontaminasi rows: 501 -> 502
##[error]scrape added 1 contaminated rows - not committing
```

Gerbang menghitung **total** ketiga detektor sebelum vs sesudah scrape dan gagal
kalau naik. Tapi scraper yang benar **menulis ulang seluruh jendela 1Y tiap
malam** (`INSERT OR REPLACE`) dan menambah hari bursa baru. Volatilitas IDX yang
sah — hari ARA +25%, ARB −15%, aksi korporasi — memicu `limit_violation`; dan
median bergulir **terpusat** (`center=True`) di `series_break` bergeser di ujung
tiap seri saat hari baru masuk. Salah satunya menambah ~1 baris tiap malam, jadi
`AFTER > BEFORE` **selalu** benar → commit selalu di-skip → `price_history` tidak
pernah maju. `check_signal_integrity` lalu melaporkan staleness-nya. Persis pola
"checker merah permanen" yang berulang di Lampiran F & K, kali ini di sisi
commit, bukan sisi alert.

Bukti bahwa +1 itu **bukan** kontaminasi sungguhan: cross-source setuju 100% atas
75 pasang di offset yang benar — baris baru cocok persis dengan screener yang
di-scrape jalur terpisah. Kalau OHLCV-nya salah-ticker, keduanya tidak mungkin
cocok.

### Perbaikan

Gerbang sekarang menghitung **`cross_ticker_dup` saja**
(`py price_audit.py count cross_ticker_dup`). Alasannya:

- Itu **satu-satunya** detektor yang naik hanya kalau scraper regres: OHLCV satu
  ticker tersimpan atas nama ticker lain. Dua saham IDX berbeda tidak mungkin
  punya open/high/low/close/**volume** byte-identik, jadi scrape yang benar tak
  pernah menaikkannya — hanya menyembuhkan dup lama (angkanya turun).
- `limit_violation` & `series_break` memang detektor triase backlog; doktrin
  modulnya sendiri: **direview, bukan di-auto-act** (aksi korporasi memicunya).
  Memakainya sebagai penghadang commit itu keliru sejak awal.

Guard lain tetap ada dan tidak dilonggarkan: assert `meta.symbol`,
`series_signature` vs ticker sebelumnya di `backfill_inventory.py`, dan
`check_signal_integrity.py` tetap mengecek cross-source + kontaminasi jendela
tiap hari (01:45 UTC). Jadi `limit_violation`/`series_break` tidak jadi tak
terjaga — hanya berhenti memblokir commit.

`price_audit.cmd_count` menerima argumen detektor opsional; default tetap total
untuk pemakaian lain. Dua tes baru di `test_pipeline.py`
(`test_commit_gate_ignores_legitimate_volatility`,
`test_commit_gate_catches_a_recontaminated_scrape`) mengunci: lonjakan +30% sah
menaikkan `limit_violation` tapi **tidak** `cross_ticker_dup`; OHLCV identik di
dua ticker **menaikkannya**.

### Setelah ini di-merge

Sekali workflow `price-history-topup.yml` jalan dengan gerbang baru, ia akan
commit data 08-24/08-25/08-26 yang selama ini ditolak, dan `price_history` maju
lagi. `check_signal_integrity` hijau tanpa perubahan apa pun di checker itu —
kegagalannya benar; yang salah ada di hulu. Bisa juga dipicu manual lewat
`workflow_dispatch` untuk tidak menunggu cron 00:30 UTC berikutnya.

**TERKONFIRMASI 2026-08-26.** Run #21 (dispatch manual, sesudah merge) exit
success, commit `Top up price_history (2026-08-26)` masuk master, dan
`price_history` maju **2026-08-21 → 2026-08-24** (44 baris). Gerbang lolos,
tidak ada kontaminasi baru.

## Lampiran P — bag holder Telegram kosong ("-") sejak `/inventory/` pensiun (2026-08-27)

Gejala yang dilaporkan user: di sinyal harian, blok Broker Stalker menampilkan
angka retail jual dengan benar tapi bag holder-nya selalu kosong:

```
1. SINI | retail jual -36.9  savg: 8759.9
   🎒 Bag holder: -
2. EMAS | retail jual -31    savg: 8354.5
   🎒 Bag holder: -
```

### Akar masalah — sisa dari halaman yang pensiun, bukan data nol

`get_inventory_bagholders()` masih men-scrape `/inventory/` Plotly yang **sudah
dipensiunkan** NeoBDM (Lampiran H/I/M): `page.click("#tick .Select-control")`,
`#submit-button`, lalu baca `.js-plotly-plot`. Selector itu tidak ada lagi, jadi
tiap lookup entah mengembalikan `[]` atau melempar exception yang ditelan
`except` di pemanggilnya jadi `holders = []`. Formatter menutupnya:

```python
bag = ", ".join(...) or "-"     # list kosong → "-"
```

Jadi fitur yang **mati** tampil identik dengan "memang tidak ada akumulator".
Itu sebabnya rusaknya berminggu-minggu tanpa ada yang menyadari.

Ini persis follow-up #2 yang ditulis terbuka di Lampiran N: waktu itu **hanya
`backfill_inventory.py`** yang dimigrasikan ke `/api/inventory`, pemanggil ini
sengaja ditinggal. Bukti bahwa yang rusak cuma jalur ini: `get_netflow()` di
halaman broker (`NEOBDM_BROKER_URL`) masih hidup — angka "retail jual" di sinyal
yang sama tetap benar.

### Perbaikan

Migrasi ke endpoint JSON yang sama seperti backfill: **satu GET terautentikasi
per ticker**, tanpa dropdown, tanpa submit, tanpa tunggu-render, tanpa race
chart-basi. Cookie di-prime **sekali** sebelum loop (dulu: satu `goto` + ~18 dtk
tunggu **per ticker**).

Perhitungannya langsung dari respons:

```
cum_lot = sum(nlot[code])            # nlot = net HARIAN (Lampiran N), jadi dijumlahkan
avg     = sum(nval[code]) / (cum_lot * 100)
```

Dua koreksi semantik yang ikut dibetulkan:

1. **Hanya akumulator.** Kode lama mengurutkan cum desc lalu ambil top-n tanpa
   syarat — di ticker yang semua broker-nya jualan, itu melaporkan **net seller**
   sebagai "bag holder", kebalikan dari istilahnya. Sekarang `cum <= 0` dibuang.
2. **Kegagalan tidak lagi menyamar jadi "-".** `holders_failed` dibawa ke
   formatter: gagal ambil → `⚠️ gagal ambil`, sukses tapi nihil → `tidak ada
   akumulator`. Prinsip yang sama dengan Lampiran F/K, dipasang di sisi tampilan.

### Kenapa fungsinya ada di `price_audit.py`

`bagholders_from_payload()` (murni, tanpa playwright) diparkir di `price_audit.py`
bersama `series_signature`/`should_fail_run`, dengan alasan yang **sudah
didokumentasikan modul itu**: di `neobdm_scraper.py` ia tidak bisa diimpor tanpa
playwright + kredensial, jadi tidak bisa diuji di CI — dan CI justru satu-satunya
tempat regresi ini tertangkap. `ml-health.yml` memang tidak menginstal playwright.
Tiga tes baru mengunci: nlot dijumlahkan (bukan ambil elemen terakhir), net
seller dibuang, dan payload rusak/`None` degrade ke `[]` bukan exception.

### Catatan cakupan broker (KEPUTUSAN TERBUKA)

`BAGHOLDER_BROKERS = ["TOP_5_NB_LOT_C20", "TOP_5_NS_LOT_C20"]` — tata bahasa
permintaan situs sendiri, dijamin diterima (tak berisiko 400 yang membakar run).
**Tapi seleksinya recency-weighted** (20 candle terakhir), sementara bag holder
dimaksudkan ~3 bulan: broker yang akumulasi besar 2 bulan lalu lalu berhenti bisa
terlewat. Melebarkan ke `TOP_8_*_ALL` cuma satu edit, tapi ejaan itu **belum
pernah terlihat dikirim situsnya** — verifikasi dulu (satu tes URL) sebelum
dipakai.

### Belum diuji terhadap situs live

Tidak ada kredensial NeoBDM di sesi ini. Yang sudah diuji: 3 tes unit atas bentuk
respons yang **sudah terkonfirmasi** di Lampiran N, `py_compile`, dan
`check_ml_health` hijau (27 tes). Jalan pertama di produksi perlu dilihat —
kalau gagal, pesannya kini eksplisit di Telegram (`⚠️ gagal ambil`) dan di log,
bukan lagi `-` yang membisu.

## Lampiran Q — alarm kontaminasi salah tuduh lagi, kali ini karena tetangga terkarantina (2026-08-27)

`check_signal_integrity.py` melaporkan:

```
❌ 1 NEW contaminated price_history row(s) in the last 10 scrape days
   ({'limit_violation': 1}) — e.g. 2026-08-24 MDIA. The scrape IS advancing
   (newest 2026-08-26), so the ticker-selection defect is writing bad rows again.
```

### Ini false positive — barisnya justru bersih

Seri MDIA:

```
2026-08-12  close=280      wajar (harga MDIA ~200-280)
2026-08-13  close=95   ⚠️  TERKARANTINA (limit_violation+cross_ticker_dup)
2026-08-14  close=93   ⚠️  TERKARANTINA (cross_ticker_dup)
2026-08-24  close=252      ← baris "tertuduh"
```

Baris 95/93 itu **harga KIOS** yang nyasar — kontaminasi lama, sudah masuk
`price_quarantine` sejak dua minggu sebelumnya. Baris 08-24 (252) sendiri:
nilainya pas di rentang normal MDIA, dan **tidak ada satu pun ticker lain yang
berbagi OHLCV dengannya**. Cross-source hari itu `agree=100%` di 113 pasang.

Pemicunya: `detect()` menghitung `pct_chg` terhadap baris sebelumnya di tabel
**mentah** — yaitu close 93 yang sudah diketahui salah. 93 → 252 = **+171%**,
langsung kena `limit_violation`. Baris bersih dituduh gara-gara tetangganya.

Ini kesalahan yang **persis sama** dengan yang sudah dijaga `add_forward_returns()`
di sisi target ("mengarang return yang tidak pernah terjadi"), hanya saja terjadi
di sisi detektor. Doktrinnya sudah ada di repo; detektornya yang belum memakainya.

Saudara kandung Lampiran O juga: **tiga detektor diperlakukan sama padahal
artinya beda.** Lampiran O di sisi commit, Lampiran Q di sisi alert.

### Perbaikan

`detect(px, trusted=None)` menerima mask opsional. Baris yang **tidak** trusted
(praktisnya: yang sudah terkarantina) tetap dinilai sendiri, tapi **tidak pernah
dipakai sebagai `prev_close`** baris sesudahnya. Baseline-nya **dibuang**, bukan
disambung ke close bersih terakhir — lompatan multi-hari juga tidak bisa dinilai
dengan pita ARA/ARB harian.

`check_new_contamination()` membangun mask itu dari `price_quarantine`.
Default `trusted=None` = perilaku lama, jadi semua pemanggil lain
(`cmd_count`, `load_clean`, gerbang commit) **tidak berubah**.

Terukur di DB yang sama:

| | sebelum | sesudah |
|---|---|---|
| fresh suspect di jendela | 1 (MDIA) | **0** |
| `limit_violation` total | 84 | **32** |
| `cross_ticker_dup` | 466 | **466** (utuh) |
| `series_break` | 100 | **100** (utuh) |

Lebih dari separuh `limit_violation` di seluruh tabel ternyata artefak tetangga
terkarantina, bukan cuma MDIA. Deteksi kontaminasi asli tidak berkurang sedikit
pun — dan itu yang dikunci tes `test_trusted_mask_leaves_real_contamination_detectable`.

### BAHAYA TERBUKA — `workflow_dispatch` di luar jam pra-buka merusak offset tanggal

Ditemukan dengan cara paling mahal: **saya sendiri melakukannya.** Memicu
`daily-scrape.yml` manual pada 2026-08-27 13:09 UTC (**21:09 WIB, sesudah pasar
tutup**) untuk memverifikasi Lampiran P.

Seluruh pipeline mengasumsikan `market_summary_daily.date − 1 = tanggal data`
(Lampiran E), yang hanya benar kalau scrape jalan **sebelum pasar buka** (23:00
UTC = 07:00 WIB). Dijalankan sesudah tutup, screener mengembalikan close **hari
itu sendiri** dan `INSERT OR REPLACE` menimpa baris pagi yang sudah benar:

```
market_summary_daily 2026-08-27   VIVA  PSKT  BRMS  ERAA
  sebelum run manual (benar)        48   220   720   468   = price_history 08-26 ✓
  sesudah run manual (salah)        52   236   765   496   = close 08-27 sendiri ✗
```

Akibatnya `check_signal_integrity` gagal dengan **true positive**:
`the two scrape paths DISAGREE — only 85% of 113`. Checker-nya benar; datanya
yang rusak. 205 baris `market_summary_daily` untuk 08-27 dan
`konglo_signal_watch` 08-27 (25 sinyal, dari yang seharusnya 12) ikut terpengaruh.

**SUDAH DIPERBAIKI — keduanya.**

### 1. Data dipulihkan

`repair_scrape_date.py` (baru) mengembalikan baris berkunci-tanggal-scrape dari
DB yang masih benar. Nilai benarnya ada persis di git, di commit sebelum run
buruk itu:

```
git show 8b454b0:neobdm.db > /tmp/good.db
py repair_scrape_date.py 2026-08-27 /tmp/good.db --apply
```

```
market_summary_daily: live 205 -> restored 204
konglo_signal_watch:  live  25 -> restored  12
```

Idempoten (jalan kedua kali melaporkan "nothing to change"), dan menolak
mengosongkan tabel kalau sumbernya kebetulan kosong. `price_history` dan
`broker_flow` **sengaja tidak** disentuh: keduanya berkunci tanggal bursa asli
dari API, bukan tanggal scrape — justru itu sebabnya ketidaksepakatan kedua
jalur terdeteksi keras.

> **Koreksi (2026-09-30, Lampiran R):** untuk `broker_flow` itu hanya benar bagi
> baris backfill (≤ 2026-07-04, `bval` NULL). Baris live (≥ 2026-07-05, `bval`
> terisi) berkunci **tanggal scrape MYT**, dan dulu tanpa pagar jendela.

Sesudahnya: `🟢 signal integrity OK — offset=1d n=113 agree=100%`, exit 0.

### 2. Pagar dipasang

`price_audit.date_offset_holds(now_local)` menjawab satu pertanyaan: apakah
invarian `scrape date − 1 = tanggal data` berlaku untuk run yang dimulai
sekarang? Hanya berlaku **sebelum pasar buka** — IDX buka 09:00 WIB (UTC+7) =
10:00 di zona waktu UTC+8 yang dipakai penjadwalan repo ini.

`neobdm_scraper._offset_safe()` memakainya untuk **melewati penulisan
`market_summary_daily` dan `konglo_signal_watch`** ketika di luar jendela, dengan
log yang menjelaskan persis kenapa. Override sadar: `NEOBDM_ALLOW_OFF_WINDOW=1`.

Yang **tidak** diubah, dan ini disengaja: scrape-nya tetap jalan dan sinyal
Telegram tetap terkirim. Angka yang ditampilkan benar jam berapa pun — yang
berbahaya cuma **menyimpannya** di bawah tanggal yang dibaca pipeline sebagai
sesi sebelumnya. Jadi `/scrape` on-demand tetap berguna; yang hilang hanya baris
yang memang tidak boleh ditulis. Dikunci tes
`test_date_offset_only_holds_before_the_open`, termasuk kasus 22:09 yang
menyebabkan kerusakan ini.

### Yang masih terbuka

Perbaikan sebenarnya tetap yang ditulis Lampiran E: **scraper mencatat tanggal
data sebenarnya** alih-alih menyimpulkannya dari tanggal scrape. Kolom
`last_date` yang disediakan untuk itu NULL di semua baris. Pagar di atas
mencegah penulisan yang salah, tapi tidak membuat run di luar jendela jadi
berguna untuk panel — itu butuh tanggal data yang otoritatif.

## Lampiran R — capture `broker_flow` live: tabel lengkap, sesi terbukti, tulis semua-atau-tidak-sama-sekali (2026-09-30)

### Masalah (audit baca-saja, `neobdm.db` per 2026-09-29)

- Jalur lama (`get_netflow(strict=False)`) membaca tabel DOM yang dirender:
  `page_size=15`, padahal `props.data` callback berisi ratusan baris (probe live
  XL/AK/IF: 181–612 baris per sisi, diurutkan menurut netval). Ticker di luar
  15 teratas hilang diam-diam; tidak ada catatan bahwa scan terpotong.
- Sel kosong/`-`/`N/A` lewat `parse_num` → 0.0; gagal klik durasi/submit
  diabaikan dan tabelnya tetap dibaca.
- Tanpa pagar jendela: run sore/malam Juli (07-06/07/09/10/13) menyimpan sesi
  hari itu sendiri, bukan sesi sebelumnya; sesi 07-08 tak pernah tertangkap.
- Rerun di tanggal yang sama hanya `INSERT OR REPLACE`: 2026-08-12 = 212 baris
  sesi 08-12 + 90 baris sisa sesi 08-11; 2026-08-27 = 158 + 121 baris sisa.
- Hanya 60 dari 86 tanggal live yang snapshot-nya berbeda (salinan akhir
  pekan/libur/basi).

### Kontrak baru (`method = dash_callback_v1`)

`read_broker_flow_code` = satu muat halaman + **satu** submit per kode (callback
menjawab akum DAN dist). Diterima hanya bila:

- request membuktikan dirinya: `inputs` `duration-picker.value == "Today"` dan
  `foreign-only-checkbox.value == []`, `state` `broker.value == [kode]`,
  `changedPropIds` memuat `submit-button.n_clicks`, `outputs` memuat kedua
  kontainer `.children`;
- respons lolos validasi Broker Stalker (PR #71) untuk kedua sisi — setiap baris,
  bukan hanya ticker yang dilacak;
- Label dbc tiap sisi persis `Stalking Net Buy|Sell from D Mon YYYY to D Mon
  YYYY`, from == to, tanggal akum == dist → `session_date` (bukti sesi dari
  sumber; jam mesin tidak dipakai);
- tidak ada ticker berulang dalam satu sisi dan tidak ada ticker di kedua sisi
  (tidak di-dedupe).

Angka dikonversi ketat: 0 eksplisit dari sumber = nol teramati (dibulatkan
sumber); sel hilang/tidak valid = scan GAGAL, tidak pernah 0.0.

### Persistensi

Semua kode ditangkap dulu, baru diputuskan. `broker_flow` untuk `scrape_date`
diganti HANYA jika semua `BROKER_FLOW_CODES` OK, semua melaporkan
`session_date` yang sama, dan `session_date < scrape_date` (MYT, awal capture);
syarat terakhir ini sudah diperketat menjadi `session_date` == sesi IDX
terakhir sebelum `scrape_date` (lihat "Sesi sumber yang diharapkan" di bawah).
Salinan akhir pekan/libur tetap diizinkan (Minggu membawa sesi Jumat). Bila
lolos: satu transaksi menghapus baris live tanggal itu (`bval IS NOT NULL`;
baris backfill dipertahankan) lalu memasukkan snapshot baru. Bila tidak:
`broker_flow` **tidak disentuh sama sekali**.

Provenance per kode per run di tabel baru `broker_flow_scan` (status OK /
SOURCE_FAILURE, `snapshot` PERSISTED / REJECTED / SUPERSEDED, `session_date`,
jumlah baris, alasan di `detail`). Tidak ada model yang membacanya.

### Pemilihan broker terverifikasi (smoke live 2026-09-30)

Mengetik kode di picker memicu callback `broker.search_value`, dan ~1,1 detik
kemudian callback `debounce-interval.n_intervals` di server bisa **menulis
`broker.value` sendiri**. `add_broker_chip` menekan Enter setelah jeda buta
0,8 detik, jadi bisa berpacu dengan tulisan itu: pada smoke 30 kode, request KI
terkirim dengan `broker.value` ≠ `["KI"]` (ditolak request proof, jadi tidak
ada data salah yang tersimpan). `select_broker_for_flow` (khusus broker_flow;
`add_broker_chip` bersama tidak diubah), maksimal 2 percobaan:

1. kosongkan chip;
2. ketik kode, lalu tunggu (maks 3 detik) sampai menu menampilkan **tepat satu**
   opsi = kode itu dan opsi itu yang fokus — baru Enter;
3. tunggu 1,5 detik (melewati debounce), chip harus tepat `[kode]`;
4. tepat sebelum submit chip dicek sekali lagi.

Opsi tidak pernah diklik: klik bisa ikut memilih opsi yang tergambar ulang di
bawah pointer (terbukti live: `['KI', 'AD']`). Request yang terkirim tetap
otoritas terakhir, dan kini dicek **sebelum** status HTTP: request salah broker
dilaporkan sebagai mismatch request, bukan sekadar HTTP 500.

### CS dikecualikan dari capture live

CS tetap kode broker yang dikenal (`BROKER_FLOW_CODES`, `BANDAR_GROUPS`
Sinarmas, backfill, ownership). Tapi Broker Stalker Today menjawab dua request
yang terbukti tepat (`broker.value == ["CS"]`, Today, foreign-only `[]`), 60
detik terpisah, pada 2026-09-30 dengan HTTP 500 text/html, dan tidak pernah ada
satu pun baris CS di `broker_flow` (live 86 tanggal maupun backfill). Karena itu
`BROKER_FLOW_LIVE_UNAVAILABLE = {"CS": ...}` dan capture default memakai
`BROKER_FLOW_CAPTURE_CODES` (29 kode). Semua-atau-tidak-sama-sekali berlaku atas
29 kode aktif itu; CS tidak diminta, tidak dicatat di `broker_flow_scan`, dan
**ketiadaan baris CS bukan nol teramati**. Mengaktifkan kembali butuh bukti live
baru dan perubahan kode eksplisit.

### Yang TIDAK dikerjakan di sini (tindak lanjut)

- Baris live sebelum PR ini untuk KI, dan mungkin kode lain, bisa jadi milik
  broker lain karena balapan ketik + jeda tetap + Enter di selector lama. Tidak
  dihapus dan tidak diperbaiki di PR ini.

- `broker_flow.date` live **tetap** berkunci tanggal scrape (= sesi selesai
  terakhir sebelum tanggal itu), berbeda dengan backfill yang berkunci tanggal
  sesi. Re-key / migrasi ke `session_date` belum dilakukan; pembaca yang
  menggabungkan `broker_flow.date` langsung dengan `price_history.date` masih
  melihat dua rezim.
- Baris live sebelum PR ini tetap apa adanya: sampel 15 teratas, bisa campuran
  dua sesi (08-12, 08-27), bisa sesi hari yang sama (Juli). Belum diperbaiki.
- Missingness di level konsumen belum diperbaiki: `horizon_scan` (`fillna(0)`),
  `ml_v2_experiment_1` (`reindex(...).fillna(0.0)`) dan agregasi `groupby().sum()`
  masih memperlakukan broker/tanggal yang absen sebagai nol.

### Sesi sumber yang diharapkan: kalender IDX resmi (2026-09-30)

`session_date < scrape_date` saja membiarkan sumber basi lolos (scrape Rabu,
sumber Senin, padahal Selasa sesi). Sekarang:

    expected_source_session(scrape_date) = latest_idx_session_before(scrape_date)

yaitu sesi IDX resmi terakhir **sebelum** tanggal scrape MYT, tidak bergantung
jam capture. Konvensi `broker_flow.date = scrape_date` tidak berubah, dan sesi
hari yang sama tetap ditolak walau capture setelah penutupan.

`idx_calendar.py` (murni, tanpa dependensi) memuat libur bursa hari kerja 2026
dan 2027 persis seperti diumumkan, dengan sumber per tahun di `SOURCES`:
2026 = BEI Peng-00171/BEI.POP/09-2025 (23-09-2025), KSEI PENG-0002/DIR/KSEI/0126
(08-01-2026; sebelumnya PENG-0005/DIR/KSEI/1025); 2027 = BEI
Peng-00169/BEI.POP/09-2026 (16-09-2026), KSEI PENG-0004/DIR/KSEI/0926
(24-09-2026). Cakupan `COVERED_FROM` 2026-01-01 s.d. `COVERED_THROUGH`
2027-12-31, versi `CALENDAR_VERSION = idx-2026-2027.v1`. `price_history` hanya
cek konsistensi sekunder (test), bukan otoritas.

Snapshot ditolak (broker_flow tidak disentuh) bila: kalender tidak bisa
menetapkan sesi (tanggal di luar cakupan, lookback > 14 hari, atau lookup
error apa pun); sesi sumber lebih lama dari yang diharapkan (basi); sesi sumber
lebih baru (hari yang sama/masa depan, atau bukan sesi IDX menurut kalender).
Tidak ada fallback ke "hari kerja sebelumnya", `price_history`, atau aturan
lama. `broker_flow_scan` mendapat kolom nullable `expected_session_date` dan
`calendar_version` (migrasi ALTER otomatis; baris lama NULL), diisi untuk run
PERSISTED maupun REJECTED.

**Perawatan:** saat BEI mengumumkan kalender tahun berikut atau mengubah yang
sudah terbit, tambahkan/ubah tahun itu di `idx_calendar.py` beserta sumbernya
dan naikkan `CALENDAR_VERSION`. Tanpa itu, capture mulai 2028-01-02 ditolak
("cannot establish the latest IDX session").

## Lampiran S — manifest bukti tanggal `broker_flow` historis (2026-09-30)

Hanya bukti, bukan migrasi. `broker_flow` **tidak diubah**: tidak ada baris
yang di-re-key, di-update, atau dihapus, dan 2026-08-12 / 2026-08-27 tidak
dibersihkan. `broker_flow.date` tetap bukti akuisisi historis apa adanya.
Interpretasi sesi kanonis hanya **metadata** di
`evidence/broker_flow_date_evidence.json`, dibuat oleh `broker_flow_regime.py`
(baca `neobdm.db` dengan `mode=ro&immutable=1`, tanpa jaringan, tanpa jam
dinding). Belum ada tabel/DB sidecar kanonis, dan belum ada pembaca yang
dimigrasikan.

Baris live historis **tidak punya satu konvensi tanggal**: sebagian memuat sesi
sebelumnya, lima tanggal Juli (07-06/07/09/10/13) memuat sesi hari itu sendiri,
dua tanggal memuat campuran dua sesi. Karena itu setiap `(date, regime)`
(regime: `bval IS NULL` = BACKFILL, selain itu LIVE) diklasifikasi dari bukti:

| date_class | level | canonical_session_date | bukti | tanggal | baris |
|---|---|---|---|---|---|
| SOURCE_DATED_BACKFILL | PROVEN | = `broker_flow.date` | jalur `backfill_inventory.insert_inventory` (`data.date` API) | 218 | 212.839 |
| SCAN_VERIFIED | PROVEN | `broker_flow_scan.session_date` | run PERSISTED `dash_callback_v1` | 1 (09-30 → 09-29) | 965 |
| CONTENT_MATCHED | PROVEN | satu-satunya sesi yang cocok | `broker_daily.parquet` | 49 | 11.433 |
| INFERRED_ONLY | INFERRED | **NULL** | aturan kalender saja | 35 | 6.655 |
| MIXED | AMBIGUOUS | **NULL** | diff git rerun | 2 (08-12, 08-27) | 601 |

`capture_class` terpisah: FULL_CALLBACK (PR #72), DOM_TOP15 (tabel 15 baris
lama), SELECTOR_UNION_BACKFILL (TOP_5_NB/NS).

**CONTENT_MATCHED** = tepat satu sesi di `broker_daily.parquet` dalam jendela
14 hari yang cocok di **setiap** baris (bval dan sval, selisih ≤ 0,05 miliar
setelah /1e9; baris tanpa pasangan di parquet tidak pernah dihitung cocok).
Hasil: 49/49 tanggal cocok penuh, korelasi ≥ 0,999995, runner-up ≤ 0,9004, dan
sesi runner-up cocok di ≤ 3,7% baris. Parquet itu gitignored, jadi bukti yang
dibawa manifest: SHA-256 file (`c8d1948f…05cc32`, 5.609.444 baris,
2025-08-22..2026-08-21), statistik per tanggal, dan `rows_sha256` baris yang
diklasifikasi. **Gagal tertutup:** tanpa parquet (atau hash berbeda) tidak ada
tanggal yang menjadi CONTENT_MATCHED; tanggal itu tetap INFERRED_ONLY.

**Inferred ≠ verified.** `inferred_session_date` = `idx_calendar.latest_idx_session_before`
(asumsi scrape sebelum pasar buka) dan tidak pernah menjadi kanonis. Pada 5
tanggal Juli di atas, inferensi ini terbukti **salah**
(`summary.live_inference_disagrees_with_proof`). 35 tanggal INFERRED_ONLY
(08-25..09-29 minus 08-27) berada di luar cakupan parquet.

**MIXED dikarantina.** Bukti dari riwayat git `neobdm.db` (blob tercatat di
`AUDITED_MIXED`, diturunkan ulang oleh tes): 08-12 = tulis pertama 212 baris
(6793d52) lalu rerun (f441ec1): 110 identik, 102 berubah, 110 baru. 08-27 = 198
baris (8b454b0) lalu rerun (da4d96b): 92 identik, 106 berubah, 81 baru. Baris
"identik" bisa sisa tulis pertama atau ditulis ulang dengan nilai sama; tidak
bisa dibedakan. Kanonis dan inferensi keduanya NULL.

Salinan akhir pekan/libur: satu sesi bisa dipegang beberapa tanggal
(`summary.live_sessions_held_by_several_dates`); konsumen yang nanti memakai
sesi kanonis harus dedupe. Sesi 07-08 tidak pernah tertangkap.

**Memakai:** `py -3 broker_flow_regime.py verify` membandingkan manifest dengan
DB (`rows_sha256` per record). Nilai backfill disembuhkan tiap malam oleh
top-up, jadi di backfill hanya jumlah baris yang dikunci; baris live tanggal
lampau tidak boleh berubah.

**Manifest yang di-commit = satu snapshot teraudit** (`AUDITED_SNAPSHOT`,
`audited-2026-09-30`, master 1aeca53): 232.493 baris, 305 record, BACKFILL
212.839/218, LIVE 19.654/87, hash urut `9c433af0…d353`, `broker_flow_scan` 29
baris / `ff59ea3a…04d7` (sumber SCAN_VERIFIED), parquet `c8d1948f…`, kalender
runtime `idx-2026-2027.v1`.
`build` default memeriksa semua fakta itu **sebelum** mengklasifikasi; kalau ada
yang beda (tanggal baru, grup hilang/berubah, baris scan berubah/bertambah
walau `broker_flow` identik, parquet lain/tidak ada, versi kalender lain) → ditolak,
exit 2, tidak ada yang ditulis. DB kerja berubah tiap malam, jadi rebuild
dilakukan dari `neobdm.db` di commit 1aeca53 (identik byte):

    git show 1aeca53:neobdm.db > <tmp>/neobdm.db
    py -3 broker_flow_regime.py build --db <tmp>/neobdm.db \
        --broker-daily <path>/broker_daily.parquet

Snapshot baru harus disengaja: `build --new-snapshot --out <file lain>`; path
manifest teraudit ditolak. Tanggal MIXED juga dikunci ke `rows_sha256` baris
rerun-nya: bila baris 08-12/08-27 berubah, build menolak alih-alih tetap
menyebutnya MIXED.

**SCAN_VERIFIED harus membuktikan dirinya** (berlaku juga untuk
`--new-snapshot`). Untuk run PERSISTED satu `scrape_date`: satu
`run_started_utc`; tiap `broker_code` sekali; semua baris OK /
`dash_callback_v1`; satu `session_date` non-NULL yang **sama dengan**
`idx_calendar.latest_idx_session_before(scrape_date)` (jadi sesi hari yang
sama, masa depan, bukan-sesi, atau basi ditolak; kalender yang tidak bisa
menjawab juga ditolak). Metadata PR #73: setiap baris punya satu
`expected_session_date` non-NULL yang sama dengan `session_date`, dan satu
`calendar_version` non-NULL. Metadata NULL hanya diterima untuk run di
`AUDITED_LEGACY_SCAN_RUNS` (hanya run 09-30 pra-PR #73, `…01:52:17.810039`).
`calendar_version` yang tercatat tidak harus sama dengan versi runtime:
kalender runtime menurunkan ulang sesinya dan harus cocok dengan
`expected_session_date`, dan itulah cek yang substantif. Menuntut label versi
sama justru membuat semua run lama tak terpakai setelah kalender dinaikkan
(wajib sebelum 2028). `tracked_rows` non-NULL dan **per broker** sama dengan
jumlah baris live broker itu pada tanggal tersebut, dan tidak ada broker live di
luar run. Tidak ada angka 29 yang dikunci di aturan umum (konfigurasi capture
bisa berubah); snapshot teraudit sudah mengunci 29 barisnya lewat hash scan.
Pelanggaran apa pun → `ProvenanceContradiction`, build ditolak (exit 2), tidak
pernah diturunkan diam-diam ke INFERRED_ONLY.

Tes: `test_broker_flow_regime.py` (pytest; set `BROKER_DAILY_PARQUET` untuk
tes reproduksi byte-identik — parquet yang disebut eksplisit tapi hash-nya
salah membuat tes GAGAL, bukan skip). Belum masuk daftar CI `check_ml_health`.

## Lampiran T — pembaca kanonis `broker_flow` (baca-saja) (2026-10-01)

Tiga lapis, masing-masing dengan satu tugas:

| lapis | apa | siapa |
|---|---|---|
| RAW | `broker_flow.date` = kunci akuisisi historis, apa adanya | tabel `broker_flow` |
| EVIDENCE | sesi mana yang **terbukti** dipegang tiap `(date, regime)` | `evidence/broker_flow_date_evidence.json` (Lampiran S) |
| CANONICAL READ | baris per sesi pasar, hanya yang PROVEN, tanpa salinan ganda | `broker_flow_canonical.py` |

    load_canonical_broker_flow(db_path, manifest_path, trust="proven")
    inspect_broker_flow_evidence(db_path, manifest_path)

Hanya pembaca. Tidak ada tabel/DB sidecar, migrasi, atau baris yang diubah.
**Belum ada konsumen yang dimigrasikan**: `walk_forward_backtest`,
`horizon_scan`, `ml_v2_experiment_1`, `multiday_features` dll. masih membaca
`broker_flow.date` langsung (dua rezim tanggal, `fillna(0)`). Itu PR berikutnya.
(Update: `walk_forward_backtest.build_panel()` sudah dimigrasikan, Lampiran V.)

### Aturan

- **Tanggal mentah dipertahankan.** Tiap baris kanonis membawa
  `acquisition_date` (= `broker_flow.date` baris yang bertahan) **dan**
  `canonical_session_date`. Tidak ada kolom bernama `date`: konsumen harus
  memilih kunci sesi secara eksplisit. Semua tanggal berupa `str` ISO.
- **Sesi kanonis hanya dari bukti.** Nilainya selalu `canonical_session_date`
  manifest. Tidak ada aturan kalender dan tidak ada fallback ke tanggal mentah.
  Kalender IDX hanya dipanggil di dalam `bfr.scan_evidence` (bukti PR #74 yang
  diulang untuk SCAN_VERIFIED) sebagai **cek**; sesinya tetap label sumber.
- **Trusted = PROVEN.** SOURCE_DATED_BACKFILL, SCAN_VERIFIED, CONTENT_MATCHED.
  INFERRED_ONLY (tebakan kalender) dan MIXED (dua sesi) **dikecualikan secara
  default** (`.excluded`), terlihat di `inspect_broker_flow_evidence` dengan
  `canonical_session_date` NULL, dan tidak pernah dinaikkan. `trust` hanya
  menerima `"proven"`. Dua fungsi, bukan satu parameter mode, karena bentuk
  hasilnya berbeda (kanonis ter-dedupe vs baris mentah beranotasi).
- **Dedupe per sesi.** Satu sesi bisa dipegang beberapa tanggal akuisisi
  (salinan akhir pekan/libur). Salinan identik persis di
  `(bval, sval, netval, bavg, savg)` (NULL ikut dibandingkan) → **satu** baris
  per `(canonical_session_date, ticker, broker_code)`. Yang bertahan = salinan
  dengan `acquisition_date` paling awal (unik karena PK
  `(date, ticker, broker_code)`); `copy_acquisition_dates` mencatat semua
  tanggal yang memegang salinan itu. Kunci yang hanya ada di sebagian salinan
  tetap diambil (union). Nilai **tidak pernah dijumlahkan**. Kelas bukti tidak
  dipakai sebagai ranking (semua PROVEN; tidak ada salinan identik lintas kelas).
- **Salinan yang konflik gagal tertutup.** Satu kunci yang nilainya berbeda
  antar-salinan (VALUES_CONFLICT), atau record trusted dengan `capture_class`
  berbeda di satu sesi walau tanpa kunci bersama (CAPTURE_CLASSES_DIFFER), →
  **seluruh sesi dikarantina** (`.quarantined`, tanpa baris di view trusted).
  Bukan per kunci: membuang kunci yang konflik saja menyisakan penampang yang
  justru kehilangan aliran terbesar yang dilihat kedua capture; memilih satu
  record = ranking karangan.
- **Karantina teraudit tidak hilang saat bukti diturunkan.** Bila manifest baru
  menurunkan salah satu record konflik ke INFERRED_ONLY, pembaca tetap
  mengarantina sesi selama seluruh kunci grup akuisisi, `row_count`, dan
  `rows_sha256` yang membentuk konflik teraudit masih hadir persis. Ini bukan
  promosi bukti: record yang diturunkan tetap membawa kelas bukti manifest baru
  saat inspeksi, tetapi status semua baris grup konflik = QUARANTINED dan tidak
  ada sisi yang bocor ke view kanonis. Grup LIVE yang hash-nya berubah tidak
  memakai jangkar lama; grup BACKFILL yang di-top-up tetap menjangkar
  (Lampiran U).
- **Absen ≠ nol.** Tidak ada baris sintetis, tidak ada `fillna`, NULL tetap
  `None`. `get(sesi, ticker, broker)` → `None` hanya untuk broker yang tidak
  teramati di sesi yang tercakup; sesi tanpa baris trusted →
  `SessionNotCovered` (`status` QUARANTINED atau NOT_COVERED). `.sessions` =
  sesi tercakup → capture_class-nya.
- **`capture_class` tetap terlihat** di tiap baris. Tidak satu pun merupakan
  universe broker penuh:

| capture_class | unit cakupan | netval |
|---|---|---|
| SELECTOR_UNION_BACKFILL | per ticker: union selector TOP_5_NB/NS | `nlot*100*close/1e9`; bval/sval/bavg/savg NULL; 0 = nlot 0 |
| DOM_TOP15 | per **broker**: tabel beli/jual 15 baris yang dirender, hanya ticker yang dilacak; satu sisi bisa hilang (08-22) | ≈ `bval − sval` (berbasis nilai) |
| FULL_CALLBACK | per broker: tabel callback penuh PR #72 | ≈ `bval − sval` |

  Nilai LIVE = miliar Rp dibulatkan 0,1: `0.0` dengan avg > 0 = nilai
  teramati di bawah 0,05 miliar; sisi dengan nilai 0 **dan** avg 0 = sisi yang
  tidak tampil di tabel (untuk DOM_TOP15: tidak diketahui, bukan nol teramati).
  Fitur seperti n_brokers, konsentrasi, dan korelasi berubah struktur di batas
  07-03/07-06 dan 09-29: stratifikasi per capture_class.
- **PROVEN untuk BACKFILL = tanggalnya dari sumber, bukan nilainya benar.**
  Nilai backfill ditulis ulang tiap malam oleh top-up, dan snapshot teraudit
  sendiri memuat cacat yang diketahui (`ML_V2_EXPERIMENT_1E_KNOWN_LIMITATION_RAJA.md`).

### Data teraudit (manifest committed + `git show 1aeca53:neobdm.db`)

- View trusted: **250 sesi, 220.343 baris**. 217 sesi backfill / 211.805
  baris, 32 sesi CONTENT_MATCHED / 7.573, 1 SCAN_VERIFIED (09-29) / 965.
- 10 sesi dipegang lebih dari satu record PROVEN; 3.642 salinan identik
  digabung. Sembilan di antaranya LIVE saja (07-07, 07-10, 07-13, 07-17,
  07-24, 07-31, 08-07, 08-14, 08-21): 0 konflik nilai di kunci bersama.
  08-21: 08-22 hanya 202 dari 211 kunci (tabel sisi AO/PD/RX/SQ tidak ada),
  08-23 = 08-24; union memulihkan 211 (202 dari 08-22, 9 dari 08-23).
- **Sesi 07-03 dikarantina.** BACKFILL 07-03 (1.034 baris) dan LIVE 07-05 (218
  baris, CONTENT_MATCHED) sama-sama PROVEN untuk 07-03, tapi ukurannya berbeda
  (netval lot × close dengan bval/sval NULL vs netval berbasis nilai): 168
  kunci bersama semuanya berbeda (rasio backfill/live −42,05 s.d. 126,79, 6
  berlawanan tanda, hanya 1 sama persis), 866 kunci hanya backfill, 50 hanya
  live. 1.252 baris mentah ditahan. `summary.live_sessions_held_by_several_dates`
  PR #74 hanya melihat record LIVE, jadi pasangan lintas rezim ini tidak
  tercantum di sana.
- Dikecualikan: 37 record / 7.256 baris (35 INFERRED_ONLY, MIXED 08-12 dan
  08-27). Sesi 08-12 tetap ada lewat akuisisi 08-13.
- Akuntansi baris mentah: 220.343 + 3.642 + 1.252 + 7.256 = 232.493.
- Sesi tanpa baris trusted (celah, bukan nol): 07-03 (karantina), 07-08 (tidak
  pernah tertangkap), 08-11, dan 08-24..09-28 (hanya INFERRED_ONLY/MIXED).
  Tanggal-tanggal Juli yang sesinya hari itu sendiri (07-06/07/09/10/13) tetap
  memegang sesi terbuktinya.

### Kontrak snapshot (penting sebelum migrasi konsumen)

Manifest menggambarkan **satu DB persis**: himpunan `(date, regime)` sama, dan
`row_count` serta `rows_sha256` sama untuk **setiap** kelompok, termasuk
BACKFILL. Kalau tidak → `ManifestMismatch` dengan `.diff` (kunci sama dengan
`bfr.verify_manifest`: missing / gone / count_changed / content_changed). Tidak
ada toleransi drift: `verify` PR #74 memantau drift, sedangkan pembaca hanya
menyajikan baris yang benar-benar dihitung bukti. Karena DB kerja berubah
tiap malam (tanggal LIVE baru, top-up backfill), pembaca **menolak
`neobdm.db` kerja setelah 1aeca53**. Pasangan yang didukung:

1. `git show 1aeca53:neobdm.db` + manifest committed (teraudit);
2. salinan beku DB lain + manifest yang dibangun untuk salinan itu:
   `py -3 broker_flow_regime.py build --new-snapshot --db <salinan>
   --broker-daily <parquet teraudit> --out <file lain>`. `--broker-daily`
   harus parquet teraudit (sha256 `c8d1948f…`) atau tidak diberikan sama
   sekali. Tanpa parquet, semua tanggal DOM_TOP15 turun ke INFERRED_ONLY.
   Kalau manifest itu memuat record CONTENT_MATCHED yang tidak persis sama
   dengan record manifest teraudit (mis. parquet baru yang mencocokkan tanggal
   08-25..09-29), pembaca **menolak seluruh manifest** (`ManifestInvalid`),
   bukan menurunkan tanggal itu. Menerima content match baru = keputusan PR
   migrasi.

Refresh manifest untuk DB kerja: Lampiran U (`broker_flow_manifest_refresh.py`).

**Akar kepercayaan.** Manifest teraudit dikenali dari **isinya**:
sha256 `bfr.dumps(manifest)` = blob git `146b3efd…8025`, tidak tergantung CRLF
checkout (`AUDITED_MANIFEST_SHA256`). Label `input.snapshot` tidak memberi hak
apa pun, dan manifest lain yang berlabel `audited-2026-09-30` ditolak. Setiap
klaim PROVEN di manifest mana pun dicek ulang:

- SOURCE_DATED_BACKFILL secara struktural: regime BACKFILL, kanonis = tanggal
  ≤ `BACKFILL_END` 2026-07-04, dan `evidence_ref` = jalur generasi;
- SCAN_VERIFIED dibuktikan ulang dengan `bfr.scan_evidence` atas
  `broker_flow_scan` dan baris live DB, dan fingerprint run harus sama. Tanggal
  yang dibuktikan run PERSISTED wajib berlabel SCAN_VERIFIED;
- CONTENT_MATCHED tidak bisa diulang (parquet gitignored), jadi hanya diterima
  bila record-nya persis sama dengan manifest teraudit (tanggal, kanonis,
  `row_count`, `rows_sha256`);
- MIXED = persis tanggal `AUDITED_MIXED`, dua arah, dengan state rerun yang
  dikunci.

Turun ke INFERRED_ONLY boleh; menaikkan yang tak bisa dicek tidak pernah.

**Sumber harus diam (quiescent).** Tidak boleh ada -wal/-shm/-journal, file DB
dengan `st_nlink > 1` ditolak karena alias hard link bisa menyembunyikan sidecar,
format header harus dikenal, dan identitas file (ukuran, mtime_ns, sha256) harus
sama sebelum dan sesudah dibaca (konvensi `targeted_actor_observations`); kalau
tidak → `SourceStateError`. Setiap kunci mentah `(date, ticker, broker_code)`
wajib unik meskipun schema sumber rusak dan kehilangan PK; duplikat identik
maupun konflik ditolak, tidak di-dedupe. Koneksi `bfr.connect_readonly`
(`mode=ro&immutable=1`); setiap statement berupa SELECT atau
PRAGMA table_info. Hasil membawa `db_path`, `db_sha256`, `manifest_sha256`
dan `audited`. Konsumen sebaiknya membaca harga dari file DB yang sama, karena
netval backfill = lot × close dari `price_history` DB itu.

`inferred_session_date` pada setiap record wajib NULL atau string tanggal ISO
ketat `YYYY-MM-DD`; object, array, angka, format ringkas, dan string bukan
tanggal ditolak sebagai `ManifestInvalid`. Aturan MIXED yang mewajibkan NULL
tetap berlaku.

Tes: `test_broker_flow_canonical.py` (pytest). Fixture sintetis diklasifikasi
oleh generator PR #74 yang asli. Tes data nyata membaca DB 1aeca53 dari git;
kalau di-skip (mis. shallow clone), artinya tes itu tidak berjalan. Seperti
`test_broker_flow_regime.py`, file ini belum masuk daftar CI
`check_ml_health`.

## Lampiran U — refresh manifest bukti `broker_flow` dari jangkar teraudit (2026-10-01)

Hanya infrastruktur refresh. `neobdm.db`, `evidence/broker_flow_date_evidence.json`,
baris `broker_flow`, capture, dan konsumen **tidak diubah**; belum ada konsumen
yang dimigrasikan, belum ada tabel/view/migrasi.

    py -3 broker_flow_manifest_refresh.py --db <salinan beku neobdm.db> \
        --out <file lain> [--source-commit SHA]

**Jangkar, bukan rantai.** Setiap refresh memercayai manifest teraudit PR #74
secara langsung, dikenali dari **isinya** (sha256 `bfr.dumps` = `146b3efd…8025`,
`AUDITED_ANCHOR_SHA256`, wajib sama dengan `bfc.AUDITED_MANIFEST_SHA256`).
Path, nama file, dan label `input.snapshot` tidak memberi hak apa pun; manifest
hasil refresh tidak pernah bisa jadi jangkar refresh berikutnya. Jangkar hanya
dibaca.

| grup `(date, regime)` | hasil refresh |
|---|---|
| BACKFILL milik jangkar | SOURCE_DATED_BACKFILL diturunkan ulang PR #74 dari baris **sekarang** (`row_count`, `rows_sha256` baru); bukti jalur generasi sama, field lain wajib sama dengan record jangkar |
| LIVE milik jangkar | `row_count` + `rows_sha256` wajib **persis** sama, untuk **semua** kelas; kalau tidak → ditolak |
| ↳ CONTENT_MATCHED, MIXED, INFERRED_ONLY | record jangkar dibawa apa adanya; parquet tidak dibaca |
| ↳ SCAN_VERIFIED | dibuktikan ulang dengan `bfr.scan_evidence` atas `broker_flow_scan` sekarang; sesi, fingerprint, dan run harus sama dengan jangkar |
| LIVE baru setelah jangkar (> 2026-09-30) | aturan PR #74 tanpa parquet: SCAN_VERIFIED bila run PERSISTED membuktikannya, selain itu INFERRED_ONLY (kanonis NULL) |
| lainnya | ditolak: grup jangkar hilang, tanggal BACKFILL baru, grup LIVE baru di dalam cakupan jangkar, tanggal historis yang kini diklasifikasi lain (mis. run scan baru untuk tanggal lama) |

Tidak ada CONTENT_MATCHED baru (itu tugas berikutnya). CONTENT_MATCHED jangkar
yang berubah/hilang **ditolak**, tidak diturunkan ke INFERRED_ONLY dan tidak
dicarikan sesi lain. Kontradiksi PR #74 (run scan, state rerun AUDITED_MIXED)
menolak refresh. Semua pelanggaran dilaporkan sekaligus (`RefreshRefused`).

Kenapa INFERRED_ONLY historis juga dikunci: penulis live hanya menulis tanggal
scrape hari itu, jadi perubahan baris live tanggal lampau = penulisan ulang bukti
akuisisi (pola yang dulu menghasilkan MIXED), dan tidak ada alasan terdokumentasi
untuk mengizinkannya. BACKFILL tidak dikunci karena top-up malam
(`backfill_inventory.insert_inventory`, INSERT OR REPLACE untuk tanggal
≤ `BACKFILL_END` di jendela 360 hari) memang menulis ulang nilainya.

**Output.** `--out` wajib. Path manifest teraudit yang di-commit, jangkar yang
dipakai, DB sumber, dan sidecar SQLite-nya (`-wal`, `-shm`, `-journal`, dari path
yang diberikan maupun path resolve-nya) ditolak, termasuk aliasnya (path yang
di-resolve: symlink/junction, huruf besar-kecil, `..`, titik/spasi di akhir yang
dibuang Win32 walau file belum ada; hard link lewat `samefile`), sebelum build dan
lagi tepat sebelum `os.replace`. File di path sidecar akan membuat sumber terbaca
hidup/rusak pada cek identitas berikutnya. Ditulis ke file temp di folder yang
sama, divalidasi, lalu `os.replace`; gagal di titik mana pun → temp dihapus,
`--out` lama tidak berubah. Deterministik: DB + jangkar sama → byte sama (tanpa
jam dinding, tanpa path).

**Validasi diri sebelum terbit.** `bfc.check_manifest` →
`load_canonical_broker_flow(db, temp)` harus menerima (struktur, grup dan hash
per grup, bukti scan diulang) → file yang divalidasi pembaca harus **persis**
manifest yang dibangun (`cf.manifest_sha256` = sha256 `bfr.dumps(manifest)`,
konvensi hash isi pembaca: format/CRLF boleh beda, isi tidak) → `db_sha256`
pembaca = sha file yang diklasifikasi → setiap sesi yang dikarantina record
trusted jangkar (aturan collapse pembaca atas baris sekarang) tetap dikarantina.
Tepat sebelum `os.replace`, isi file temp dicek sekali lagi terhadap manifest
yang sama.

**Sumber.** Seperti pembaca: -wal/-shm/-journal, `st_nlink > 1`, format tak
dikenal → `SourceStateError` sebelum apa pun diklasifikasi; identitas file
(ukuran, mtime_ns, sha256) sebelum = sesudah baca. `mode=ro&immutable=1`, hanya
SELECT / PRAGMA table_info.

**Bentuk manifest.** Tetap skema PR #74 (`generator` broker_flow_regime.py v1:
record dibuat generatornya), jadi pembaca tidak perlu diubah untuk metadata.
`input.snapshot` = `audited-anchor-refresh-v1`; `input.content_match_evidence` =
milik jangkar (hanya dirujuk record CONTENT_MATCHED jangkar). Blok top-level
`refresh`: kontrak, jangkar (sha, label, source_commit, jumlah record, tanggal
terakhir), `current_db` (sha256, bytes), `inheritance` (BACKFILL diturunkan
ulang + tanggal yang berubah, LIVE jangkar per kelas, LIVE baru), aturan.

### Satu perubahan di pembaca (PR #75)

Data nyata 2026-10-01 membuktikan celah: top-up mengubah grup BACKFILL 07-03
(KIOS/ZP netval 1,00436 → 0,0; tetap 1.034 baris), sehingga aturan jangkar PR #75
("grup yang hash-nya berubah tidak memakai jangkar lama") melepas karantina
07-03. Manifest `broker_flow_regime.py build --new-snapshot` tanpa parquet atas
DB 6f21a72 membuat pembaca menyajikan sesi 07-03 dari sisi BACKFILL
(SELECTOR_UNION_BACKFILL). `_anchored_quarantine_records` sekarang: grup LIVE
jangkar tetap harus persis; grup SOURCE_DATED_BACKFILL jangkar tetap menjangkar
selama manifest masih mencatatnya SOURCE_DATED_BACKFILL, dengan record
**sekarang** (akuntansi baris mengikuti baris sekarang). Top-up mengubah nilai,
bukan sesi (= tanggalnya) atau capture class, jadi konfliknya dengan capture live
sesi itu tetap ada. Hanya bisa menambah karantina, tidak pernah mengurangi.

### Snapshot nyata 2026-10-01 (master 6f21a72, blob `neobdm.db` c2f839ad, sha256 `df3d94af…`)

- Tanggal akuisisi maks 2026-10-01; 306 grup / 233.549 baris (jangkar 305 /
  232.493). Grup baru hanya (2026-10-01, LIVE), 947 baris.
- `broker_flow_scan` 58 baris (jangkar 29). Run PERSISTED 2026-10-01
  `01:51:07.838116`: 29 broker, `session_date` = `expected_session_date` =
  2026-09-30, `idx-2026-2027.v1`, `tracked_rows` per broker = baris live →
  SCAN_VERIFIED → 2026-09-30.
- 49/49 CONTENT_MATCHED jangkar tidak berubah. Tidak ada grup LIVE historis yang
  berubah atau hilang.
- 173 dari 218 grup BACKFILL berubah (2025-10-06..2026-07-03 = jendela top-up),
  +109 baris (212.839 → 212.948).
- Manifest refresh diterima pembaca. View kanonis: **251 sesi** (217 backfill,
  32 DOM_TOP15, 2 FULL_CALLBACK), **221.399 baris** (211.914 backfill, 7.573
  CONTENT_MATCHED, 1.912 SCAN_VERIFIED). Akuntansi 221.399 + 3.642 salinan +
  1.252 karantina + 7.256 dikecualikan = 233.549. Sesi baru: 09-30
  (FULL_CALLBACK, dari akuisisi 10-01).
- **07-03 tetap dikarantina** (CAPTURE_CLASSES_DIFFER + VALUES_CONFLICT, 168
  kunci, 1.252 baris); `get` → `SessionNotCovered` QUARANTINED.
- Refresh atas DB teraudit 1aeca53 menghasilkan record yang **persis** sama
  dengan jangkar.

Catatan: `test_broker_flow_regime.py::test_committed_manifest_still_describes_the_real_rows`
sudah gagal di master sejak top-up 2026-10-01, karena tes itu mengunci jumlah
baris BACKFILL `neobdm.db` kerja (top-up menambah baris). Bukan dari PR ini;
refresh ini memperlakukan BACKFILL sebagai turunan.

Tes: `test_broker_flow_manifest_refresh.py` (pytest). Fixture sintetis =
fixture `test_broker_flow_canonical.py` + top-up + capture 10-01. Tes data nyata
membaca `neobdm.db` 6f21a72 dan 1aeca53 dari git; kalau di-skip, artinya tidak
berjalan. Belum masuk daftar CI `check_ml_health`.

## Lampiran V — konsumen referensi: `walk_forward_backtest.build_panel()` membaca broker flow kanonis (2026-10-01)

Satu konsumen riset dimigrasikan, sebagai pola untuk migrasi berikutnya:
`walk_forward_backtest.build_panel()` tidak lagi membaca tabel `broker_flow`.
Fitur broker hanya berasal dari `broker_flow_canonical.load_canonical_broker_flow()`
(Lampiran T) di bawah manifest **eksplisit** yang menggambarkan DB yang dibaca.
`neobdm.db`, manifest bukti, `broker_flow_canonical.py`,
`broker_flow_manifest_refresh.py`, `broker_flow_regime.py`, capture, dan workflow
**tidak diubah**. Konsumen lain tidak dimigrasikan (daftar di bawah).

Alur (dua langkah, dua lapis terpisah; manifest hasil refresh tidak di-commit):

    python broker_flow_manifest_refresh.py --db neobdm.db --out <manifest>
    python walk_forward_backtest.py --broker-flow-manifest <manifest>

### Kontrak

    build_panel(conn, *, broker_flow_db_path, broker_flow_manifest_path)

- **Manifest wajib.** Tidak ada default (juga bukan manifest teraudit, yang
  hanya menggambarkan DB 1aeca53). `None`/kosong → `BrokerFlowManifestRequired`
  dengan pesan yang menunjuk `broker_flow_manifest_refresh.py`; tanpa argumen →
  `TypeError`. CLI tanpa `--broker-flow-manifest` → exit 2. `build_panel` tidak
  pernah membuat manifest sendiri.
- **Adapter** `canonical_broker_flow_frame(canonical)`: satu baris per
  `CanonicalRow`, kolom persis yang dibaca helper fitur:

| kolom frame | dari `CanonicalRow` |
|---|---|
| `date` | `canonical_session_date` (**bukan** `acquisition_date`) |
| `ticker`, `broker_code` | apa adanya |
| `netval` | apa adanya; NULL → NaN (float64) |

  Tidak ada baris sintetis, tidak ada penjumlahan salinan (pembaca sudah
  dedupe), tidak ada `fillna(0)`. Hanya baris PROVEN: INFERRED_ONLY/MIXED
  dikecualikan, sesi karantina (07-03) tidak menyumbang apa pun.
- **Satu snapshot, yang benar-benar dibaca `conn`.** `build_panel` membuka
  **satu transaksi baca** di `conn` (karena itu `conn` tidak boleh sedang dalam
  transaksi), lalu membandingkan **image SQLite `main` yang terlihat di
  transaksi itu** (`conn.serialize("main")`, sha256 = `image_sha256`) dengan
  image yang sama dari file yang diverifikasi pembaca kanonis (dibaca dengan
  `bfr.connect_readonly`, immutable, dan diikat ke `canonical.db_sha256` lewat
  sha file sebelum dan sesudah). Serialisasinya identik di kedua sisi.
  `clean_panel` berjalan di dalam transaksi yang sama (pembaca rollback-journal
  memegang shared lock, pembaca WAL memegang snapshot-nya), image dicek lagi,
  lalu hanya transaksi milik `build_panel` yang di-ROLLBACK. Hash path saja tidak
  cukup dan sudah dibuang: perubahan harga di WAL dengan byte file `main` yang
  sama, dan koneksi lama yang masih membaca inode lama setelah path diganti
  atomik dengan byte kanonis, keduanya ditolak (`BrokerFlowSnapshotMismatch`).
  Harga DB A + broker flow DB B → ditolak (juga kalau broker_flow-nya sama tapi
  harganya beda); salinan byte-identik di path lain (termasuk salinan WAL dengan
  WAL kosong) diterima. Koneksi in-memory/temporary, database ter-ATTACH,
  transaksi terbuka, atau TABLE/VIEW temp yang membayangi `price_history` /
  `price_quarantine` **tanpa peduli huruf besar-kecil** (`PRICE_HISTORY`,
  `Price_History`, …; SQLite me-resolve nama case-insensitive) → ditolak. Alasan:
  netval backfill = lot × close dari `price_history` DB itu. Satu file DB format
  WAL tidak bisa sekaligus jadi sumber kanonis dan koneksi harga: koneksi harga
  membuat -wal/-shm di sampingnya dan pembaca menolaknya (sumber tidak diam).
- **Gagal tertutup.** `ManifestInvalid`, `ManifestMismatch` (termasuk bukti scan
  tidak valid), `SourceStateError` (sumber tidak diam, kunci mentah ganda)
  diteruskan apa adanya; file manifest tidak ada → `FileNotFoundError`. Tidak
  ada baris kanonis, atau tidak ada sesi kanonis yang bertemu harga bersih →
  `BrokerFlowUnavailable`. Tidak ada jalur baca `broker_flow` mentah.
- **Join harga tidak berubah**: `clean_panel(conn, horizons=(1,), lags=(1, 5),
  open_anchored=True)`, inner join pada `(ticker, canonical session)`; kunci
  harga terkarantina tetap membuang baris broker-nya sebelum agregasi.
- **Helper fitur tidak diubah.** `_broker_day_aggregates`: baris NULL tetap
  dihitung `n_brokers` (broker teramati), `net_flow_total` skipna;
  `_broker_correlation_1d`: NULL dan absen sama-sama keluar dari vektor.
  **Satu aturan baru di konsumen:** `broker_correlation_1d` = NaN pada sesi yang
  sesi harga sebelumnya **tidak punya baris trusted sama sekali** (karantina,
  hanya INFERRED_ONLY/MIXED, atau tidak pernah tertangkap). Tanpa itu helper
  membandingkan dengan sesi yang lebih lama di seberang lubang (07-06 vs 07-02
  melewati 07-03). Ticker yang absen di sesi yang tercakup tetap memakai
  perilaku helper lama.
- **Provenance** (bukan fitur): `panel.attrs["broker_flow"]` = `db_path`,
  `db_sha256`, `manifest_path`, `manifest_sha256`, `manifest_snapshot`,
  `source_commit`, `audited_manifest`, `image_sha256` (image SQLite yang dibaca
  harga), jumlah sesi/baris, akuntansi, sesi karantina, record yang
  dikecualikan, dan catatan point-in-time. CLI, `check_ml_health`, dan job
  summary `run_ml_reports` mencetak hash-nya. Output pendek (`check_ml_health`,
  pesan Telegram `run_ml_reports`) membawa `PIT_WARNING`: "Broker flow:
  session-aligned; point-in-time availability not proven."
- `broker_flow_canonical` di-import **di dalam** `build_panel`, bukan di level
  modul: modul yang hanya memakai helper split/agregat
  (`experiment_1f_gate_b.HELPER_FILES`) tetap punya closure modul lokal yang sama.

### Point-in-time: TIDAK terbukti

`canonical_session_date` = sesi pasar **milik** observasi, **bukan** timestamp
`known_at`. Migrasi ini memperbaiki penjajaran sesi, filter kepercayaan, dan
dedupe; **bukan** ketersediaan pada EOD(T). Backtest ini tidak boleh disebut
"bebas leakage" hanya karena memakai pembaca kanonis.

Temuan konkret (data nyata di bawah): untuk 27 sesi 07-14..08-21, panel lama
memakai baris akuisisi `d` yang isinya sesi bursa sebelumnya (fitur **basi
satu sesi**).
Panel baru menaruh data sesi `d` di `d`, tetapi data itu baru diakuisisi pada
tanggal akuisisi berikutnya (scrape pra-buka menurut Lampiran E, tidak terbukti
per baris). Jadi dibanding EOD(T), fitur baru di periode itu **lebih maju** dari
fitur lama; target `open(T+1)→open(T+2)` dan waktu capture yang sebenarnya
menentukan apakah itu tersedia sebelum entry, dan itu belum dibuktikan. Baris
backfill tidak membawa waktu capture historis sama sekali. Kontrak
`known_at`/episode = tugas berikutnya.

### Pemanggil `build_panel`

| pemanggil | perubahan |
|---|---|
| `walk_forward_backtest.py` CLI | `--broker-flow-manifest` wajib, `--db` (default `neobdm.db`), koneksi harga read-only |
| `strategy_variants.py`, `shap_analysis.py`, `ara_arb_simulation.py`, `regime_gated_momentum.py` | CLI sama (`parse_cli`); `run_ara_arb_check(..., broker_flow_manifest_path=...)`, `load_neobdm(manifest, db)` |
| `check_ml_health.py` (CI ml-health) | argparse ketat (`--quick`, `--telegram`, `--broker-flow-manifest PATH` atau `=PATH`; opsi tak dikenal/singkatan → exit 2). Manifest yang **diberikan** dipakai apa adanya; hilang/rusak/kosong → problem kesehatan, **tidak pernah** diganti refresh. Hanya bila flag tidak ada: **langkah 1 secara eksplisit** (`broker_flow_manifest_refresh.refresh(neobdm.db, backtest_out/ml_health/broker_flow_manifest.json)`), lalu `build_panel`. Refresh ditolak = problem kesehatan, bukan fallback |
| `run_ml_reports.py` (ml-daily-report) | sama: `--broker-flow-manifest` atau refresh eksplisit ke `backtest_out/ml_reports/`; hash masuk job summary |
| `test_pipeline.py` | fixture kontaminasi jadi file DB + manifest dari generator PR #74; dua penjaga baru (manifest wajib, tanpa SQL `broker_flow` di koneksi harga) yang ikut jalan di CI |

Orkestrator (`check_ml_health`, `run_ml_reports`) menjalankan dua lapis berurutan
karena workflow-nya tidak diubah di PR ini; konsumen riset sendiri tidak pernah
me-refresh.

### Data nyata: lama (mentah) vs baru (kanonis)

`neobdm.db` blob `c2f839ad` (master 6f21a72, era PR #76, sha256 `df3d94af…`),
manifest refresh `audited-anchor-refresh-v1` dengan `source_commit` 6f21a72,
sha256 isi `0188cf97…8660` (deterministik). Dikunci di
`test_walk_forward_canonical.py::test_real_*`.

| | lama | baru |
|---|---|---|
| baris broker | 233.549 mentah; 221.685 setelah join harga | 221.399 kanonis; 216.378 setelah join harga |
| tanggal | 306 tanggal akuisisi (278 terpakai) | 251 sesi kanonis (251 terpakai) |
| baris panel | 10.756 (276 tanggal, 45 ticker) | 9.990 (249 tanggal, 45 ticker) |

- Dikecualikan: INFERRED_ONLY 35 record / 6.655 baris; MIXED 08-12, 08-27 / 601
  baris. Karantina: 07-03, 1.252 baris (CAPTURE_CLASSES_DIFFER +
  VALUES_CONFLICT, 168 kunci). Salinan identik digabung: 3.642.
- Pindah sesi: 45 pasangan (akuisisi → sesi), 12.039 baris trusted, semuanya
  LIVE sesi-sebelumnya (07-08..08-24 CONTENT_MATCHED, 09-30→09-29 dan
  10-01→09-30 SCAN_VERIFIED). Lima akuisisi Juli hari-sama (07-06/07/09/10/13)
  tetap di sesinya sendiri; agregatnya identik lama vs baru (129 baris panel).
- Tanggal panel yang hilang (28): 07-03 (karantina), 07-08, 08-11, 08-24 (tidak
  ada baris trusted untuk sesi itu; baris akuisisinya pindah ke 07-07, 08-10,
  08-21), dan 24 hari kerja 08-26..09-28 (hanya INFERRED_ONLY/MIXED). Muncul: 08-10
  (dari akuisisi 08-11).
- Kunci bersama 9.899 (lama saja 857, baru saja 91). Fitur harga dan target: 0
  berubah. Agregat broker berubah di 642 kunci pada 26 tanggal; semuanya
  re-key di 07-14..08-21 (+ dedupe untuk 07-17, 07-24, 07-31, 08-07, 08-14,
  08-21; + eksklusi MIXED akuisisi 08-12 untuk sesi 08-12). Hanya `broker_correlation_1d` yang berubah di 25 kunci
  (07-06: 14, 07-09: 11): sesi sebelumnya 07-03 dikarantina dan 07-08 tidak
  tertangkap (aturan sesi tertahan). Tidak ada perubahan yang tak terjelaskan.
- 08-21: 211 baris, satu per kunci (202 dari akuisisi 08-22 + 9 dari 08-23;
  salinan 08-22/08-23/08-24 = 202/211/211), nilai = salinan yang bertahan, tidak
  dijumlah.

### Konsumen `broker_flow` mentah yang tersisa (belum dimigrasikan)

`ml_v2_experiment_1`, `horizon_scan`, `multiday_features`, `ddqn_entry_exit`
(`build_episode_frame`), `smart_money_divergence`, `feature_ablation`,
`ara_arb_scan`, `experiment_1f_universe_gate`, `analyze_ticker_patterns`,
`macro_analysis`. Import helper `_broker_day_aggregates` /
`_broker_correlation_1d` / `FEATURES` / `XGB_PARAMS` dari `walk_forward_backtest`
**tidak** membuat modul itu termigrasi. (`neobdm_scraper` dan `price_audit` membaca
`broker_flow` untuk capture/audit, bukan fitur.)

Tes: `test_walk_forward_canonical.py` (pytest; sintetis = fixture
`test_broker_flow_canonical.Synth` + tabel harga, dan DB backfill kecil dengan
manifest dari generator; data nyata = blob git `c2f839ad` + refresh, di-skip
hanya bila objek git tidak ada, artinya tidak berjalan). **Jalan di CI**: workflow
`ml-health.yml` memasang `pytest` dan menjalankan
`python -m pytest -q -rs test_walk_forward_canonical.py` setelah health check
(`if: !cancelled()`). Checkout dangkal biasanya tidak punya blob `c2f839ad`,
jadi tes data nyata di CI bisa ter-skip (terlihat di `-rs`); tes sintetis tetap
menangkap substitusi `acquisition_date`, penjumlahan salinan, karantina, dan
bukti yang dikecualikan. `test_pipeline.py` (ikut health check) memeriksa bahwa
langkah pytest itu masih ada di workflow, plus dua penjaga inti.
