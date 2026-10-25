# CHANGELOG

## 0.2.5 (2026-10-25)
From this version on, the engine's user-facing text is in English (manifest labels, settings, README, code comments and docstrings).
Entries for earlier versions below are kept in Japanese.
### Decimals below 10 in VTE and peak pressure were truncated
- Hamilton shows VTE (expiratory VT) and peak pressure with one decimal below 10 ("9.6") and as integers from 10 up
  (36,881 Study Recorder records: no integer below 10; every value with a decimal is below 10: Exp.Volume 17 (max 9.6), P max 7)
- Up to 0.2.4 these fields were type "int", so a correct EasyOCR reading "9.6" became 9 (Ptranspulm 2026-10-09, B939; shown in the
  yellow alarm colour; the colour did not affect reading: 57 of the 58 yellow VTE breaths matched the Study Recorder, the remaining one was this truncation)
- int fields with small_decimal (VTE, Ppeak) keep a reading of the form "one digit . one digit" as a decimal; everything else stays an integer.
  Cost: one string test
- manifest.json outputs.breath_values format of VTE, Ppeak, VTE_displayed and Ppeak_displayed: {:.0f} → {:g} (so 9.6 is not shown as 10)
### A screen clear right after a normal wrap was missed (Alert Cleared)
- Since 0.2.3 a mid-sweep clear (wrap = 2) was detected only when the pen moved back left by 100 px (SWEEP_WRAP_JUMP) or more.
  A clear right after a normal wrap (pen near the left edge) moved back less than 100 px and was missed
  (Ptranspulm 2026-10-09, B854: cleared at frame 26449, pen x 523 → 472; no Cleared, and the part before the clear was read from the cleared screen)
- A back-jump of SWEEP_RESTART_MARGIN (15 px) or more to near the left edge also starts a new pass (wrap = 2 unless the pen had reached the right edge)
### A patient-trigger triangle at the very left edge was missed (Trigger C instead of A)
- A new triangle is one just behind the pen (pen − apex within TRIGGER_NEW_LAG_PX). A triangle drawn at the left edge as the sweep wraps
  can first appear while the pen is still recorded at the right edge, so pen − apex was about 1100 px and it was not counted
  (Ptranspulm 2026-10-09, B911: apex x 468.5 at frame 27994, pen x 1566)
- pen − apex is now wrap-aware (more than half a pass: subtract one pass; less than minus half a pass: add one pass)
### Breath boundaries are not snapped across an undrawn stretch
- With the B854 fix the part before the clear became readable, and the boundary between B853 and B854 was snapped 1.75 s back onto B853's second
  inspiration (Study Recorder boundary: 1.3 s later). An inspiration-onset candidate is no longer used when an undrawn stretch of CLEARED_GAP_SEC (0.3 s)
  or more lies between it and the original boundary (the true onset may lie inside it)
- A boundary is not moved by more than SNAP_MAX_SEC (0.8 s) when snapping (normally a median of 0.28 s, 99% within 0.39 s, max 0.66 s in 0.2.4).
  B854's inspiration fell in a stretch the ventilator drew as a flat line (no data), so the nearest onset was B853's second inspiration 1.1 s earlier;
  the boundary now stays at the display-update position as in 0.2.4
### Robustness for frames decoded without re-encoding (VentOCR 0.8.0 direct decoding)
- Found by comparing app's 0.8.0-style analysis (original video decoded directly, no per-segment re-encode) with the current style
  (both engine 0.2.5): one Trigger and seven Alerts differed, and in each case the current style was right (Study Recorder)
- Triangles are searched only in the waveform area x [SWEEP_X_LEFT − TRIGGER_X_MARGIN, SWEEP_X_RIGHT + TRIGGER_X_MARGIN) (15 px).
  Sharper frames showed pink in the numbers panel (x about 52), picked up as a triangle in 3,209 frames
- A triangle redrawn at the left edge is from the previous pass also when the pen is at the right edge and the triangle within
  TRIGGER_WRAP_PX (50 px) across the wrap (B417: redrawn at x 472 while the pen was at x 1562; with the frame phase of direct decoding
  no frame showed the old triangle erased, so it became C). The TRIGGER_KEEP_PX check is wrap-aware too
- After an axis-label read that missed a waveform (e.g. read while the scale was changing), the next AXIS_RETRY_FRAMES (10) frames are also read.
  Labels were otherwise re-read only when the label column changed, and a failure could stay for 9.5 s on the old scale
  (B829–B832, 4th panel 0–40 instead of 0–20, values about 2× off)
### Alerts reorganized (user decision A61)
- Abnormal and Popup are reported as one alert, Hidden (the two checks are unchanged; the Abnormal column of Breaths.csv stays)
- TriggerSplit is no longer an alert (splitting by a triangle is part of the algorithm; Frames.csv Trigger_Split still marks it)
- Axis? is renamed Axis. Partial, Cleared, Clipped, Fragment and Long are unchanged
- manifest.json outputs.alerts and the README alert table (conditions, thresholds, accuracy exclusion) match this
### English
- manifest.json labels and breath_values labels, schema.py labels and choice labels, README, and code comments/docstrings are now in English
- Verification (Ptranspulm 2026-10-09 10-25-03.mov, 1 h, python -m ventocr_core, 5 workers, compared with 0.2.4):
  - Breath count (1260) and breath boundaries (Frame, End_Frame) unchanged
  - B939 VTE and B940 VTE_displayed 9 → 9.6 (Study Recorder 9.6); B854 Alert Cleared, Trigger C → blank; B911 Trigger C → A;
    waveform boundaries of B853/B854 and B910/B911 moved (B853/B854: 2643.21 s, Study Recorder 2643.46 s, 0.2.4 2643.87 s;
    B911 start 2799.28 s, Study Recorder 2799.35 s, 0.2.4 2799.06 s). The X column of later waveforms shifts by +10 (B854's clear is a new pass)
  - Trigger vs Study Recorder (IsPatientTriggeredBreath): A 1103/1103 patient, C 151/151 machine, blank 5 (B541, B854, B990, B991, B988)
  - Analysis time: 474.4 s (0.2.4: 450.3 s; within run-to-run variation)

## 0.2.4 (2026-10-25)
### 設定のダイアログが波形の欄を覆った呼吸（Alert に Popup）
- 換気モードの設定などのダイアログは、波形の欄を覆っても縦軸の目盛りを隠さないことがあり、Abnormal にならなかった
  （Ptranspulm 2026-10-09 の B989〜991: フレーム 30050〜30119 に換気モードの設定 PCV+ → SPONT。その間ペン先はダイアログの下で、
  三角も見えず、B990 は自発なのに C になっていた。Study Recorder では Paw・Flow とも欠けなく記録されていた）
- initial_ocr で、波形の欄（x 467〜1567、y 95〜1000）の 8 px おきの画素のうち明るい（グレー 180 より上）ものの割合を
  Frames.csv の Wave_Covered に記録する（1 フレームあたりの手間はほぼ 0）。POPUP_COVER_MIN（0.1）以上のフレームを含む呼吸は Alert に Popup、
  三角が無ければ Trigger を空欄（わからない）にする。実測: ふだん 0.02 以下、ダイアログ 0.3〜0.8
- manifest.json の outputs.alerts に Alert の値の一覧と、読み取りの精度の評価で除く値（exclude_from_accuracy）を宣言した
  （ENGINE_API.md §8.1。除く: Fragment・Abnormal・Popup・Clipped・Partial。除かない: TriggerSplit・Axis?・Long・Cleared）
- 確認（Ptranspulm 2026-10-09 10-25-03.mov の 1 時間、python -m ventocr_core・並列数 5、0.2.3 と比べた）:
  - 呼吸の数（1260）・区切り・波形・数値は同じ
  - Alert に Popup: B988〜991・B1014〜1020・B1030〜1031（13 呼吸。B990・B991 のほかは Abnormal でもあった）
  - Trigger C → 空欄: B990（Study Recorder では自発）
  - 解析時間: 450.3 秒（0.2.3 は 476.3 秒）

## 0.2.3 (2026-10-24)
### 呼吸の途中で呼吸器の画面（波形）が消去されたとき
- Hamilton は、測定値を出さない間（Ptranspulm 2026-10-09 の B541 では約 1.5 秒。Study Recorder でも Paw 0・Flow なし）のあと、
  4 つの波形を全部消して左端から描き直すことがある（1 時間で 4 回: B244・B541・B991・B1238）。開始のフレームは波形でいっぱい、
  終了のフレームはほぼ空になり、波形の前半が空欄になる
- 不具合: 左端に戻ったと分かるのは左端でペン先を見つけたときなので、その前の数フレームは同じ周のまま、画面はもう消えている。
  消去の前の部分を「周の最後のフレーム」（もう消去された画面）から読み、消去の前に描かれていた部分も空欄になっていた
- 消去の前の部分は、消去される直前のフレーム（画面全体が変わった最初のフレームの前）から読む（core._clear_events）。
  そのフレームでは、ペン先を最後に見つけた位置より右の列は読まない（まだ描かれておらず、1周前の波形が残っている）。
  画面が消去された周の列は、縦軸が広がっても読み直さない（もう画面に無い。読み直しの対象に入れると、ほかの周の列の読み直しもできなくなる）
- 呼吸の間に画面が消去されたら Alert に Cleared
- Cleared の呼吸で、波形の描かれていない区間が CLEARED_GAP_SEC（0.3 秒）以上あり三角が無ければ、Trigger を C ではなく空欄（わからない）にする。
  その間に吸気が始まり、三角が描かれなかったかもしれない（B541・B991 は Study Recorder では自発。0.2.2 までは C）
- 確認（Ptranspulm 2026-10-09 10-25-03.mov の 1 時間、python -m ventocr_core・並列数 5、0.2.2 と比べた）:
  - 呼吸の数（1260）・区切り（Frame・End_Frame）・波形の範囲（X・Wave_T0・Wave_Points・Sweep_Start / End・Wave_Trim_*）・数値は同じ
  - 変わったのは: Alert に Cleared（B244・B541・B991・B1238）、Trigger C → 空欄（B541・B991）、
    波形の値（B243・B244・B541・B1237・B1238。消去の前に描かれていた点を読んだ。Paw は B243 14 点・B244 6 点・B541 22 点）、
    Wave_Missing（B243 0.222 → 0.093、B244 0.649 → 0.596、B541 0.571 → 0.436）
  - 読んだ点を Study Recorder（Paw、Flow、Ptranspulm = Paw − P-Optional）と比べた平均の差: Paw 0.13〜0.68 cmH2O、
    Ptranspulm 0.10〜0.72 cmH2O、Flow 1.3〜11 L/min（11 は B244 の 6 点で、時刻が約 0.15 秒ずれた吸気の立ち上がり）
  - 解析時間: 476.3 秒（0.2.2 は 587.5 秒。ばらつきの中）

## 0.2.2 (2026-10-24)
### コンプライアンスの読み違い（小数1桁の取り違え・値が空）を数字の見本で直す
- 画面の数字はきれいに描かれているが、EasyOCR は特定の数字で読み違いを繰り返していた（Ptranspulm 2026-10-09 の 1 時間で 49 呼吸）:
  小数点の後ろの「2」を落とす（41.2 → 41.0）、「57.7」→「57.77」、「51.2」→「51.22」、「47.4」→「47..4」（数字として通らず空）など。
  ほかの動画（Pes 2026-10-07、Ptranspulm 2026-10-08）でも同じ読み違いがあった
- 数字を塊ごとに切り出して 0〜9 の見本（value_glyphs.npz）と照合して読み、全部の数字が見本と合えば（相関 VALUE_MATCH_MIN 0.9 以上）
  その値を使う。合わない数字がある、小数点が最後の2つの数字の間に無い、などのときは今までどおり EasyOCR の値（core._read_glyphs、
  Settings.OCR_FIELDS の glyph_read。コンプライアンスだけ）。*_raw・*_conf は EasyOCR のまま
- 見本は、Ptranspulm 2026-10-09 の動画で Study Recorder と一致した呼吸の半分から、記録の値で数字を決めて作った（数字の高さ約 80 px）。
  正しい数字との相関は 0.985 以上、ほかの数字とは 0.65 以下
- EasyOCR の読んだ文字の「..」を「.」にする（dec1 の値すべて。見本で読めなかったときのため）
- 確認（Ptranspulm 2026-10-09 10-25-03.mov の 1 時間、python -m ventocr_core・並列数 5、Study Recorder の呼吸ごとの値と比べた）:
  - コンプライアンスの一致: 0.2.1 は 1192/1241（空 16・読み違い 33）、0.2.2 は 1241/1241
  - 0.2.1 と比べて: 呼吸の数（1260）・区切り・波形・Breaths.csv のほかの列は同じで、変わったのはコンプライアンスの 49 呼吸（すべて記録と一致する方へ）
  - 見本の読み: 10-09 の 1241 呼吸すべてと赤い文字（画像で作った）で正しい。10-07・10-08 の動画の 600 フレームで、EasyOCR と違ったのは
    12 回で、どれも EasyOCR の読み違い（37.2 → 37 など）。見本で読めなかったフレーム（9 回）は EasyOCR も読めていなかった
  - 解析時間: 587.5 秒（0.2.1 は 668.7 秒、0.2.0 は 624.8 秒）。見本の照合は 1 回 0.3〜1.3 ms
- 解像度・字体の違う画面（別の機種・設定、カメラで撮った動画）では見本と合わず、EasyOCR の値になる（0.2.1 と同じ）

## 0.2.1 (2026-10-24)
### コンプライアンスが 100 以上で 1/10 になる不具合
- Hamilton の画面のコンプライアンスは、100 未満は「89.0」のように必ず小数1桁、100 以上は「126」のように小数点の無い整数で表示する。
  0.2.0 までは dec1 の決まり（小数点の無い 3 桁以上は、小数点を読み落としたとみなして最後の1桁の前に補う）で、126 を 12.6 にしていた
- 小数点の無い 3 桁以上を読んだときだけ、ROI の画素で小数点（数字の下端の近くの小さな塊）を確かめ、無ければ整数として扱う
  （core._has_dot、Settings.OCR_FIELDS の dot_check。コンプライアンスだけ。MinVol・P0.1 は今までどおり）。
  数字が見つからないとき（ROI が空など）も整数として扱う。確かめは 1 回 0.1〜0.8 ms
- 確認（Ptranspulm 2026-10-09 10-25-03.mov の 1 時間、Study Recorder の呼吸ごとの値と比べた）:
  - Study Recorder が 100 以上の 85 呼吸: 0.2.0 は 85 呼吸すべてが 1/10、0.2.1 は 85/85 が一致
  - 100 未満の 1156 呼吸: 0.2.0 と同じ（1115 が一致）
  - 小数点の判定: 比べた 1241 呼吸すべてで正しかった。100 未満を読んだ 18,492 回で、EasyOCR が小数点を読み落としたことは 0 回だった
  - 0.2.0 と比べて（python -m ventocr_core・並列数 5）: 呼吸の数（1260）・区切り・波形・Breaths.csv のほかの列は同じで、
    変わったのはコンプライアンスの 85 呼吸（10 倍）だけ
  - 解析時間: 0.2.0 が 624.8 秒（ocr 532.5）、0.2.1 が 668.7 秒（ocr 575.2）。差は同じ版を 2 回動かしたときのばらつき
    （0.8.3 の確認では 612.6 / 722.3 秒）の中。小数点の確かめは、全 36,096 フレームで動いたとしても CPU 29 秒（並列 5 で約 6 秒）が上限

## 0.2.0 (2026-10-20)
### 4 段目の圧（Pes / Ptranspulm）を設定で選ぶ
- Hamilton は 4 段目に Pes（食道内圧）か Ptranspulm（経肺圧 = Paw − Pes）を表示できるが、0.1.0 までは常に「Pes」として出力していた
  （Ptranspulm 2026-10-09 の動画の 4 段目は Ptranspulm）
- 解析の設定に post_ocr.ptranspulm（bool、既定オフ = Pes、解析ダイアログの上に出す）を足した。オンなら出力の名前を Ptranspulm にする:
  BreathCSVs（waves.sqlite3）の列、Breaths.csv の Axis_Ptranspulm・Ptranspulm_min・Ptranspulm_max、Frames.csv の Axis_Ptranspulm・
  Axis_Filled_Ptranspulm、fields.txt、scale_meta.json。エンジンの中の処理は名前が違うだけで同じ（core.aux_wave_name）
- パネルの名前の自動判定はしない（ユーザーの判断）。記録の途中で切り替えた動画には対応しない
- 確認（Ptranspulm 2026-10-09 の 600〜720 秒、python -m ventocr_core）: 0.1.0 と 0.2.0（オフ）は Breaths.csv（作業フォルダのパスの列を除く）・
  Frames.csv・波形 34 呼吸・fields.txt・scale_meta.json が同一。オンはオフと値が同じで、名前だけが Ptranspulm
### 出力の宣言（manifest.json の outputs。ENGINE_API.md §8.1、VentOCR 0.6.2 から）
- outputs.waves に Pressure（表示名 Paw）・Flow・Volume・Pes・Ptranspulm を宣言（単位・色・縦軸は Breaths.csv の <頭>_min / <頭>_max）。
  Pes と Ptranspulm は設定でどちらか一方だけが出るが、両方を宣言する（アプリは波形の CSV にある方だけを出す）。色はどちらも Pes の茶色（#8c564b）
- outputs.breath_values に、呼吸プロパティのサマリーの列（breath_summary と同じ順。labels と単位、数の書き方）を宣言
- ventocr_min を 0.5.5 → 0.6.2（outputs を読み、ventocr_min を確かめるアプリ。古いアプリには入らない）
- breath_summary・labels は、outputs を読まないアプリのために残した
- 版を 0.2.0 にした理由: オンにすると出力の列の名前が変わり、0.1.0 以前の結果（同じ動画でも Pes の名前）と列の名前の整合が取れなくなるため。
  アプリ（チャート・書き出し）が Ptranspulm の列に対応するまでは、オンにした解析の 4 段目はチャートに出ない

## 0.1.0 (2026-10-19)
- 0.1.0（旧 v0.8.3 と中身は同じ。版の付け方を 0.X.Y に改めた）

## 0.8.3 (2026-10-19)
### 解析の高速化（グレー変換）
- initial_ocr の時間の約 7 割が、フレームのグレー変換（_to_gray: np.dot を 1920×1080 全体に float64 で計算。1 フレーム 21〜52 ms）だった。
  cv2.cvtColor（0.1〜0.4 ms）にした
- 確認（Ptranspulm 2026-10-09 10-25-03.mov の 1 時間、python -m ventocr_core・並列数 5 で 0.8.2 と 2 回ずつ）:
  解析全体 612.6 / 722.3 秒 → 419.9 / 457.9 秒（平均 -34%）、ocr の段階 -39%。同じ版の 2 回は結果が完全に同じ
- 結果の違い（0.8.2 と比べて）: 呼吸の数・区切り・Trigger・VTE・Ppeak は同じ。cvtColor は四捨五入、np.dot→astype は切り捨てなので画素値が
  最大 1 違い、Compliance が 4 呼吸で変わった（B80 57.7→57.77、B776 51.2→51.22、B1101 17.0→17.7、B1165 空欄→47.0。Study Recorder では
  57.7・51.2・17.7・47.2 で、2 件は悪く 2 件は良くなる）。波形は X 列を除いて 1260 中 1245 呼吸が同じで、15 呼吸は区切りが 1 点前後した。
  ペン先の位置が 144 フレームで最大 6 px 違うため、BreathCSVs の X 列（通しの位置）は全体に数 px ずれる

## 0.8.2 (2026-10-18)
### 動画の始まりに三角があると post_ocr が止まる不具合
- 不具合: 最初のフレームには前のフレームが無いので、画面に残っている三角まで新しい三角と数え、その時刻（ペン先からの距離だけさかのぼる）が
  動画の始まりより前の負の行番号になって `KeyError: '[-2] not in index'` で止まった（0.8.0 から。動画の途中から切り出した解析で見つかった:
  Ptranspulm 2026-10-09 の 600〜720 秒。1時間の解析で止まらなかったのは、最初のフレームにペン先のすぐ後ろの三角が無かったため）
- 修正: 最初のフレームは新しい三角の判断に使わない。三角の時刻は 0 行より前にしない
### engine_api 3（フレームの画像は必要なものだけ）と、10 fps 以外のフレームレート（engine_api 2）
- manifest.json に `engine_api: 3` を入れた（0.8.1 までは項目が無く engine_api 1 扱い）。フレームの画像は extract_waves が
  `cv2.imread(パス)` で必要なものだけを読む（一覧はしない）。2 分の動画で読むのは 40 枚（5・10・30 fps のどれでも。全フレームは 600〜3600 枚）
- 10 fps を前提にしていた値を、秒または掃引の速さからの換算にした
  - 掃引の速さの推定: 隣り合うフレームの検出値の差だけでなく、続けて検出できた値どうしの進み ÷ 間のフレーム数を使う
    （30 fps では1フレームの進みが 1.7px で、検出できるフレームが大きく進んだものに偏り、90 px/秒と見積もってペン先が 20px ずれていた）
  - 画面全体の描き直しとみなす変化の幅（SWEEP_RUN_MAX 40px）は、1フレームの進みの 3 倍より狭くしない（低い fps）
  - 新しい三角とみなすペン先からの距離（最大 40px）は、1フレームの進みの 2 倍 + 4px より狭くしない（低い fps）
  - 描き直された三角の確認: 5 フレーム → 0.5 秒（TRIGGER_KEEP_SEC）
  - VTE とコンプライアンスの変化を1回の更新にまとめる幅: 1 フレーム → 0.1 秒（UPDATE_MERGE_SEC）
  - 設定の stable_frames は 10 fps のときのフレーム数とし、ほかの fps では同じ時間に換算する
- 確認（Ptranspulm 2026-10-09 の 600〜720 秒を 5・10・30 fps にして OCR から波形まで）: どれも 34 呼吸、A/C は全呼吸一致、VTE は 33/34 一致。
  波形の始まりの時刻は 10 fps と比べて 5 fps で中央値 0.04 秒、30 fps で 0.06 秒の差（30 fps は一様に 0.06 秒早いが、開始時刻 = Frame / FPS の
  1 フレーム分の違い）。10 fps の1時間（0.8.0 の解析の OCR）は、ペン先・区切り・A/C・三角の数とも 0.8.1 と同一

## 0.8.1 (2026-10-18)
### 自発の呼吸の区切りを三角に合わせる
- 不具合: 波形の区切りを吸気の始まりへ合わせるとき（_snap_to_breaths）、流量の立ち上がりの足元を探していたため、
  - 前の吸気の流れが続いたまま次の呼吸に入ると立ち上がりが見つからず、表示の更新の 0.5 秒前のまま残った（B94: 0.35 秒早く、Volume が 300 から始まる）
  - 呼気の途中でトリガーされると、呼気の一番下（呼気の始まり）までさかのぼった（B79/80: 前の呼吸の呼気がまるごと次の呼吸に入った。B96: 0.3 秒早く、B95 が Volume が 0 に戻る前に切れた）
- 修正: 自発の呼吸（三角がある）は、三角の頂点の 0.08 秒前で区切る（TRIGGER_SNAP_LEAD_SEC）。三角の位置は post_ocr で
  Frames.csv の Trigger_U（通しの位置）に記録する。強制の呼吸は今までどおり流量の立ち上がり
- 確認（Ptranspulm 2026-10-09、0.8.0 の解析の1時間）: 自発の呼吸 1103 の始まりは三角の 3.8〜4.5px 前にそろった
  （0.8.0 は 0.5〜41.5px 前とばらついていた。335 呼吸が 0.2 秒以上後ろへ）。強制の呼吸 156 の区切りは変わらない。
  B79/80・B95・B96（0.7.3 の B94・B95）は、Volume が 0 に戻る位置で区切られるようになった
### Breaths.csv の P_min / P_max などを描画領域の範囲に
- 0.8.0 までは縦軸の目盛りの最小・最大の数字だった。Hamilton の画面は一番下の目盛りより下にも描画領域がある
  （気道内圧 0〜20 のとき約 -7.9、Volume 0〜800 のとき約 -97 まで）ので、そこに出た波形がアプリのチャートの外に出ていた
  （B109・B111 の Ptranspulm の負の部分。0.8.0 の解析で 1260 呼吸中 479 呼吸）
- 描画領域（画面の縦の軸線 x≈465）の上端・下端の y を実測し（Settings.WAVES[...]["area"]: 気道内圧 121〜317、フロー 346〜542、
  Volume 576〜772、Pes 801〜997。1時間を通して一定）、縦軸の換算式で値にする。目盛りの最小・最大は Axis_<波形> に残っている。追加の処理はほぼ無い

## 0.8.0 (2026-10-18)
### 患者トリガー（自発 / 強制）の判定
- Breaths.csv に `Trigger` を追加: Hamilton が自発トリガーと判定した呼吸は `A`、強制は `C`。
  Abnormal を含み三角が見つからない呼吸は空欄。manifest.json の breath_summary・labels に入れたので、呼吸プロパティのサマリーにも出る
- Hamilton は自発トリガーの呼吸の始まり（吸気の始まり）に、気道内圧の時間軸のすぐ下にピンクの三角（▲、幅約16px・高さ約10px、
  BGR ≈ 86, 6, 248）を描き、波形と一緒に掃引する
  - initial_ocr: 各フレームで三角を探し、頂点の x を Frames.csv の `Trigger_Apexes` に記録する（core.trigger_apexes。約 1ms/フレーム）。
    時間軸は気道内圧の 0 の位置で縦軸のスケールにより上下に動く（0〜40 のとき y≈282〜291、0〜20 のとき y≈266〜275）ので、
    気道内圧の欄（y=100〜315）から色・高さ（7〜16px）・幅（3〜24px）で探す。時間軸の目盛りの数字と重なった三角は
    動画の圧縮で色がにじんで小片に分かれるので、closing（5×5）でつないでから判定する
  - post_ocr: ペン先のすぐ後ろ（0〜40px）に新しく出た三角を「吸気の始まり」とする（core.trigger_events、Frames.csv の `Trigger_New`）。
    画面には1周前（約22秒前）の三角もペン先より右に残っているので、位置で区別する。1周前の三角とほぼ同じ位置に
    描き直された三角（B237・B452）は、ペン先が 8px 以上通り過ぎた後も残っていることで区別する（1周前の三角は消される）
  - その呼吸の間に新しい三角があれば A
### 三角による区切りの追加
- 不具合: 前後の呼吸の VTE・コンプライアンスがたまたま同じ値だと、Hamilton は2呼吸として記録しても画面の表示が変わらず、
  2呼吸が1つにまとめられていた（Ptranspulm 2026-10-09 の B87: Study Recorder の 8899・8900 がどちらも 404 mL / 52.8）
- 修正: 三角の前 0.5秒〜後 1秒に表示の更新が無ければ、三角の 0.3秒後に表示の更新があったものとして区切る
  （表示の更新は三角の 0.14〜0.39 秒後、中央値 0.27 秒）。Alert に TriggerSplit、Frames.csv の `Trigger_Split` = 1。
  Abnormal の近く（前 3秒〜後 1秒）と録画の終わりは、表示の更新が見えていないだけかもしれないので除く。
  強制の呼吸（三角が出ない）どうしが同じ値のときは区切れない
### Abnormal（ポップアップ）を含む呼吸の波形
- 0.7.4 までは Abnormal のフレームを含む呼吸の波形を書き出さなかった。ポップアップの間も Hamilton は裏で波形を描き続けていて、
  ポップアップが消えると1周分がすべて見える。区切りは、ポップアップの間も見えている数値欄の更新で 0.7.3 から求めている
  （B1014〜B1020 は Study Recorder の 9826〜9832 と時刻・VT が一致）
- Abnormal を含む呼吸も波形を読む。列を読む画像がポップアップの最中なら、ポップアップが消えた直後のフレームから4つの波形を読む
  （Breaths.csv の Wave_Popup = Reread）。そのフレームで上書きされていた列（ポップアップが 22 秒以上続いた場合）は、
  ポップアップの間も見えている気道内圧の欄から Paw だけを読む（PawOnly）。途中で画面が消去された場合と、気道内圧の欄も
  見えない（縦軸の目盛りを読めない）場合は空欄。Alert の Abnormal は残す。追加の処理は読み直す呼吸ごとに画像1枚（23 呼吸で 0.3 秒）
- ペン先の追跡（track_sweep）: ポップアップの間も上の気道内圧の欄で掃引を検出できるが、ポップアップの描画も変化として検出され、
  左端からの描き始めと取り違えていた（フレーム 30471）。その後のポップアップの間のペン先がずれ、波形の列の位置が合わなかった。
  Abnormal のフレームでは、左端からの描き始めは右端まで描き終えたはずのときだけ認める。変わったのは Abnormal の 99 フレームだけ
- 患者トリガー: Abnormal でも気道内圧の欄が見えている（縦軸の目盛りを読めた）フレームでは三角を使う（Frames.csv の Paw_Hidden = 0）
- 確認（同じ動画）: Abnormal を含む 11 呼吸のうち、画面が消去された区間の B989 を除く 10 呼吸（B988・B1014〜B1020・B1030・B1031）の
  4つの波形を欠けなく読めた（B989 は 26% 欠け）。空欄だった Trigger のうち 7 呼吸が埋まり、Study Recorder と一致（A）。B988 はポップアップが気道内圧の欄も覆っていたので空欄のまま
  （0.8.1 で訂正: 当初「8 呼吸・C 1」と書いたのは、目盛りを引き継いだ後の Frames.csv から post_ocr をやり直した確認の誤り）。
  隣の B1013・B1021 などは、波形がつながったので区切りを吸気の始まりへ合わせ直した。
  PawOnly は、ポップアップがフレーム 30700 まで続いたことにして確かめた（上書きされていた列は Paw だけ、残っていた列は4つとも）

### 確認（Ptranspulm 2026-10-09 10-25-03.mov の1時間、Study Recorder の MonitorParameters と照合）
- 新しい三角 1105 個。三角の直後には必ず Hamilton の呼吸の記録があった（時計のずれは表示の更新と値で合わせた）
- 区切りが増えたのは B87 の1か所だけ（1259 → 1260 呼吸）。B87 = 404 / 52.8（Hamilton 8900）、新しい B88 = 401 / 40.0（8901）。
  ほかの 1258 呼吸は区切り・測定値とも 0.7.4 と同一
- Trigger と IsPatientTriggeredBreath: A 1103 呼吸はすべて自発（誤り 0）、C 156 呼吸のうち 5 呼吸（B541・B854・B911・B990・B991）は
  記録では自発（三角を見つけられなかった）
- 三角の無い Hamilton_Sample.mov（316 フレーム）では誤検出 0

## 0.7.4 (2026-10-17)
### バージョン番号の整理（解析の処理は 0.7.3 と同一）
- 独立したリポジトリ（VentOCR_Hamilton）として公開するにあたり、バージョンを 0.7.4 にした
- manifest.json の version が 0.7.0 のまま残っていたので、core.py の ENGINE_VERSION とそろえた

## 0.7.3 (2026-10-13)
### Abnormal の間も、数値欄の更新で呼吸を区切る
- 不具合: Abnormal（波形の縦軸が読めない。ポップアップなど）のフレームでは数値欄の変化をすべて無視して前の値を引き継いでいたため、
  - その間の呼吸が1つにまとめられた（Ptranspulm 2026-10-09 の B1012: 11:15:40〜53 の約13秒・Study Recorder の6呼吸）
  - Abnormal の間に起きた更新を、Abnormal が終わったフレームで検出し、区切りが遅れた（B987: 3秒、B1013、B1024）
- 修正: Abnormal のフレームでも、数値欄が読めていればその値で更新を検出する。数値欄までポップアップに覆われたときに
  空欄が続いて偽の区切りにならないよう、Abnormal のフレームの空欄（読めない）だけは前の値を引き継ぐ。
  測定値の割り当て（表示の更新の区間）でも Abnormal のフレームの値を使う。Abnormal の呼吸の波形は、これまでどおり書き出さない
- 確認（同じ動画）: 1253 → 1259 呼吸。増えた区切り 9 か所はすべて Abnormal の区間。B1012 の約15秒は 7 呼吸
  （VTE 311 → 446 → 155 → 176 → 492 → 431 → 562 と、各更新の値）。区切りが動いたのは B987（30046 → 30016）・
  旧 B1013（30593 → 30582）・旧 B1024（30830 → 30780）の3か所で、いずれも Abnormal の間の実際の更新の位置。
  区切りが変わらなかった 1247 呼吸の VTE・コンプライアンス・Ppeak は 0.7.2 と同一

### manifest.json の total_breaths（アプリ側）
- 以前は「波形CSVを書き出した呼吸の数」で、Abnormal の呼吸（波形なし）の分だけ Breaths.csv の行数より少なかった（1246 と 1253）。
  total_breaths = Breaths.csv の行数にし、波形を書き出した呼吸の数は breaths_with_waves に分けた

## 0.7.2 (2026-10-13)
### VTE が 1000 mL 以上のとき、先頭の「1」を読み落として 1000 mL ずれる不具合
- 原因: VTE の読み取り範囲が x=185〜385（3桁分）で、4桁になると先頭の「1」（x≈140）が範囲の外だった。
  検証（Ptranspulm 2026-10-09 10-25-03.mov）: B246、383、646、1212、1213、1216 の6呼吸（1300→300、1083→83 など）
- 修正: VTE の読み取り範囲の左端を x=110 に広げた（アラーム上下限の数字 700 / 250 などは x≈80 まで）
- ほかの左の値（コンプライアンスなど）も広げて試したが、コンプライアンスの読み取りがわずかに悪くなった
  （空欄 36→41 呼吸、小数2桁の誤読 10→13 呼吸）ので、VTE だけを広げた
- 確認（同じ動画、1253 呼吸）: 変わったのは上の6呼吸の VTE（と次の呼吸の VTE_displayed）だけで、すべて正しい4桁の値になった。
  呼吸の区切り・コンプライアンス・Ppeak は 0.7.1 と同一

### 設定の記録
- `post_ocr.min_breath_sec` は 0.7.0 で廃止済み（使っていない）。解析ダイアログが以前のバージョンの設定を覚えていて、
  解析に渡され manifest.json に残っていた。アプリ側で、エンジンが知らない設定項目（schema.py にも engine_options.json にも無いもの）は
  解析に渡さないようにした（ventocr_core.pipeline.known_engine_options）。manifest.json の engine_options は、実際に使われる設定だけになる

## 0.7.1 (2026-10-13)
### 次の呼吸が 1 秒以下だと、測定値（VTE など）が1つ前の呼吸の値になる不具合
- 原因: 呼吸 i の測定値を「次の呼吸の区間の最頻値」で決めていた。次の呼吸の区間は表示の更新の 0.5 秒（boundary_shift_sec）前から
  始まるので、次の呼吸が 1 秒以下（10 フレーム以下）だと更新前の（前の呼吸の）値の方が多くなっていた。
  検証（Ptranspulm 2026-10-09 10-25-03.mov）: ずれていた 39 呼吸のうち 31 呼吸がこれ。次の呼吸が 1.0 秒以下の 38 件中 35 件で起きていた
  （例: B990。表示の更新は 30107 フレームで 152、30135 フレームで 450。正しい 450 ではなく 152 が入り、450 は B991 に入っていた）
- 修正: 呼吸 i の測定値 = 呼吸 i+1 を始めた表示の更新（Frames.csv の Display_Update=1）から、その次の更新までの間に表示されていた値
  （最頻値。同数なら後に表示されていた値）。次の呼吸の長さによらない
- `*_displayed` も同じ区間で決める: 呼吸 i の `*_displayed` = 呼吸 i を始めた更新で表示された値（= 呼吸 i-1 の値）。
  以前はずらした区切りの区間（更新の前を含む）の最頻値だった
- 換気モード・設定値（Mode / Set_*）は変更なし（その呼吸の間に表示されていた値）
- 確認（resource/Hamilton_Sample.mov）: 37 呼吸。36 呼吸すべてで、VTE が更新の区間の値と一致し、VTE(i) = VTE_displayed(i+1)

## 0.7.0 (2026-10-13)
### 呼吸の区切りを「2フレーム持続した変化」に（時間で間引かない）
- 1呼吸 = VTE かコンプライアンスの新しい値が `post_ocr.stable_frames`（既定 2）フレーム以上続いた変化から、次の変化まで。
  区切りはその値が出た最初のフレーム（から boundary_shift_sec）
  - 1フレームだけの値は無視（A→B(1)→C なら C の始まり、A→B(1)→A なら変化なし）
  - 空欄（「---」・読めない）も1つの値として扱う
  - 2つの値の変化が1フレーム差なら同じ1回の更新（表示の書き換えが2フレームにまたがった）
  - Abnormal のフレームの間の変化は使わない（前の値を引き継ぐ）
- `post_ocr.min_breath_sec`（1秒より近い更新をまとめていた）を廃止
- 確認（Ptranspulm 2026-10-09 10-25-03.mov、1時間）: 呼吸 1230 → 1253。1秒未満の呼吸が 30（最短 0.4 秒、29 は波形あり）。
  その多くは 50〜55分ごろで、VTE とコンプライアンスが同じフレームで、はっきり読める値に 0.7〜1.7 秒ごとに更新されていた
  （呼吸器が数えた短い呼吸。0.6 までは 1秒の間引きで捨てていた）。気道内圧の最大 / Ppeak は中央値 1.00（0.98〜1.02）で変わらず。
  コンプライアンスの空欄は 26 区間（計約 110 秒）
- 確認（resource/Hamilton_Sample.mov）: 37 呼吸、区切りの位置も 0.6 と同じ

## 0.6.0 (2026-10-12)
### 掃引を毎フレーム追跡する（Ptranspulm 2026-10-09 の B243）
- 不具合: 「一定の速さで 22秒ごとに x=1567 で左端に戻る」モデルを動画全体に当てはめ、前後30秒でならしていた。
  B243 では画面操作などで波形の描画が約1.1秒止まり（消去帯だけが進む）、画面全体が消去されて x≈608 から左端に戻った（フレーム 8293）。
  モデルがその前後でずれ、別の時刻の列や消去された空白を切り出して、チャートの波形がでたらめになった。
  また 1分ごとの区間の境目でフレームの時刻がずれ、掃引の位置が 10px ほど飛ぶ（この動画で 20 か所）のも、モデルがならしていた
- 毎フレームの検出値を、前の位置から自然に進んだもの・左端から描き始めたものだけ採用して追跡する（track_sweep）。
  画面全体の描き直し（縦軸の自動変更など、変化した列が 40 列を超えるフレーム）は使わない（Frames.csv に Sweep_Run）。
  左端に戻るたびに周を数え（Sweep_Pass）、右端まで描いて戻った（Sweep_Wrap=1）か途中で消去されて戻った（=2）かを記録する
- 波形の各列は、その列を描いた周の最後のフレーム（呼吸の終わりまで）から読む。途中で消去されて戻った呼吸も、戻る前の部分は消去の直前のフレームから読める。
  描画が止まっていた間は空欄。3割以上が空欄の呼吸は Alert に Partial（Breaths.csv に Wave_Missing）
- 縦軸の範囲を超えて描画領域の端で切れた波形（Settings.WAVES の plot）と途中の空欄は、その後縦軸が広がったフレームで、
  列がまだ画面に残っていれば読み直す（Breaths.csv の Wave_Reread）。読み直せずに切れたままなら Alert に Clipped
- 確認（B243 の前後 90 秒の切り出し）: 途中で消去されて戻ったのを 1 回検出。B243 は波形の 95% が空欄（描画が止まっていたため）で Partial。
  縦軸が広がる前に切れていた呼吸を読み直し、ボリューム 800 → 1188 mL、フロー -91 → -105 L/min。
  抽出した値と、後のフレームの画面から読み直した値の差は中央値で Pressure 0.05 / Flow 0.45 / Volume 2.3 / Pes 0.1
- 確認（resource/Hamilton_Sample.mov）: 37 呼吸。各列の値は後のフレームの画面と一致（0.5 と同じ）。
  呼吸の開始時刻は 0.5 と最大 0.1 秒違う（ペン先を各フレームの検出値で決めるため）

## 0.5.0 (2026-10-12)
### 縦軸のスケールを呼吸ごとに読む
- 不具合: Hamilton は波形ごとに縦軸のスケールを頻繁に自動で変える（Ptranspulm 2026-10-09 の1時間で15通り）のに、
  0.4 までは固定の目盛り（Paw 0〜40 など）で換算していた。さらに目盛りの列が基準画像と違うフレームを Abnormal にしていたため、
  スケールが変わった区間では表示の更新で区切られず、22秒の1呼吸（Abnormal,Long）になり波形も出なかった（B251）
- 縦軸の目盛りの数字を1文字ずつ見本（axis_glyphs.npz）と照合して読み、波形ごとに「数字の y → 値」を直線で当てはめる（axis_scale.py）。
  見本はこの動画の14通りの目盛りから切り出した文字を、形でまとめて目視で判定して作った（「9」は無い）
- 呼吸の波形は、切り出した最後のフレームの換算式で物理量にする。Breaths.csv に Axis_<波形>（"a;b;最小;最大"）、P_min / P_max などは目盛りの最小・最大
- Abnormal = 目盛りが読めた波形が3つ未満（ポップアップ・別の画面）。1つだけ読めないときは直前の目盛りを引き継ぎ、Alert に Axis?
- 基準画像（axis_reference.png）と post_ocr.abnormal_screen_diff を削除。Pes の帯を y990 まで（-40 の目盛りがあると y≈977 まで描かれる）
- 確認（Ptranspulm 2026-10-09 10-25-03.mov、1時間）:
  - 呼吸 1137 → 1230。Abnormal,Long 8 → 0、Abnormal 15 → 6（残りは目盛りにポップアップが重なった区間）。B251 は7呼吸に分かれた
  - 気道内圧の最大 / Ppeak（表示値）: 中央値 2.02 → 1.00（10〜90% 0.98〜1.02。スケール 20・40 とも 1.00）
  - Flow の吸気の積分 / ボリュームの増加: 中央値 0.99（±45: 0.99、±75: 0.99、±150: 0.97）
  - ボリュームの最大 / VTE: 中央値 1.04。約2割の呼吸で 1.5 以上だが、吐き切る前に次の吸気が来た呼吸（吸った量 > 吐いた量）で、0.4 と同じ（換算の問題ではない）
- 確認（resource/Hamilton_Sample.mov）: 37 呼吸、全フレームで目盛りを読めた（40 / ±75 / 800 / 0〜20）。波形の値は 0.4 と同じ

## 0.4.0 (2026-10-12)
### OCR する値を選べるようにした
- 読み取れる値: 呼気VT（VTE）、コンプライアンス、ピーク圧（Ppeak）、分時換気量（MinVol）、P0.1（P01）、
  換気モード（Mode、文字）、設定値の呼吸回数（Set_Rate）・Δ吸気圧（Set_Pinsp）・PEEP/CPAP（Set_PEEP）・酸素濃度（Set_Oxygen）
- 設定 `initial_ocr.ocr_keys`（既定: VTE・Compliance・Ppeak）で選ぶ。呼吸の区切りに使う値（`post_ocr.breath_trigger`）は常に読む。
  0.3 以前の `skip_keys` は除外として引き続き効く
- 読み取り位置と読み方は Settings.OCR_FIELDS（kind: measured = 次の呼吸の間の表示がその呼吸の値、*_displayed も出す /
  setting = その呼吸の間の表示）。右の丸の設定値は、単位（cmH2O の「2」）を読まないよう数字の部分だけを囲む
- 確認（resource/Hamilton_Sample.mov、37 呼吸、全項目）: Ppeak 23、MinVol 7.0/7.1、Mode PCV+、Set_Rate 14、Set_Pinsp 15、
  Set_PEEP 8、Set_Oxygen 40 をすべての呼吸で読めた（最後の呼吸の測定値は次の表示が無いため空）。
  P0.1 はこの動画では「---」（測定なし）のため空で、数値の読み取りは未確認。
  既定の3項目で解析した結果の VTE・Compliance・Ppeak・呼吸の区切りは、全項目のときと同一

## 0.3.0 (2026-10-11)
### 波形の点の選び方（NAVA v1.3 と同じ方式）
- 以前は各列の「その色の画素の y の中央値」。掃引が遅く（50 px/秒）、Flow の吸気→呼気の切り替わりなど縦線のある列では
  線の真ん中を取るため、V字の先端（呼気の最大流量）が浅くなっていた（偽のスパイクは出ない）
- 列ごとに「線が通った範囲」を取り、隣の列とつながる線を全体で最適に選び、範囲の中でなめらかな経路を引き、
  局所的に突き出す先端は固定する（trace_columns。px 単位の値は 1280x720 基準で、画面の高さ/720 倍して使う）
- 確認（resource/Hamilton_Sample.mov、37 呼吸）: Flow の呼気の谷が中央値 2.1 L/min（最大 3.4 L/min）深くなった。
  Pressure・Volume・Pes の最大・最小はほぼ変わらない。計算は1呼吸あたり約 11 ms

## 0.2.0 (2026-10-11)
### 波形の区切りを吸気の始まりへ合わせる
- 1呼吸 = 吸気の始まり 〜 次の吸気の始まり（NAVA v1.3 の「呼吸の範囲への切り詰め」と同じ考え方）。
  Flow の吸気の立ち上がり（4 L/min 超が 0.06 秒以上続き、12 L/min 以上に届き、その前に吸気でない区間が 0.06 秒以上）を見つけ、
  立ち上がりの足元（最大値の 20% を超えた点から下がり続ける所までさかのぼる）の 0.04 秒前を区切りにする（`_snap_to_breaths`）
- この機種は呼吸どうしの波形が時間の上で隙間なくつながっているので、NAVA のように削るのではなく、
  区切りを「元の区切りに最も近い吸気の始まり」へ動かし、点を前後の呼吸の間で移す（データは消えない）。
  前後の呼吸に波形が無い端だけ、吸気の始まりより前と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）を削る
- 呼吸の Frame / End_Frame（表示の更新による区切り）と OCR の値は変えない。変わるのは波形 CSV と Wave_T0 / Wave_Rate。
  動かした点の数は Breaths.csv の Wave_Trim_Head / Wave_Trim_Tail（正 = 削った / 渡した、負 = 受け取った）
- 区切り直した呼吸の Wave_Rate は、両端の点の時刻から求め直す（受け取った点は元の呼吸の速さで時刻が決まっているため）
- 設定 `post_ocr.trim_to_breath`（既定 ON）で元の動作に戻せる
- 確認（resource/Hamilton_Sample.mov）: 37 呼吸。区切りは中央値 0.30 秒（0〜0.46 秒）後ろへ動いた（表示の更新の 0.5 秒前の区切りは、吸気の始まりより約 0.3 秒早かった）。隣の呼吸との 0.05〜0.5 秒の隙間 9 → 0 か所、重なりなし

## 0.1.0 (2026-10-11)
- 初版。サンプル: resource/Hamilton_Sample.mov（1920x1080、PCV+、2026-10-07 収録、158秒・37呼吸）
- 呼吸の区切り = コンプライアンス / VTE の表示の更新（-0.5秒）
- OCR: VTE、Compliance
- 波形: Pressure（Paw）、Flow、Volume、Pes。wrap around する掃引に対応
