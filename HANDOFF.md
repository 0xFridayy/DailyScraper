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
2. **`get_inventory_bagholders` (neobdm_scraper.py:740) MASIH pakai `/inventory/`
   Plotly yang pensiun** — fitur broker-stalker Telegram (bag-holder) juga rusak,
   perlu migrasi API yang sama. Di luar cakupan rewrite backfill ini.
3. **`bval/sval` kini tersedia dari API** tapi dibiarkan NULL sampai konvensi
   satuan live-vs-backfill direkonsiliasi (live simpan angka page-derived via
   `parse_num`, backfill simpan miliar). Mengisinya jadi perubahan satu baris.
