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
- [ ] `py price_audit.py quarantine`
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

- [ ] Ganti `sharpe_from_returns()` di **empat** file (bukan tiga) dengan
      IC / hit_rate / edge top-decile. Yang terlewat di Temuan 2:
      `ara_arb_simulation.py:98` punya ekspresi identik.
- [ ] Laporkan **base rate** di samping setiap hit_rate. Tanpa itu angkanya
      tidak bisa ditafsirkan — lihat Lampiran B, hit_rate 42,8% yang tercatat
      di repo ternyata **persis sama** dengan base rate universe.
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
