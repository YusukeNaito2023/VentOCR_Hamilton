# Hamilton Engine Settings
# 座標はすべて基準解像度（1920x1080）の画面座標。入力フレームはこの解像度に正規化してから使う。
# 基準画像: layout.jpg（resource/Hamilton_Sample.mov, 2026-10-07 収録の「PCV+」画面）

BASE_RESOLUTION = (1920, 1080)  # (width, height)

# ------------------------------------------------------------
# OCR する値（列名 → 読み取り位置 (x, y, w, h) と読み方）
# ------------------------------------------------------------
# kind:   "measured" = 左の測定値。直前の呼吸の値なので、その呼吸の値 = 次の呼吸の間に表示される値（*_displayed も残す）
#         "setting"  = 換気モード・右の丸の設定値。その呼吸の間に表示されていた値
# type:   "int" = 整数、"dec1" = 小数1桁（小数点を読み落としたら dot_min_digits 桁以上のとき最後の1桁の前に補う）、
#         "text" = 文字（換気モード）
# 右の丸の設定値は、単位（cmH2O の「2」）を読まないよう数字の部分だけを囲む。
# VTE は左端を x=110 まで広げる（0.7.2。1000 mL 以上で4桁になると先頭の「1」が x≈140 に出る。
# アラーム上下限の数字（700 / 250 など）は x≈80 まで）。ほかの値も広げると、コンプライアンスの読み取りがわずかに悪くなったので広げない
# どれを OCR するかは engine_options の initial_ocr.ocr_keys で選ぶ（呼吸の区切りに使う breath_trigger の値は常に読む）。
DIGITS = "0123456789."
TEXT_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-/() "
OCR_FIELDS = {
    "VTE":        {"roi": (110, 435, 275, 100), "kind": "measured", "type": "int", "unit": "mL",
                   "label": "呼気VT（換気量）"},
    "Compliance": {"roi": (160, 605, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 3,
                   "unit": "mL/cmH2O", "label": "コンプライアンス"},
    "Ppeak":      {"roi": (185, 95, 200, 100), "kind": "measured", "type": "int", "unit": "cmH2O",
                   "label": "ピーク圧"},
    "MinVol":     {"roi": (160, 265, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 2,
                   "unit": "L/min", "label": "分時換気量"},
    "P01":        {"roi": (160, 775, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 2,
                   "allow": DIGITS + "-", "unit": "cmH2O", "label": "P0.1"},
    "Mode":       {"roi": (160, 18, 240, 55), "kind": "setting", "type": "text", "allow": TEXT_CHARS,
                   "label": "換気モード（設定）"},
    "Set_Rate":   {"roi": (1768, 186, 60, 40), "kind": "setting", "type": "int", "unit": "b/min",
                   "label": "呼吸回数（設定）"},
    "Set_Pinsp":  {"roi": (1768, 350, 60, 35), "kind": "setting", "type": "int", "unit": "cmH2O",
                   "label": "Δ吸気圧（設定）"},
    "Set_PEEP":   {"roi": (1768, 512, 60, 34), "kind": "setting", "type": "int", "unit": "cmH2O",
                   "label": "PEEP/CPAP（設定）"},
    "Set_Oxygen": {"roi": (1768, 674, 60, 32), "kind": "setting", "type": "int", "unit": "%",
                   "label": "酸素濃度（設定）"},
}
DEFAULT_OCR_KEYS = ["VTE", "Compliance", "Ppeak"]
ROIs = {key: spec["roi"] for key, spec in OCR_FIELDS.items()}  # 以前の名前（互換のため）

# ------------------------------------------------------------
# 波形の縦軸の目盛り（axis_scale.py）
# ------------------------------------------------------------
# Hamilton は波形ごとに縦軸のスケールを頻繁に自動で変えるので、呼吸ごとに目盛りの数字を読んで換算する。
# 目盛りが読めないフレーム（ポップアップ・別の画面）は Abnormal。
# 0.4 以前は目盛りの列を基準画像と比べて「違えば Abnormal」にしていたため、スケールが変わると Abnormal になり、
# その間の表示の更新で区切られず、波形も出なかった（Ptranspulm 2026-10-09 の B251）。
AXIS_LABEL_X = (414, 462)     # 目盛りの数字の列（実測 x=421〜458。右寄せで「1600」「-150」も入る。左のスクロールバー x≈399〜409 と右の軸線は含めない）
AXIS_PANELS = {               # 波形ごとの目盛りの数字の縦の範囲
    "Pressure": (95, 300),
    "Flow": (318, 548),
    "Volume": (550, 775),
    "Pes": (778, 1000),
}
AXIS_GLYPH_FILE = "axis_glyphs.npz"   # 文字の見本
AXIS_BIN_THRESH = 150         # 数字は黒地に白。これより明るい画素を文字とする（灰色のスクロールバーを除く）
AXIS_TEXT_HEIGHT = (10, 16)   # 1行の文字の高さ（実測 12〜14px）
AXIS_MATCH_MIN = 0.85         # 見本との相関がこれ未満の文字は読めない
AXIS_FIT_TOL_PX = 2.0         # 目盛りの位置が直線からこれ（px）以上外れたら、その波形は読めない
AXIS_MIN_PANELS = 3           # 目盛りが読めた波形がこれ未満のフレームは Abnormal（ポップアップ・別の画面）。
                              # 1つだけ読めない（見本に無い「9」など）ときは、その波形は直前に読めた目盛りを引き継ぐ

# ------------------------------------------------------------
# 波形の描画領域と掃引
# ------------------------------------------------------------
# 掃引は左端 X_LEFT から右へ描き、X_RIGHT に達すると X_LEFT に戻って古い波形を上書きする（wrap around）。
# 画面操作などで描画が止まり、画面全体が消去されて途中から左端に戻ることもある（core.track_sweep）。
# 横軸は 50 px/秒、1周 1100 px = 22 秒（目盛り 0秒 = x467, 5秒ごとに 250 px）。
SWEEP_X_LEFT = 467
SWEEP_X_RIGHT = 1567          # この列は描かれない（[X_LEFT, X_RIGHT) が1周）
SWEEP_PX_PER_SEC = 50.0       # 掃引の速さの既定値（実際の値は動画から推定する）
SWEEP_ERASE_OFFSET = 3        # 前フレームとの差分の右端 − ペン先（ペン先の前を消している帯の幅）
SWEEP_DIFF_ROWS = (95, 960)   # 掃引位置の検出に使う縦の範囲（4つの波形全体）
SWEEP_DIFF_THRESH = 40        # グレースケールの差がこれを超えた画素を「変化あり」とする
SWEEP_DIFF_MIN_PIXELS = 3     # 1列の中で変化した画素がこの数以上なら、その列は変化あり
SWEEP_RUN_MAX = 40            # 変化した列のまとまりがこれより広いフレームは画面全体の描き直し（縦軸の自動変更など）で、掃引位置に使わない
SWEEP_WRAP_JUMP = 100         # ペン先がこれ以上左に戻ったら次の周（右端まで描いて戻った、または途中で消去されて戻った）
SWEEP_RESTART_MARGIN = 15     # 左端から描き始めた / 右端まで描いた とみなす余裕（px）
SWEEP_REREAD_SEC = 22.0       # 縦軸の範囲を超えて切れた波形を、縦軸が広がったフレームから読み直すときに探す時間（1周分）

# ------------------------------------------------------------
# 患者トリガーのマーク（0.8.0）
# ------------------------------------------------------------
# 患者の吸気努力で始まった呼吸（自発トリガー）には、気道内圧の時間軸のすぐ下にピンクの三角（▲、幅約16px・高さ約10px）が
# 吸気の始まりの位置に描かれ、波形と一緒に掃引される。時間軸は気道内圧の 0 の位置なので、縦軸のスケールで上下に動く
# （0〜40 のとき y≈282〜291、0〜20 のとき y≈266〜275。Ptranspulm 2026-10-09 で実測）。
# 色は BGR ≈ (86, 6, 248)、色相 ≈ 170〜175（フローの線と同じ色だが、フローの帯 y≥325 より上にある）。
# 三角は Hamilton が自発トリガーと判定した呼吸（Study Recorder の IsPatientTriggeredBreath = 1）の始まりにだけ出る。
TRIGGER_ROWS = (100, 315)     # 三角を探す縦の範囲（気道内圧の欄。フローの欄より上）
TRIGGER_HUE = 172
TRIGGER_HUE_TOL = 8
TRIGGER_SAT_MIN = 120
TRIGGER_VAL_MIN = 120
TRIGGER_CLOSE = (5, 5)        # 小片をつなぐ closing の大きさ（高さ, 幅）
TRIGGER_HEIGHT = (7, 16)      # 三角の高さ（px）。警報の上限の赤い線（高さ 1〜2px）は含めない
TRIGGER_WIDTH = (3, 24)       # 三角の幅（px）。左端で切れたもの・消去帯で一部消えたものも含める
TRIGGER_APEX_ROWS = 2         # 三角の頂点 = 上から この行数の画素の x の平均
# 新しく描かれた三角 = 前のフレームに無く（±TRIGGER_SAME_PX）、ペン先の TRIGGER_NEW_LAG_PX の範囲（ペン先 − 頂点）にあるもの。
# 三角は描かれた時点でペン先の約4px 後ろにある。画面には1周前（約22秒前）の三角もペン先より右に残っているので、
# 位置で新しいものだけを選ぶ（Ptranspulm 2026-10-09: 新しい三角 1090 個のうち 1089 個の直後に Hamilton の呼吸の記録があった）
TRIGGER_SAME_PX = 2
TRIGGER_NEW_LAG_PX = (-5, 40)
TRIGGER_DEDUP_SEC = 0.3       # これより近い2つの三角は同じもの
TRIGGER_KEEP_PX = 8           # 1周前の三角と同じ位置に描き直された三角は、ペン先がこれだけ先へ進んだ後も残っていること
TRIGGER_KEEP_FRAMES = 5       # その確認に見るフレーム数

# 三角による区切りの追加（0.8.0）: 表示の更新は三角の 0.14〜0.39 秒後（中央値 0.27 秒）に起きる。
# 前後の呼吸で VTE・コンプライアンスがたまたま同じ値だと表示が変わらず区切れない（Ptranspulm 2026-10-09 の B87）ので、
# 三角の前 TRIGGER_UPDATE_BEFORE_SEC 〜 後 TRIGGER_UPDATE_AFTER_SEC に表示の更新が無ければ、
# 三角の TRIGGER_UPDATE_LAG_SEC 後に更新があったものとして区切る。
# Abnormal（ポップアップなど）の近く（前 TRIGGER_ABNORMAL_BEFORE_SEC 〜 後 TRIGGER_UPDATE_AFTER_SEC）と、録画の終わりは除く
TRIGGER_UPDATE_LAG_SEC = 0.3
TRIGGER_UPDATE_BEFORE_SEC = 0.5
TRIGGER_UPDATE_AFTER_SEC = 1.0
TRIGGER_ABNORMAL_BEFORE_SEC = 3.0

# ------------------------------------------------------------
# 波形（CSV の列名 → 画面上の帯・色・目盛り）
# ------------------------------------------------------------
# hue は OpenCV の HSV（0-179）。換算は呼吸ごとに読んだ目盛り（axis_scale.py）で行う。
# ticks / axis は Hamilton_Sample.mov の既定のスケール（目盛りが読めないときの換算と、scale_meta の既定）。
# plot は描画領域の上端・下端の y（2026-10-12、Ptranspulm 2026-10-09 で実測）。上端は目盛りの最大値の位置、
# 下端は目盛りの最小値より下まである。縦軸の範囲を超えた波形はここで切れる（core._clipped）。
# Paw は既存エンジン・アプリとの互換のため列名を "Pressure" とする。
WAVES = {
    "Pressure": {  # 気道内圧（黄）
        "band": (100, 285), "hue": 29,
        "ticks": [(122, 40), (199, 20), (278, 0)],
        "unit": "cmH2O", "scale_prefix": "P", "axis": (0, 40),
        "plot": (123, None),   # 描画領域の上端・下端の y（縦軸の範囲を超えるとここで切れる。None は判定しない）
    },
    "Flow": {      # フロー（赤）
        "band": (325, 545), "hue": 171,
        "ticks": [(346, 75), (376, 50), (405, 25), (434, 0), (464, -25), (493, -50), (522, -75)],
        "unit": "L/min", "scale_prefix": "F", "axis": (-75, 75),
        "plot": (346, 542),
    },
    "Volume": {    # ボリューム（緑）
        "band": (560, 770), "hue": 58,
        "ticks": [(575.5, 800), (620, 600), (663, 400), (707, 200), (751, 0)],
        "unit": "mL", "scale_prefix": "V", "axis": (-100, 800),  # 呼気の終わりに 0 を下回る
        "plot": (576, None),   # 下端（y769）でも 0 を下回る部分がよく切れるが、呼気の終わりのわずかな負の値なので判定しない
    },
    "Pes": {       # 食道内圧（橙）
        "band": (785, 990), "hue": 7,   # 目盛りが -40 まであると y≈977 まで描かれる
        "ticks": [(800, 20), (871, 10), (941, 0)],
        "unit": "cmH2O", "scale_prefix": "Pes", "axis": (0, 20),
        "plot": (800, 989),
    },
}
WAVE_HUE_TOL = 12     # 色相の許容差（循環距離）
WAVE_SAT_MIN = 60     # これ未満の彩度は背景・灰色の軸線とみなす
WAVE_VAL_MIN = 60     # これ未満の明度は背景とみなす
WAVE_GAP_FILL = 6     # この幅（px）以下の欠けは直線で補間する
