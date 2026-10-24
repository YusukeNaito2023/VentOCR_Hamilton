"""Hamilton（C6 / G5 系の画面）VentOCR Engine

- 1呼吸 = 左側の数値（コンプライアンス・呼気VT）の表示が更新されてから、次に更新されるまで
  （更新 = 新しい値が2フレーム以上続いたこと。空欄も1つの値。時間では間引かない）
- OCR: 呼気VT（VTE）、コンプライアンス、ピーク圧、分時換気量、P0.1、換気モード、設定（呼吸回数・Δ吸気圧・PEEP・酸素濃度）
  のうち engine_options の initial_ocr.ocr_keys で選んだもの（既定: VTE・コンプライアンス・ピーク圧。Settings.OCR_FIELDS）
- 波形: 気道内圧（Pressure）、フロー（Flow）、ボリューム（Volume）、食道内圧（Pes）。
  縦軸のスケールは頻繁に自動で変わるので、呼吸ごとに目盛りの数字を読んで換算する（axis_scale.py）

波形の画面は左から右へ一定の速さで掃引し、右端に達すると左端に戻って古い波形を上書きする（wrap around）。
各フレームで「前のフレームから変化した列」の右端を掃引位置として記録し（initial_ocr）、
動画全体から掃引の速さと位相を推定して、各フレームのペン先の位置を決める（post_ocr）。
呼吸の波形は、その呼吸の最後のフレームの画像から、開始時のペン先〜終了時のペン先の列を切り出す（extract_waves）。
"""
import json
import os
import re

import cv2
import numpy as np
import pandas as pd
from tqdm import tqdm

from . import Settings
from . import axis_scale

ENGINE_VERSION = "0.2.1"
VENTOCR_TESTED = "0.5.5"

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_KEYS = list(Settings.OCR_FIELDS)
TEXT_KEYS = [k for k, f in Settings.OCR_FIELDS.items() if f["type"] == "text"]
MEASURED_KEYS = [k for k, f in Settings.OCR_FIELDS.items() if f["kind"] == "measured"]


def selected_ocr_keys(settings):
    """OCR する値: ocr_keys で選んだもの + 呼吸の区切りに使う値（breath_trigger）。skip_keys（0.3 以前の設定）は除く。"""
    init = settings.get("initial_ocr", {})
    chosen = set(init.get("ocr_keys", Settings.DEFAULT_OCR_KEYS))
    chosen |= set(settings.get("post_ocr", {}).get("breath_trigger", ["Compliance", "VTE"]))
    chosen -= set(init.get("skip_keys", []))
    return [k for k in OCR_KEYS if k in chosen]


# ============================================================
# EasyOCR初期化（V600_ventocr と同じ）
# ============================================================

_EASYOCR_LOCKFILE = "easyocr_init.lock"


def _get_easyocr_model_dir():
    model_dir = os.path.join(".", "_runtime_models", "easyocr")
    os.makedirs(model_dir, exist_ok=True)
    return model_dir


def _easyocr_model_ready(model_dir):
    from pathlib import Path
    p = Path(model_dir)
    if not p.exists():
        return False
    patterns = ["craft*.pth", "dbnet*.pth", "latin*.pth", "english_g2*.pth", "standard*.pth"]
    return any(list(p.glob(pat)) for pat in patterns)


def _acquire_easyocr_file_lock(model_dir, timeout=300, poll=0.5):
    import time
    lock_path = os.path.join(model_dir, _EASYOCR_LOCKFILE)
    start = time.time()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            return fd, lock_path
        except FileExistsError:
            if (time.time() - start) > timeout:
                raise TimeoutError(f"Timeout waiting for EasyOCR lock: {lock_path}")
            time.sleep(poll)


def _release_easyocr_file_lock(fd, lock_path):
    try:
        os.close(fd)
    except Exception:
        pass
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception:
        pass


def _ensure_easyocr_reader_ready(gpu_enabled):
    from easyocr import Reader
    model_dir = _get_easyocr_model_dir()
    if _easyocr_model_ready(model_dir):
        return Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)

    fd, lock_path = _acquire_easyocr_file_lock(model_dir)
    try:
        if not _easyocr_model_ready(model_dir):
            Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)
    finally:
        _release_easyocr_file_lock(fd, lock_path)

    return Reader(["en"], gpu=gpu_enabled, model_storage_directory=model_dir, download_enabled=True)


# ============================================================
# 共通
# ============================================================

def _normalize_frame(frame):
    """基準解像度（1920x1080）にそろえる。"""
    w, h = Settings.BASE_RESOLUTION
    if frame.shape[1] != w or frame.shape[0] != h:
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    return frame


def _to_gray(frame):
    # 0.8.3: cv2.cvtColor（0.8.2 までは np.dot を画面全体に float64 で計算し、initial_ocr の時間の約 7 割を占めていた。
    # 四捨五入と切り捨ての違いで画素値が最大 1 違う）
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _crop(img, roi):
    x, y, w, h = roi
    return img[y:y + h, x:x + w]


def _runs(mask):
    """True が続く区間の (開始, 終了) のリスト（終了を含む）。"""
    idx = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _format_time(frame, fps):
    """フレーム番号からHH:MM:SS:CC形式のタイムスタンプを生成"""
    total_seconds = frame / fps
    s = int(total_seconds)
    centiseconds = int((total_seconds - s) * 100)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}:{centiseconds:02d}"


# ============================================================
# OCR結果パース
# ============================================================

def _has_dot(roi):
    """ROI（グレー）に小数点があるか: 数字の下端の近くにある小さな塊を探す。数字が見つからなければ None。

    文字は白（≈255）か赤（グレーで ≈76）、背景は黒なので、しきい値 40 で二値化する。
    """
    _, bw = cv2.threshold(np.asarray(roi, np.uint8), 40, 255, cv2.THRESH_BINARY)
    n, _, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    st = st[1:]
    big = st[st[:, cv2.CC_STAT_HEIGHT] >= 0.4 * bw.shape[0]]     # 数字（ROI の高さの 4 割以上）
    if len(big) == 0:
        return None
    bottom = (big[:, 1] + big[:, 3]).max()
    h = bottom - big[:, 1].min()          # 数字の高さ（1080p で約 80 px、小数点は約 14 px 角）
    for x, y, w, hh, a in st:
        if 0.08 * h <= w < 0.3 * h and 0.08 * h <= hh < 0.3 * h and y + hh >= bottom - 0.15 * h:
            return True
    return False


def _parse_ocr(result, key, roi=None):
    """EasyOCR の結果 → (値の文字列, 生の文字列, 信頼度の文字列)。

    読み方は Settings.OCR_FIELDS の type: int = 整数、text = 文字（換気モード）、
    dec1 = 小数1桁で表示されるが小数点を読み落とすことがあるので、小数点が無い dot_min_digits 桁以上の数字は
    最後の1桁の前に小数点を補う（生の文字列は *_raw に残す）。P0.1 は負の値（-）も読む。
    dot_check のある値は、補う前に ROI の画素で小数点を確かめ、無ければ整数として扱う（roi を渡したときだけ）。
    """
    field = Settings.OCR_FIELDS[key]
    texts = [t for _, t, _ in result]
    confs = [s for _, _, s in result]
    raw = ", ".join(texts)
    conf = ", ".join(f"{s:.2f}" for s in confs)
    if field["type"] == "text":
        value = re.sub(r"\s+", "", "".join(texts))
        return value, raw, conf
    joined = "".join(texts)
    sign = "-" if "-" in field.get("allow", "") and joined.strip().startswith("-") else ""
    digits = re.sub(r"[^0-9.]", "", joined)
    if not re.fullmatch(r"\d+\.?\d*", digits or ""):
        return "", raw, conf
    if field["type"] == "int":
        value = digits.split(".")[0]
    elif "." not in digits and len(digits) >= field.get("dot_min_digits", 3):
        if field.get("dot_check") and roi is not None and not _has_dot(roi):
            value = digits
        else:
            value = digits[:-1] + "." + digits[-1]
    else:
        value = digits.rstrip(".")
    return sign + value, raw, conf


# ============================================================
# initial_ocr
# ============================================================

def initial_ocr(frames, total_frames, subfolder, settings={},
                progress_callback=None, frames_dir=None):
    """
    各フレームで選んだ値（selected_ocr_keys）を読み取り、掃引位置（前のフレームから変化した列の右端）を記録する。

    数値は、読み取り位置の画像が前回OCRしたときから変わったフレームだけOCRし、それ以外は前の値を引き継ぐ。

    Returns:
        pd.DataFrame (columns: Frame, BreathMark, <値>, <値>_raw, <値>_conf（選んだ値ごと）,
                      OCR_Done, Sweep_Raw, Sweep_Run, Trigger_Apexes, Axis_OK, Axis_Pressure, Axis_Flow, Axis_Volume, Axis_Pes)
        Axis_<波形> は縦軸の目盛りから求めた換算式 "a;b;最小の目盛り;最大の目盛り"（値 = a*y + b）。読めなければ空
    """
    opts = settings.get("initial_ocr", {})
    keys = selected_ocr_keys(settings)
    gpu_enabled = opts.get("use_gpu", True)
    diff_thresh = float(opts.get("diff_thresh", 3.0))

    reader = _ensure_easyocr_reader_ready(gpu_enabled)
    print(f"GPU:{gpu_enabled}  OCR: {', '.join(keys)}")

    ax_x0, ax_x1 = Settings.AXIS_LABEL_X
    last_axis_img, last_axis = None, None
    y0, y1 = Settings.SWEEP_DIFF_ROWS
    x_lo, x_hi = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT

    last_roi = {k: None for k in keys}
    last_val = {k: ("", "", "0.00") for k in keys}
    prev_band = None
    data = []

    with tqdm(total=total_frames, desc="OCR Processing") as pbar:
        for frame_count, color_frame in enumerate(frames):
            frame = _normalize_frame(color_frame)
            gray = _to_gray(frame)
            row = {"Frame": frame_count, "BreathMark": 0, "OCR_Done": 0}

            # --- 数値（表示が変わったときだけOCR） ---
            for key in keys:
                field = Settings.OCR_FIELDS[key]
                roi = _crop(gray, field["roi"]).astype(np.int16)
                prev = last_roi[key]
                if prev is None or np.abs(roi - prev).mean() > diff_thresh:
                    try:
                        result = reader.readtext(roi.astype(np.uint8), allowlist=field.get("allow", Settings.DIGITS))
                        last_val[key] = _parse_ocr(result, key, roi)
                    except Exception:
                        last_val[key] = ("", "", "0.00")
                    last_roi[key] = roi
                    row["OCR_Done"] = 1
                value, raw, conf = last_val[key]
                row[key], row[f"{key}_raw"], row[f"{key}_conf"] = value, raw, conf

            # --- 掃引位置: 前のフレームから変化した列のまとまりのうち最大のものの右端 ---
            band = gray[y0:y1, x_lo:x_hi].astype(np.int16)
            sweep, sweep_run = np.nan, np.nan
            if prev_band is not None:
                changed = (np.abs(band - prev_band) > Settings.SWEEP_DIFF_THRESH).sum(0) >= Settings.SWEEP_DIFF_MIN_PIXELS
                runs = _runs(changed)
                if runs:
                    a, b = max(runs, key=lambda r: r[1] - r[0])
                    if b - a >= 2:
                        sweep, sweep_run = float(x_lo + b), float(b - a + 1)
            prev_band = band
            row["Sweep_Raw"] = sweep
            row["Sweep_Run"] = sweep_run   # 変化した列の幅（画面全体の描き直しでは 1000 列以上になり、掃引位置ではない）

            # --- 患者トリガーの三角（▲）の頂点の x（";" 区切り）。新しく描かれたものかは post_ocr でペン先の位置から判断する ---
            row["Trigger_Apexes"] = ";".join(f"{x:.1f}" for x in trigger_apexes(frame))

            # --- 縦軸の目盛り（数字の列が変わったときだけ読む）。Axis_OK = 目盛りが読めた波形の数 ---
            axis_img = gray[:, ax_x0:ax_x1].astype(np.int16)
            if last_axis_img is None or np.abs(axis_img - last_axis_img).mean() > 1.0:
                axis = axis_scale.read_axis(gray)
                last_axis = (axis_scale.encode(axis), sum(v is not None for v in axis.values()))
                last_axis_img = axis_img
            row.update(last_axis[0])
            row["Axis_OK"] = last_axis[1]

            data.append(row)
            pbar.update(1)
            if progress_callback is not None and (frame_count % 100 == 0 or frame_count == total_frames - 1):
                progress_callback(current=frame_count, total=total_frames, message="OCR Processing")

    df = pd.DataFrame(data)
    print(f"[initial_ocr] {len(df)} frames processed")
    return df


# ============================================================
# 掃引の追跡
# ============================================================
# 0.5 までは「一定の速さで 22秒ごとに x=1567 で左端に戻る」モデルを動画全体に当てはめ、近い検出値だけを使っていた。
# 実際には
#   - 画面操作などで波形の描画が止まり、画面全体が消去されて途中から左端に戻る（Ptranspulm 2026-10-09 の B243、フレーム 8293）
#   - 1分ごとの区間の境目でフレームの時刻がずれ、掃引の位置が 10px ほど飛ぶ
# ことがあり、モデルが前後30秒をならすため、その前後でペン先の位置がずれて波形がでたらめになった。
# そこで V600 と同じく、毎フレームのペン先を直接追跡する。

def track_sweep(sweep_raw, sweep_run, fps, abnormal=None):
    """各フレームのペン先を追跡する。

    Args:
        sweep_raw: 各フレームで検出した掃引位置（前のフレームから変化した列のまとまりの右端、画面の x）。無ければ NaN
        sweep_run: そのまとまりの幅（SWEEP_RUN_MAX を超えるものは画面全体の描き直しで、掃引位置ではない）
        abnormal: 各フレームの Abnormal（ポップアップなど）。ポップアップの間も掃引は止まらず、上の気道内圧の欄で検出できるが、
            ポップアップの描画も変化として検出されるので、左端からの描き始めは右端まで描き終えたはずのときだけ認める（0.8.0）
    Returns:
        dict(
            pen:   各フレームのペン先（画面の x）,
            u:     通しの位置（描いた列の数の累計。左端に戻っても連続）,
            pass_: 何周目か（左端に戻るたびに +1）,
            wrap:  このフレームの直前で左端に戻ったか（0: いいえ、1: 右端まで描いて戻った、2: 途中で消去されて戻った）,
            detected: 検出値を使ったフレームか,
            speed: 掃引の速さ（px/フレーム）,
        )
    """
    XL, XR = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT
    raw = np.asarray(sweep_raw, float) - Settings.SWEEP_ERASE_OFFSET
    run = np.asarray(sweep_run, float) if sweep_run is not None else np.full(len(raw), np.nan)
    # 1フレームの進み（掃引の速さ / fps）より広いまとまりだけを描き直しとみなす（低い fps では1フレームで 40px 以上進む。engine_api 2）
    raw[run > max(Settings.SWEEP_RUN_MAX, 3 * Settings.SWEEP_PX_PER_SEC / fps)] = np.nan
    n = len(raw)
    speed = Settings.SWEEP_PX_PER_SEC / fps
    if n == 0:
        return {"pen": raw, "u": raw, "pass_": np.zeros(0, int), "wrap": np.zeros(0, int),
                "detected": np.zeros(0, bool), "speed": speed}

    # 速さ: 続けて検出できた値どうしの進み ÷ 間のフレーム数（1フレームあたり 0 < d < 4倍の既定値）の中央値。
    # 0.8.1 までは隣り合うフレームの検出値の差だけを使っていた。高い fps では1フレームの進みが小さく（30 fps で 1.7px）、
    # 検出できるのは大きく進んだフレームに偏るので、速さを大きく見積もっていた（30 fps で 90 px/秒。engine_api 2）
    vi = np.flatnonzero(np.isfinite(raw))
    if len(vi) >= 2:
        gap = np.diff(vi)
        step = np.diff(raw[vi]) / gap
        ok = (gap <= max(1, int(round(fps)))) & (step > 0) & (step < 4 * speed)
        if ok.sum() >= 20:
            speed = float(np.median(step[ok]))
    margin = Settings.SWEEP_RESTART_MARGIN

    # 検出値を順に確かめる: 前に採用した位置から自然に進んだもの、または左端から描き始めたものだけを採用する
    abn = np.zeros(n, bool) if abnormal is None else np.asarray(abnormal, bool)
    acc = np.full(n, np.nan)
    last = None
    for i in range(n):
        p = raw[i]
        if not np.isfinite(p):
            continue
        if last is not None:
            gap = i - last[0]
            dd = p - last[1]
            restart = p <= XL + speed * gap + margin
            if restart and abn[i] and last[1] + speed * gap < XR - 1 - margin:
                # ポップアップの描画を左端からの描き始めと取り違えない（Ptranspulm 2026-10-09 のフレーム 30471: 0.7.4 までは
                # ここで次の周に入ったとみなし、その後のポップアップの間のペン先がずれていた）
                restart = False
            if not (-1 <= dd <= 2 * speed * gap + 8 or restart):
                continue
        acc[i] = p
        last = (i, p)

    # 何周目か: 採用した検出値が SWEEP_WRAP_JUMP 以上左へ戻ったら次の周
    pass_ = np.zeros(n, int)
    wrap = np.zeros(n, int)
    vi = np.flatnonzero(np.isfinite(acc))
    for a, b in zip(vi[:-1], vi[1:]):
        if acc[b] < acc[a] - Settings.SWEEP_WRAP_JUMP:
            near_right = acc[a] + speed * (b - a) >= XR - 1 - margin
            pass_[b:] += 1
            wrap[b] = 1 if near_right else 2
    # 欠けを埋める: 同じ周の中は前後の検出値から直線で、周の終わりの後は速さで進める（右端で止める）
    pen = np.full(n, np.nan)
    for k in np.unique(pass_):
        idx = np.flatnonzero(pass_ == k)
        ok = idx[np.isfinite(acc[idx])]
        if len(ok) == 0:
            continue
        pen[idx] = np.interp(idx, ok, acc[ok])
        tail = idx[idx > ok[-1]]
        pen[tail] = np.minimum(acc[ok[-1]] + speed * (tail - ok[-1]), XR - 1)
        head = idx[idx < ok[0]]
        pen[head] = np.maximum(acc[ok[0]] - speed * (ok[0] - head), XL)
    if not np.isfinite(pen).any():
        pen = np.full(n, float(XL))
    pen = pd.Series(pen).ffill().bfill().to_numpy()

    # 通しの位置: 同じ周の中はペン先の進み。右端まで描いて戻ったときは右端までと左端からの列数、
    # 途中で消去されて戻ったときは描いていないので、その間の時間だけ（速さ × 1フレーム）進める
    adv = np.zeros(n)
    for i in range(1, n):
        if pass_[i] == pass_[i - 1]:
            adv[i] = max(pen[i] - pen[i - 1], 0.0)
        elif wrap[i] == 1:
            adv[i] = max(XR - 1 - pen[i - 1], 0.0) + (pen[i] - XL + 1)
        else:
            adv[i] = speed
    u = (pen[0] - XL) + np.cumsum(adv)
    return {"pen": pen, "u": u, "pass_": pass_, "wrap": wrap, "detected": np.isfinite(acc), "speed": speed}


# ============================================================
# post_ocr
# ============================================================

BLANK = "<blank>"   # 表示が空欄（「---」・読めない）


def confirmed_changes(values, stable=2):
    """値の列の中で、新しい値が stable フレーム以上続いた変化の始まりのフレーム（行番号）。

    1フレームだけ（stable 未満）の値は無視する: A→B（1フレーム）→C なら C の始まり、A→B（1フレーム）→A なら変化なし。
    最後まで続いた値は stable 未満でも変化とする（録画の終わり）。
    """
    n = len(values)
    if n == 0:
        return []
    starts = [0] + [i for i in range(1, n) if values[i] != values[i - 1]]
    runs = [(a, (starts[k + 1] if k + 1 < len(starts) else n) - a, values[a]) for k, a in enumerate(starts)]
    out, current = [], runs[0][2]
    for a, length, v in runs[1:]:
        if v == current:
            continue
        if length >= stable or a + length == n:
            out.append(a)
            current = v
    return out


def _mode(series):
    s = series.dropna()
    s = s[s.astype(str) != ""]
    return s.mode().iloc[0] if len(s) else np.nan


def _shown_value(series):
    """表示の更新から次の更新までの間に表示されていた値（最頻値。同数なら後に表示されていた方）。
    表示の書き換えが列によって1フレーム遅れても、短い区間で前の値が選ばれないように後の値を優先する。"""
    s = series.dropna()
    s = s[s.astype(str) != ""]
    if not len(s):
        return np.nan
    counts = s.value_counts()
    best = counts[counts == counts.max()].index
    if len(best) == 1:
        return best[0]
    last = {v: i for i, v in enumerate(s.tolist())}
    return max(best, key=lambda v: last[v])


def trigger_events(apexes, pen, hidden, speed, fps, sweep_u=None):
    """新しく描かれた患者トリガーの三角（▲）の位置 = 吸気の始まり。(行番号のリスト, 頂点の通しの位置 u のリスト)（時間順）。
    sweep_u: 各フレームの通しの位置（Sweep_U）。無ければ u は NaN。

    apexes: 各フレームの Trigger_Apexes（頂点の x を ";" でつないだ文字列）, pen: 各フレームのペン先（画面の x）,
    hidden: 気道内圧の欄が見えないフレーム（ポップアップなど）, speed: 掃引の速さ（px/フレーム）。
    前のフレームに無く、ペン先のすぐ後ろ（TRIGGER_NEW_LAG_PX）にある頂点を新しい三角とし、
    その位置をペン先が通った時刻（行番号）を返す。1周前の三角（ペン先より右に残っているもの）と、
    気道内圧の欄が見えないフレーム（とその次のフレーム）は使わない。"""
    lo, hi = Settings.TRIGGER_NEW_LAG_PX
    hi = max(hi, 4 + 2 * speed)   # 低い fps では、三角が見えたときにペン先が1フレーム分（speed px）先へ進んでいる（engine_api 2）
    keep_frames = max(2, int(round(Settings.TRIGGER_KEEP_SEC * fps)))
    xs = [[float(v) for v in str(text).split(";") if v not in ("", "nan")] for text in apexes]
    n = len(xs)

    def stays(i, x):
        """頂点 x が、ペン先が TRIGGER_KEEP_PX 以上先へ進んだ後のフレームにも残っているか（消されずに描き直されたもの）"""
        for j in range(i + 1, min(i + 1 + keep_frames, n)):
            if hidden[j] or not np.isfinite(pen[j]):
                return False
            if pen[j] - x >= Settings.TRIGGER_KEEP_PX:
                return any(abs(x - q) <= Settings.TRIGGER_SAME_PX for q in xs[j])
        return False

    found = []
    for i in range(1, n):
        # 最初のフレームは前のフレームが無く、新しい三角か判断できない（画面に残っている三角まで数え、その時刻が動画の始まりより前の
        # 負の行番号になって post_ocr が止まっていた: 動画の途中から切り出した解析の KeyError '[-2] not in index'。0.8.2）
        if hidden[i] or hidden[i - 1] or not np.isfinite(pen[i]):
            continue
        prev = xs[i - 1]
        for x in xs[i]:
            lag = pen[i] - x
            if not lo <= lag <= hi:
                continue
            same = [q for q in prev if abs(x - q) <= Settings.TRIGGER_SAME_PX]
            if same:
                # 1周前の三角とほぼ同じ位置に新しい三角が描かれることがある（Ptranspulm 2026-10-09 の B237・B452）。
                # 前のフレームではペン先より右（1周前）にあり、ペン先が通り過ぎた後も残っていれば描き直されたもの
                # （ペン先が通った列は消されるので、1周前の三角は消える）
                if not (all(q > pen[i - 1] for q in same) and stays(i, x)):
                    continue
            found.append((max(i - max(lag, 0) / speed, 0.0), sweep_u[i] - lag if sweep_u is not None else np.nan))
    out = []
    for t, uu in sorted(found):
        if not out or t - out[-1][0] > Settings.TRIGGER_DEDUP_SEC * fps:
            out.append((t, uu))
    return [int(round(t)) for t, _ in out], [uu for _, uu in out]


def aux_wave_name(settings):
    """4 段目の圧の出力の名前（0.2.0）。Hamilton は 4 段目に Pes か Ptranspulm（経肺圧 = Paw − Pes）を表示でき、
    どちらかは利用者が設定（post_ocr.ptranspulm）で選ぶ（記録の途中で切り替えない前提）。エンジンの中ではどちらも "Pes" として扱い、
    出力（BreathCSVs の列、Breaths.csv・Frames.csv の Axis_<波形>・<波形>_min/_max、fields.txt、scale_meta.json）の名前だけを変える"""
    return "Ptranspulm" if settings.get("post_ocr", {}).get("ptranspulm", False) else "Pes"


def _rename_aux(df, aux, to_output=True):
    """4 段目の圧の列の名前を、エンジンの中の名前（Pes）と出力の名前（aux）の間で付け替える。"""
    if aux == "Pes":
        return df
    a, b = ("Pes", aux) if to_output else (aux, "Pes")
    names = {f"Axis_{a}": f"Axis_{b}", f"Axis_Filled_{a}": f"Axis_Filled_{b}", f"{a}_min": f"{b}_min", f"{a}_max": f"{b}_max"}
    return df.rename(columns={k: v for k, v in names.items() if k in df.columns})


def post_ocr(df, Analyze_Id, user_ID, settings={}):
    """
    呼吸の区切り（数値の表示の更新）を求め、呼吸番号・ペン先の位置を付ける。

    Returns:
        (df_frames, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    triggers = [k for k in opts.get("breath_trigger", ["Compliance", "VTE"]) if k in df.columns]
    # stable_frames は 10 fps のときのフレーム数。ほかの fps では同じ時間になるよう換算する（engine_api 2）
    stable = max(1, int(round(int(opts.get("stable_frames", 2)) * fps / 10)))
    merge = max(1, int(round(Settings.UPDATE_MERGE_SEC * fps)))
    shift = int(round(float(opts.get("boundary_shift_sec", -0.5)) * fps))

    aux = aux_wave_name(settings)
    df = _rename_aux(df, aux, to_output=False).sort_values("Frame").reset_index(drop=True).copy()
    n = len(df)
    keys = [k for k in OCR_KEYS if k in df.columns]   # initial_ocr で読んだ値
    for col in keys:
        if col in TEXT_KEYS:
            df[col] = df[col].fillna("").astype(str).str.strip().replace("", np.nan)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 波形画面でないフレーム = 縦軸の目盛りが読めた波形が AXIS_MIN_PANELS 未満（ポップアップ・別の画面）。
    # 0.4 以前は目盛りの列を基準画像と比べていたため、縦軸のスケールが変わっただけで Abnormal になっていた
    n_axis = pd.to_numeric(df["Axis_OK"], errors="coerce").fillna(0) if "Axis_OK" in df else pd.Series(0, index=df.index)
    df["Abnormal"] = (n_axis < Settings.AXIS_MIN_PANELS).astype(int)
    # 1つの波形だけ目盛りが読めない（見本に無い文字など）ときは、直前に読めた目盛りを引き継ぐ（Axis_Filled_<波形> = 1）
    for name in Settings.WAVES:
        col = f"Axis_{name}"
        if col in df:
            read = df[col].map(lambda t: axis_scale.decode(t) is not None)
            df[f"Axis_Filled_{name}"] = (~read).astype(int)
            df[col] = df[col].where(read).ffill().fillna("")

    # --- ペン先の位置 ---
    sw = track_sweep(df["Sweep_Raw"].to_numpy(float) if "Sweep_Raw" in df else np.full(n, np.nan),
                     df["Sweep_Run"].to_numpy(float) if "Sweep_Run" in df else None, fps, df["Abnormal"].to_numpy(int))
    df["Sweep_U"] = np.round(sw["u"], 2)
    df["Sweep_X"] = np.round(sw["pen"], 1)
    df["Sweep_Pass"] = sw["pass_"]
    df["Sweep_Wrap"] = sw["wrap"]
    df["Sweep_Detected"] = sw["detected"].astype(int)
    df["Sweep_Rate"] = round(sw["speed"] * fps, 4)  # px/秒
    print(f"[post_ocr] sweep {sw['speed'] * fps:.2f} px/s, detected {sw['detected'].mean() * 100:.1f}% of frames, "
          f"{int((sw['wrap'] == 1).sum())} wraps, {int((sw['wrap'] == 2).sum())} restarts")

    # --- 呼吸の区切り: 表示の更新 ---
    # 0.7.0: トリガー列（VTE・コンプライアンス）ごとに、新しい値が stable_frames（2）フレーム以上続いたら「変化」。
    # 区切りはその値が出た最初のフレーム。空欄（「---」・読めない）も1つの値として扱う。1フレームだけの値は無視する。
    # 2つの列の変化が1フレーム差なら同じ1回の更新（表示の書き換えが2フレームにまたがった）として最初のフレームで1回だけ区切る。
    # 0.6 までは min_breath_sec（1秒）より近い更新をまとめていたが、時間で間引くのをやめた
    # 0.7.3: Abnormal（波形の縦軸が読めない）フレームでも、数値欄が読めていれば更新に使う。
    # 0.7.2 までは Abnormal の間の変化をすべて無視していたため、ポップアップなどの間の呼吸が1つにまとめられていた
    # （Ptranspulm 2026-10-09 の B1012: 約13秒・6呼吸）。数値欄までポップアップに覆われたときに空欄が続いて
    # 偽の区切りにならないよう、Abnormal のフレームの空欄（読めない）だけは前の値を引き継ぐ
    events = []
    for col in triggers:
        vals = df[col].astype(str).where(df[col].notna(), BLANK)
        vals = vals.where((df["Abnormal"] == 0) | df[col].notna()).ffill().bfill()
        events += confirmed_changes(vals.to_numpy(object), stable)
    updates = []
    for i in sorted(set(events)):
        if not updates or i - updates[-1] > merge:
            updates.append(int(i))
    display_updates = list(updates)

    # 0.8.0: 患者トリガーの三角（吸気の始まり）の近くに表示の更新が無ければ、三角の少し後に更新があったものとして区切る。
    # 前後の呼吸の VTE・コンプライアンスがたまたま同じ値だと、Hamilton は2呼吸として記録しても表示が変わらない
    # （Ptranspulm 2026-10-09 の B87: Study Recorder の 8899・8900 がどちらも 404 mL / 52.8）
    abn = df["Abnormal"].to_numpy(int)
    # 三角は、Abnormal でも気道内圧の縦軸を読めた（気道内圧の欄が見えている）フレームなら使う（ポップアップは下の欄を覆うことが多い）
    paw_seen = df["Axis_Filled_Pressure"].eq(0) if "Axis_Filled_Pressure" in df else pd.Series(False, index=df.index)
    paw_hidden = (df["Abnormal"] == 1) & ~paw_seen
    df["Paw_Hidden"] = paw_hidden.astype(int)
    triggers_at, triggers_u = trigger_events(
        df["Trigger_Apexes"].fillna("").to_numpy(object) if "Trigger_Apexes" in df else [""] * n,
        df["Sweep_X"].to_numpy(float), paw_hidden.to_numpy(bool), sw["speed"], fps, sw["u"])
    before = int(round(Settings.TRIGGER_UPDATE_BEFORE_SEC * fps))
    after = int(round(Settings.TRIGGER_UPDATE_AFTER_SEC * fps))
    lag = int(round(Settings.TRIGGER_UPDATE_LAG_SEC * fps))
    abn_before = int(round(Settings.TRIGGER_ABNORMAL_BEFORE_SEC * fps))
    split_updates = []
    for t in triggers_at:
        if t + after >= n or abn[max(t - abn_before, 0):t + after + 1].any():
            continue   # 録画の終わり・Abnormal の近くは、表示の更新が見えていないだけかもしれない
        if any(t - before <= u <= t + after for u in display_updates):
            continue
        split_updates.append(t + lag)
    updates = sorted(set(updates) | set(split_updates))

    # 区切りの位置を boundary_shift_sec だけずらす（表示の更新は吸気の始まりより少し遅れる）
    bounds = sorted({min(max(u + shift, 0), n - 1) for u in updates})
    df["BreathMark"] = 0
    df.loc[bounds, "BreathMark"] = 1
    df["BreathNumber"] = df["BreathMark"].cumsum()
    if not bounds or bounds[0] > 0:
        df["BreathNumber"] += 1          # 最初の区切りより前は呼吸1（途中から始まる呼吸）
    df["Display_Update"] = 0
    df.loc[display_updates, "Display_Update"] = 1
    df["Trigger_New"] = 0
    df.loc[triggers_at, "Trigger_New"] = 1     # 新しく描かれた三角（吸気の始まり）
    df["Trigger_U"] = np.nan
    df.loc[triggers_at, "Trigger_U"] = np.round(triggers_u, 1)   # 三角の頂点の通しの位置（extract_waves で区切りを合わせる）
    df["Trigger_Split"] = 0
    df.loc[split_updates, "Trigger_Split"] = 1  # 三角で足した区切り（表示の更新があったものとして扱う位置）

    df["Time"] = df["Frame"].apply(lambda f: _format_time(f, fps))

    # --- 呼吸ごとの表 ---
    # 0.7.1: 測定値の割り当ては表示の更新（Display_Update）の区間で行う。
    # 呼吸 i の値 = 呼吸 i+1 を始めた更新から、その次の更新までの間に表示されていた値。
    # 0.7.0 までは「次の呼吸の区間（更新の 0.5 秒前から）の最頻値」だったため、次の呼吸が 1 秒以下だと
    # 更新前の（前の呼吸の）値の方が多くなり、1つ前の値が入っていた（Ptranspulm 2026-10-09 の B990 など）
    start_update = {}   # 呼吸の最初の行 → その呼吸を始めた表示の更新の行
    for u in updates:
        start_update.setdefault(min(max(u + shift, 0), n - 1), u)
    rows = []
    groups = list(df.groupby("BreathNumber", sort=True))
    windows = []        # 呼吸ごとの「その呼吸を始めた更新〜次の更新」の行の範囲（更新で始まらない最初の呼吸は None）
    for k, (_no, g) in enumerate(groups):
        u0 = start_update.get(int(g.index[0]))
        if u0 is None:
            windows.append(None)
            continue
        u1 = start_update.get(int(groups[k + 1][1].index[0]), n) if k + 1 < len(groups) else n
        windows.append((u0, u1))

    def shown(key, window):
        # Abnormal のフレームも使う（数値欄は読めている。読めない空欄は _shown_value が除く）
        if window is None:
            return np.nan
        a, b = window
        return _shown_value(df[key].iloc[a:b]) if b > a else np.nan

    for k, (no, g) in enumerate(groups):
        s, e = int(g.index[0]), int(g.index[-1])
        normal = g[g["Abnormal"] == 0]
        nxt = groups[k + 1][1] if k + 1 < len(groups) else None
        row = {
            "BreathNumber": int(no),
            "Frame": int(df.at[s, "Frame"]),
            "End_Frame": int(df.at[e, "Frame"]),
            "Time": df.at[s, "Time"],
            "Duration": round((e - s + 1) / fps, 2),
        }
        # 左の測定値は直前の呼吸の値: この呼吸の値 = 次の呼吸を始めた更新で表示された値（次の呼吸の長さによらない）。
        # *_displayed = この呼吸を始めた更新で表示された値（= 前の呼吸の値。最初の呼吸はその区間の表示）
        # 換気モード・設定値は、この呼吸の間に表示されていた値
        for key in keys:
            if key in MEASURED_KEYS:
                row[key] = shown(key, windows[k + 1]) if k + 1 < len(groups) else np.nan
                row[f"{key}_displayed"] = shown(key, windows[k]) if windows[k] is not None else _mode(normal[key])
            else:
                row[key] = _mode(normal[key])
        # 患者トリガー: この呼吸の間に新しい三角が描かれたら A（自発）、無ければ C（強制）。
        # 気道内圧の欄が見えないフレームを含む呼吸で三角が無いときは、隠れていたかもしれないので空欄
        if g["Trigger_New"].any():
            row["Trigger"] = "A"
        else:
            row["Trigger"] = "" if g["Paw_Hidden"].any() else "C"
        alerts = []
        u0 = start_update.get(s)
        if u0 is not None and u0 in split_updates:
            alerts.append("TriggerSplit")   # 表示が変わらず、三角で区切った呼吸
        if k == 0 and (not bounds or bounds[0] > 0):
            alerts.append("Fragment")   # 録画の途中から始まる呼吸
        if nxt is None:
            alerts.append("Fragment")   # 録画の終わりで切れている呼吸
        if g["Abnormal"].any():
            alerts.append("Abnormal")
        row["Abnormal"] = int(g["Abnormal"].any())
        # 縦軸の換算式 = 波形を切り出す最後のフレームのもの。
        # 0.8.1: P_min / P_max などは描画領域の下端・上端の値（呼吸器の画面に見える範囲）。0.8.0 までは目盛りの最小・最大の数字で、
        # 一番下の目盛りより下にも描画領域があるため、その範囲に出た波形（Ptranspulm・Volume の負の部分など）がチャートの外に出ていた。
        # 目盛りの最小・最大は Axis_<波形> の3・4番目に残っている
        for name, spec in Settings.WAVES.items():
            text = df.at[e, f"Axis_{name}"] if f"Axis_{name}" in df else None
            cal = axis_scale.decode(text)
            row[f"Axis_{name}"] = text if cal is not None else ""
            a, b = cal[:2] if cal else _CAL[name]
            top, bottom = spec["area"]
            row[f"{spec['scale_prefix']}_min"], row[f"{spec['scale_prefix']}_max"] = round(a * bottom + b, 2), round(a * top + b, 2)
            filled = cal is None or (f"Axis_Filled_{name}" in df and df.at[e, f"Axis_Filled_{name}"] == 1)
            if filled and not row["Abnormal"]:
                alerts.append("Axis?")   # このフレームでは目盛りを読めず、前の目盛り（または既定）で換算する
        row["Alert"] = ",".join(dict.fromkeys(alerts))
        rows.append(row)
    df_breaths = pd.DataFrame(rows)
    df["Alert"] = df["BreathNumber"].map(df_breaths.set_index("BreathNumber")["Alert"]).fillna("")

    print(f"[post_ocr] {len(df)} frames, {len(df_breaths)} breaths ({len(display_updates)} display updates, "
          f"{len(triggers_at)} trigger marks, {len(split_updates)} split by trigger marks)")
    return _rename_aux(df, aux), _rename_aux(df_breaths, aux)


# ============================================================
# extract_waves
# ============================================================

def _calibration(spec):
    """目盛り (y, 値) の直線 → (傾き, 切片)。値 = 傾き*y + 切片"""
    ys, vs = zip(*spec["ticks"])
    a, b = np.polyfit(ys, vs, 1)
    return float(a), float(b)


_CAL = {name: _calibration(spec) for name, spec in Settings.WAVES.items()}


# ============================================================
# 波形の点の選び方（NAVA v1.3 と同じ方式）
# ============================================================
# 1列に1点を選ぶと、急峻な箇所（1列に縦線がある所）で選ぶ高さが偶然で決まって偽のスパイクになるか
# （最も明るい画素などを選ぶ場合）、縦線の真ん中になって V 字の先端が浅くなる（中央値の場合）。
# 列ごとに「線が通った範囲」を取り、
#   1) 縦に続いた画素のかたまりの中から、隣の列とつながる線を全体で最適に選ぶ（ノイズ・文字を除く）
#   2) 範囲の中でなめらかな経路を全体で最適に引く（縦線は坂になり、跳ねない）
#   3) 範囲が経路から局所的に突き出している所（V字の谷・山の先端）はその値に固定して引き直す
# px 単位の値は 1280x720 の画面での値。trace_columns(scale=画面の高さ/720) で拡大して使う。
TRACE_RUN_JOIN = 4          # 同じ列で、これ未満の画素の切れ目はつないで1つのかたまりとする
TRACE_SEG_GAP = 12          # 列の番号の差がこれを超えたら別の線とみなして経路を切る（列）
TRACE_HUE_WEIGHT = 0.5      # かたまりの選択で、色のずれ（cost）に掛ける重み
TRACE_STEP = 0.5            # 経路の高さの刻み
TRACE_TIP_MARGIN = 2.0      # 範囲が経路から これ以上 突き出している列を先端の候補とする
TRACE_TIP_DROP = 2.0        # 先端の両側で、範囲の端が これ以上 引っ込んでいること
TRACE_TIP_WIN = 8           # 先端の両側を調べる列数
TRACE_TIP_SUPPORT = 2       # 先端の深さ（±TRACE_TIP_SUPPORT_PX）に届いている列がこれ以上あること
TRACE_TIP_SUPPORT_PX = 1.5
TRACE_MAX_WIDTH = 5         # 線の太さの上限


def _trace_runs(mask, cost, join):
    """列ごとの縦のかたまり [(上端の行, 下端の行, 色のずれ)]。"""
    cols = []
    for c in range(mask.shape[1]):
        idx = np.flatnonzero(mask[:, c])
        if not len(idx):
            cols.append([])
            continue
        runs, a, b = [], idx[0], idx[0]
        for r in idx[1:]:
            if r > b + join:
                runs.append((a, b))
                a = r
            b = r
        runs.append((a, b))
        cc = cost[:, c] if cost is not None else None
        cols.append([(float(a), float(b), float(cc[a:b + 1][mask[a:b + 1, c]].min()) if cc is not None else 0.0)
                     for a, b in runs])
    return cols


def _trace_choose(cols, seg_gap):
    """隣の列とつながる1本の線のかたまりを選ぶ（Viterbi）。(列ごとの (上端, 下端) or None, 区間の列番号のリスト)"""
    chosen = [None] * len(cols)
    idx = [i for i, c in enumerate(cols) if c]
    if not idx:
        return chosen, []
    segs, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] > seg_gap:
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    for seg in segs:
        cost = np.array([TRACE_HUE_WEIGHT * r[2] for r in cols[seg[0]]])
        back = []
        for p, q in zip(seg, seg[1:]):
            A, B = cols[p], cols[q]
            ta = np.array([r[0] for r in A]); ba = np.array([r[1] for r in A])
            tb = np.array([r[0] for r in B]); bb = np.array([r[1] for r in B])
            gap = np.maximum(0.0, np.maximum(tb[:, None] - ba[None, :] - 1, ta[None, :] - bb[:, None] - 1))
            tr = cost[None, :] + gap * gap / (q - p)
            arg = tr.argmin(axis=1)
            back.append(arg)
            cost = tr[np.arange(len(B)), arg] + np.array([TRACE_HUE_WEIGHT * r[2] for r in B])
        k = int(cost.argmin())
        chosen[seg[-1]] = cols[seg[-1]][k][:2]
        for t in range(len(seg) - 1, 0, -1):
            k = int(back[t - 1][k])
            chosen[seg[t - 1]] = cols[seg[t - 1]][k][:2]
    return chosen, segs


def _trace_path(lo, hi, gaps, step):
    """各列の範囲 [lo_i, hi_i] の中で Σ(隣の列との差^2 / 列の間隔) が最小の経路。"""
    grids = [np.arange(lo[i], hi[i] + 1e-9, step) if hi[i] > lo[i] else np.array([lo[i]]) for i in range(len(lo))]
    cost = np.zeros(len(grids[0]))
    back = []
    for t in range(1, len(grids)):
        g0, g1 = grids[t - 1], grids[t]
        tr = cost[None, :] + (g1[:, None] - g0[None, :]) ** 2 / gaps[t - 1]
        arg = tr.argmin(axis=1)
        back.append(arg)
        cost = tr[np.arange(len(g1)), arg]
    out = np.empty(len(grids))
    k = int(cost.argmin())
    out[-1] = grids[-1][k]
    for t in range(len(grids) - 1, 0, -1):
        k = int(back[t - 1][k])
        out[t - 1] = grids[t - 1][k]
    return out


def _trace_is_tip(env, sign, ext, i, j, win, drop):
    n = len(env)
    for step, start in ((-1, i - 1), (1, j + 1)):
        k, ok = start, False
        while 0 <= k < n and abs(k - start) < win:
            v = sign * (env[k] - ext)
            if v > 0.5:
                return False
            if v <= -drop:
                ok = True
                break
            k += step
        if not ok:
            return False
    return True


def _trace_tips(a, b, y, s):
    """範囲（中心線に直した上側 a・下側 b）が経路 y から局所的に突き出している先端 {位置: 値}。"""
    n = len(y)
    margin, drop, win = TRACE_TIP_MARGIN * s, TRACE_TIP_DROP * s, max(2, int(round(TRACE_TIP_WIN * s)))
    support_px, support = TRACE_TIP_SUPPORT_PX * s, TRACE_TIP_SUPPORT
    pins = {}
    for sign, env in ((-1, a), (1, b)):
        out = sign * (env - y) > margin
        i = 0
        while i < n:
            if not out[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and out[j + 1]:
                j += 1
            grp = np.arange(i, j + 1)
            ext = float(env[grp].max() if sign > 0 else env[grp].min())
            if _trace_is_tip(env, sign, ext, i, j, win, drop):
                w0, w1 = max(0, i - win), min(n, j + win + 1)
                if int((np.abs(env[w0:w1] - ext) <= support_px).sum()) >= support:
                    near = grp[np.abs(env[grp] - ext) <= 0.5 * s]
                    pins[int(near[len(near) // 2])] = ext
            i = j + 1
    return pins


def trace_columns(mask, cost=None, scale=1.0):
    """mask: (行, 列) の波形の色の画素。cost: 同じ形の色のずれ（小さいほど波形の色らしい）。
    戻り値: 列ごとの線の中心の行（float、画素が無い列は NaN）。scale = 画面の高さ / 720。"""
    s = float(scale)
    cols = _trace_runs(mask, cost, max(1, int(round(TRACE_RUN_JOIN * s))))
    out = np.full(mask.shape[1], np.nan)
    chosen, segs = _trace_choose(cols, max(2, int(round(TRACE_SEG_GAP * s))))
    if not segs:
        return out
    widths = np.array([c[1] - c[0] + 1 for c in chosen if c is not None])
    half = (float(np.clip(np.percentile(widths, 25), 1, TRACE_MAX_WIDTH * s)) - 1) / 2
    step = TRACE_STEP * s
    for seg in segs:
        top = np.array([chosen[i][0] for i in seg])
        bot = np.array([chosen[i][1] for i in seg])
        a, b = top + half, bot - half
        thin = a > b
        a[thin] = b[thin] = (top[thin] + bot[thin]) / 2
        gaps = np.diff(seg).astype(float)
        if len(seg) == 1:
            out[seg[0]] = (a[0] + b[0]) / 2
            continue
        y = _trace_path(a, b, gaps, step)
        pins = _trace_tips(a, b, y, s)
        if pins:
            a2, b2 = a.copy(), b.copy()
            for k, v in pins.items():
                a2[k] = b2[k] = v
            y = _trace_path(a2, b2, gaps, step)
        out[seg] = y
    return out


def _wave_masks(frame):
    """波形ごとの (帯の上端y, その色の画素のマスク, 色相の差)。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    out = {}
    for name, spec in Settings.WAVES.items():
        y0, y1 = spec["band"]
        h = hsv[y0:y1]
        d = np.abs(h[..., 0].astype(np.int16) - spec["hue"])
        d = np.minimum(d, 180 - d)
        out[name] = (y0, (d <= Settings.WAVE_HUE_TOL) & (h[..., 1] >= Settings.WAVE_SAT_MIN) & (h[..., 2] >= Settings.WAVE_VAL_MIN), d)
    return out


def trigger_apexes(frame):
    """患者トリガーの三角（▲）の頂点の x（画面の列）の一覧。frame は _normalize_frame 済みの BGR。"""
    y0, y1 = Settings.TRIGGER_ROWS
    hsv = cv2.cvtColor(frame[y0:y1], cv2.COLOR_BGR2HSV)
    d = np.abs(hsv[..., 0].astype(np.int16) - Settings.TRIGGER_HUE)
    d = np.minimum(d, 180 - d)
    mask = ((d <= Settings.TRIGGER_HUE_TOL) & (hsv[..., 1] >= Settings.TRIGGER_SAT_MIN)
            & (hsv[..., 2] >= Settings.TRIGGER_VAL_MIN)).astype(np.uint8)
    # 時間軸の目盛りの数字（白）と重なった三角は、動画の圧縮で色がにじんで小片に分かれるので、閉じてから1つのかたまりにする
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones(Settings.TRIGGER_CLOSE, np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h = stats[i, :4]
        if not (Settings.TRIGGER_HEIGHT[0] <= h <= Settings.TRIGGER_HEIGHT[1]
                and Settings.TRIGGER_WIDTH[0] <= w <= Settings.TRIGGER_WIDTH[1]):
            continue
        ys, xs = np.nonzero((labels[y:y + h, x:x + w] == i) & (mask[y:y + h, x:x + w] > 0))
        if len(ys) == 0:
            continue
        top = ys < ys.min() + Settings.TRIGGER_APEX_ROWS
        out.append(float(x + xs[top].mean()))
    return out


def _fill_small_gaps(v, max_gap):
    v = v.copy()
    nan = ~np.isfinite(v)
    for a, b in _runs(nan):
        if a > 0 and b < len(v) - 1 and b - a + 1 <= max_gap:
            v[a:b + 1] = np.interp(np.arange(a, b + 1), [a - 1, b + 1], [v[a - 1], v[b + 1]])
    return v


def extract_column_values(frame, xs, cal=None):
    """画面の列 xs（screen x、時間順）ごとに、各波形の値（物理量）を求める。
    cal: {波形名: (a, b)}（値 = a*y + b、縦軸の目盛りから読んだもの）。無い波形は既定の目盛り（Settings.WAVES の ticks）。
    0.3.0: 各列の「線が通った範囲」から全体で最適な経路を引き、V字の先端は残す（trace_columns）。
    以前は各列の画素の y の中央値で、縦線のある列（Flow の吸気→呼気の切り替わりなど）では線の真ん中になり、先端が浅くなっていた。"""
    masks = _wave_masks(frame)
    scale = frame.shape[0] / 720
    out = {}
    for name, (y0, m, d) in masks.items():
        sub = m[:, xs]
        vals = y0 + trace_columns(sub, d[:, xs].astype(float), scale)
        a, b = (cal or {}).get(name) or _CAL[name]
        out[name] = _fill_small_gaps(a * vals + b, Settings.WAVE_GAP_FILL)
    return out


# ============================================================
# 呼吸の区切りを吸気の始まりへ合わせる（NAVA v1.3 の切り詰めと同じ考え方）
# ============================================================
# 1呼吸 = 吸気の始まり 〜 次の吸気の始まり。
# 波形が時間の上で隙間なくつながっている呼吸どうしは、区切りを「近くの吸気の始まりの TRIM_LEAD_SEC 前」へ動かし、
# 行を前後の呼吸の間で移す（データは消えない）。つながりの端（前後の呼吸に波形が無い）は、
# 吸気の始まりより前（前の呼吸の呼気の終わり）と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）を削る。
ONSET_FLOW = 4.0        # Flow がこれ（L/min）を超えたら吸気の流れ
ONSET_RUN_SEC = 0.06    # 吸気の流れがこれ以上続くこと
ONSET_PEAK = 12.0       # その吸気がこれ（L/min）以上に届くこと
QUIET_SEC = 0.06        # 吸気の前に、吸気でない区間がこれ以上あること
ONSET_FRAC = 0.2        # 吸気の最大値のこの割合を超えた点から、立ち上がりの足元までさかのぼる
EXP_FLOW = 4.0          # 呼気の流れ（L/min, 0 よりこれ以上下）
BACKTRACK_TOL = 0.3     # 足元までさかのぼるとき、これ（L/min）以上下がり続ける間はさかのぼる
TRIM_LEAD_SEC = 0.04    # 吸気の始まりのこの時間前から残す
CONTIG_TOL_SEC = 0.15   # 前の呼吸の最後の点の次の時刻と、次の呼吸の最初の点の時刻の差がこれ（秒）以内ならつながっている（時刻の推定の誤差を見込む）


def _inspiration_onsets(flow, rate):
    """flow: Flow（L/min, NaN 可）, rate: 点/秒。吸気の始まり（立ち上がりの足元）の行番号の一覧。"""
    raw = np.asarray(flow, dtype=float)
    g = pd.Series(raw).interpolate(limit_area="inside").to_numpy()
    n = len(g)
    fin = np.isfinite(g)
    ins = fin & (np.nan_to_num(g) > ONSET_FLOW)
    run_min = max(2, int(round(ONSET_RUN_SEC * rate)))
    quiet_min = max(2, int(round(QUIET_SEC * rate)))
    out = []
    i = 0
    while i < n:
        if not ins[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and ins[j + 1]:
            j += 1
        quiet, k = 0, i - 1
        while k >= 0 and fin[k] and not ins[k]:
            quiet += 1
            k -= 1
        peak = np.nanmax(g[i:j + 1])
        if j - i + 1 >= run_min and peak >= ONSET_PEAK and quiet >= quiet_min:
            c = i + int(np.argmax(g[i:j + 1] >= ONSET_FRAC * peak))
            while c > 0 and fin[c - 1] and g[c - 1] < g[c] - BACKTRACK_TOL:
                c -= 1
            k = c + int(np.argmax(np.isnan(raw[c:]))) if np.isnan(raw[c:i + 1]).any() else None
            before = raw[:k][np.isfinite(raw[:k])] if k is not None else None
            if before is None or len(before) == 0 or before[-1] >= -EXP_FLOW:
                out.append(c)
        i = j + 1
    return out


def _snap_to_breaths(items, trigger_u=()):
    """items: 時間順の dict のリスト {"wave": DataFrame（X, Flow 列, 行=点）または None, "t0": 秒, "rate": 点/秒}。
    各 item に "wave"（切り直した波形）, "t0", "rate", "trim_head", "trim_tail"（元の範囲から削った/渡した点の数。負は前後から受け取った点）を書き込む。
    trigger_u: 患者トリガーの三角の頂点の通しの位置（X と同じ単位）。0.8.1: 自発の呼吸の区切りは、流量の吸気の始まりではなく三角に合わせる"""
    trigger_u = np.sort(np.asarray([t for t in trigger_u if np.isfinite(t)], float))
    # つながっている呼吸のまとまりに分ける
    groups, cur = [], []
    for i, it in enumerate(items):
        it.setdefault("trim_head", 0)
        it.setdefault("trim_tail", 0)
        if it["wave"] is None or len(it["wave"]) == 0 or not np.isfinite(it["t0"]):
            if cur:
                groups.append(cur)
            cur = []
            continue
        if cur:
            p = items[cur[-1]]
            if abs(p["t0"] + len(p["wave"]) / p["rate"] - it["t0"]) > CONTIG_TOL_SEC:
                groups.append(cur)
                cur = []
        cur.append(i)
    if cur:
        groups.append(cur)

    for g in groups:
        waves = [items[i]["wave"] for i in g]
        cat = pd.concat(waves, ignore_index=True)
        times = np.concatenate([items[i]["t0"] + np.arange(len(items[i]["wave"])) / items[i]["rate"] for i in g])
        bounds = np.r_[0, np.cumsum([len(w) for w in waves])]
        rate = float(np.median([items[i]["rate"] for i in g]))
        lead = max(1, int(round(TRIM_LEAD_SEC * rate)))
        flow = cat["Flow"].to_numpy(float)
        on = np.array(_inspiration_onsets(flow, rate), int)
        # 三角の位置（cat の行）。三角は Hamilton が自発トリガーと判定した位置そのもので、流量の立ち上がりは、前の吸気の流れが
        # 続いたまま次の呼吸に入ると見つからず、呼気の途中でトリガーされると呼気の一番下までさかのぼってしまう
        # （Ptranspulm 2026-10-09 の B79/80・B94・B96: 区切りが Hamilton の呼吸の始まりより 0.3〜0.5 秒早かった）
        xs_cat = cat["X"].to_numpy(float)
        tri = trigger_u[(trigger_u >= xs_cat[0]) & (trigger_u <= xs_cat[-1])]
        tri = np.array([int(np.argmin(np.abs(xs_cat - t))) for t in tri], int)
        tri_lead = int(round(Settings.TRIGGER_SNAP_LEAD_SEC * rate))
        valid = cat[[c for c in cat.columns if c != "X" and not c.startswith("_")]].notna().any(axis=1).to_numpy()
        new = bounds.copy()
        # 先頭: 空の行と、最初の呼吸の吸気の始まりより前
        lo = int(np.argmax(valid)) if valid.any() else 0
        first = on[on < (bounds[0] + bounds[1]) // 2]   # 最初の呼吸の前半にある吸気の始まり
        first_tri = tri[tri < (bounds[0] + bounds[1]) // 2]
        if len(first_tri):
            new[0] = max(lo, int(first_tri[0]) - tri_lead)
        else:
            new[0] = max(lo, int(first[0]) - lead) if len(first) else lo
        # 呼吸の間の区切り: 前後の呼吸の真ん中より内側にある吸気の始まりのうち、元の区切りに最も近いもの
        for j in range(1, len(g)):
            a = (bounds[j - 1] + bounds[j]) // 2
            b = (bounds[j] + bounds[j + 1]) // 2
            cand_tri = tri[(tri > a) & (tri < b)]
            cand = on[(on > a) & (on < b)]
            if len(cand_tri):   # 自発の呼吸: 三角に合わせる
                o = int(cand_tri[np.argmin(np.abs(cand_tri - bounds[j]))])
                new[j] = o - tri_lead
            elif len(cand):     # 強制の呼吸: 流量の吸気の始まりに合わせる（今までどおり）
                o = int(cand[np.argmin(np.abs(cand - bounds[j]))])
                new[j] = o - lead
        # 末尾: 空の行と、呼気を伴わない最後の吸気（次の呼吸の吸気の途中）
        hi = len(cat) - int(np.argmax(valid[::-1])) if valid.any() else len(cat)
        new[-1] = hi
        last_on = on[(on > new[-2] + lead) & (on < hi)]
        if len(last_on) and not (np.nan_to_num(flow[last_on[-1]:hi]) < -EXP_FLOW).any():
            new[-1] = max(new[-2] + 1, int(last_on[-1]) - lead)
        # 順序を保つ（区切りが追い越さない）
        for j in range(1, len(new)):
            new[j] = max(new[j], new[j - 1] + 1)
        for j, i in enumerate(g):
            a, b = int(new[j]), int(new[j + 1])
            items[i]["trim_head"] = a - int(bounds[j])
            items[i]["trim_tail"] = int(bounds[j + 1]) - b
            items[i]["wave"] = cat.iloc[a:b].reset_index(drop=True)
            items[i]["t0"] = float(times[a])
            # 前後の呼吸から受け取った点は元の呼吸の速さで時刻が決まっているので、速さは両端の点の時刻から求め直す
            if b - a >= 2 and times[b - 1] > times[a]:
                items[i]["rate"] = float((b - a - 1) / (times[b - 1] - times[a]))
    return items

def _axis_range(df_frames, i, name):
    """フレーム i の縦軸の (最小の目盛り, 最大の目盛り)。読めなければ None。"""
    c = axis_scale.decode(df_frames.at[i, f"Axis_{name}"]) if f"Axis_{name}" in df_frames else None
    return None if c is None else (c[2], c[3])


def _clipped(values, img, valid, name, df_frames, run=3):
    """描画領域の上端・下端（Settings.WAVES[name]["plot"]）に張り付いた点（run 点以上続くもの、または隣が空欄のもの）。
    端の値は、その点を読んだ画像のフレームの換算式で求める。"""
    y_top, y_bot = Settings.WAVES[name].get("plot", (None, None))
    v = np.asarray(values, float)
    hi = np.full(len(v), np.nan)
    lo = np.full(len(v), np.nan)
    tol = np.full(len(v), np.nan)
    for im in np.unique(img):
        c = axis_scale.decode(df_frames.at[im, f"Axis_{name}"]) if f"Axis_{name}" in df_frames else None
        if c is None:
            continue
        a, b = c[:2]
        sel = img == im
        if y_top is not None:
            hi[sel] = a * y_top + b
        if y_bot is not None:
            lo[sel] = a * y_bot + b
        tol[sel] = 2.5 * abs(a)   # 線の太さ（端に張り付いた線の中心は端から1〜2px内側）
    with np.errstate(invalid="ignore"):
        at = valid & np.isfinite(v) & ((v >= hi - tol) | (v <= lo + tol))
    out = np.zeros(len(v), bool)
    gap = valid & ~np.isfinite(v)   # 端に張り付いた線は読めずに空欄になることがある
    for a2, b2 in _runs(at):
        next_to_gap = (a2 >= 2 and gap[a2 - 2:a2].all()) or (b2 + 2 < len(v) and gap[b2 + 1:b2 + 3].all())
        if b2 - a2 + 1 >= run or next_to_gap:
            out[a2:b2 + 1] = True
    return out


def _wider_axis_frame(name, after, passes, xs, df_frames, pas, pen, abn, max_frames):
    """after より後で、name の縦軸が after のときより広く、列 xs（周 passes）がまだ画面に残っている最初のフレーム。"""
    base = _axis_range(df_frames, after, name)
    if base is None:
        return None
    n = len(df_frames)
    for g in range(after + 1, min(after + 1 + max_frames, n)):
        if abn[g]:
            continue
        r = _axis_range(df_frames, g, name)
        if r is None or (r[1] - r[0]) <= (base[1] - base[0]):
            continue
        # 列が残っている: 同じ周でペン先より左、または次の周でペン先（と消去帯）より右
        same = (passes == pas[g]) & (xs <= pen[g])
        prev = (passes == pas[g] - 1) & (xs > pen[g] + 8)
        if np.all(same | prev):
            return g
        return None   # 広がったときにはもう上書きされていた
    return None


def _after_popup_frame(after, passes, xs, pas, pen, abn, wrap, max_frames):
    """after（Abnormal のフレーム）の後で最初の Abnormal でないフレームと、列 xs（周 passes）のうちそのフレームにまだ残っている列。
    ポップアップの間も Hamilton は裏で波形を描き続けるので、ポップアップが消えた直後のフレームには、ポップアップの間に描かれた列が
    （上書きされていなければ）残っている。途中で画面が消去されて左端に戻ったら（wrap = 2）残っていない。見つからなければ (None, None)"""
    n = len(abn)
    for g in range(after + 1, min(after + 1 + max_frames, n)):
        if wrap[g] == 2:
            return None, None
        if abn[g]:
            continue
        # 列が残っている: 同じ周でペン先より左、または次の周でペン先（と消去帯）より右
        same = (passes == pas[g]) & (xs <= pen[g])
        prev = (passes == pas[g] - 1) & (xs > pen[g] + 8)
        return g, same | prev
    return None, None


def extract_waves(df_frames, df_breaths, frames_root, output_dir, user_ID,
                  settings={}, overwrite=False, archive_dir=None):
    """
    呼吸ごとの波形CSV（列: X, Pressure, Flow, Volume, Pes または Ptranspulm（aux_wave_name））を output_dir/<BreathNumber>.csv に書き出す。

    呼吸の開始時のペン先の次の列〜終了時のペン先の列を切り出す。各列は、その列を描いた周の最後のフレーム（呼吸の終わりまで）から読む。
    途中で左端に戻った呼吸は、戻る前の部分と戻った後の部分をつなぐ（画面が消去されて途中から戻った場合も、戻る前の部分は消去の直前のフレームから読む）。
    各点の時刻 = Wave_T0 + 行番号 / Wave_Rate（秒、動画の先頭から）。
    0.2.0: trim_to_breath（既定 ON）なら、波形の区切りを吸気の始まりへ合わせる（_snap_to_breaths）。
    行の X は通しの掃引位置のまま。動かした点の数は Wave_Trim_Head / Wave_Trim_Tail。

    Returns:
        (CSV を書き出した呼吸の数, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    aux = aux_wave_name(settings)
    os.makedirs(output_dir, exist_ok=True)
    W = Settings.SWEEP_X_RIGHT - Settings.SWEEP_X_LEFT
    max_len = W - 15  # ペン先の前の消去帯と、1周前の波形を含めないための余白

    df_frames = _rename_aux(df_frames, aux, to_output=False).reset_index(drop=True)
    XL, XR = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT
    tip_u = df_frames["Sweep_U"].to_numpy(float)
    pen = df_frames["Sweep_X"].to_numpy(float)
    pas = df_frames["Sweep_Pass"].to_numpy(int) if "Sweep_Pass" in df_frames else np.zeros(len(df_frames), int)
    wrap = df_frames["Sweep_Wrap"].to_numpy(int) if "Sweep_Wrap" in df_frames else np.zeros(len(df_frames), int)
    abn = df_frames["Abnormal"].to_numpy(int) if "Abnormal" in df_frames else np.zeros(len(df_frames), int)
    # そのフレームで気道内圧の縦軸の目盛りを読めた = 気道内圧の欄が見えている（ポップアップの間も見えていることが多い）
    paw_seen = (df_frames["Axis_Filled_Pressure"].to_numpy(int) == 0) if "Axis_Filled_Pressure" in df_frames \
        else np.zeros(len(df_frames), bool)
    frame_no = df_frames["Frame"].to_numpy(int)
    last_of_pass = pd.Series(np.arange(len(df_frames))).groupby(pas).max().to_dict()
    rate = float(df_frames["Sweep_Rate"].iloc[0]) if len(df_frames) else Settings.SWEEP_PX_PER_SEC
    times = frame_no / fps
    pos_of_frame = {int(f): i for i, f in enumerate(frame_no)}

    def frame_cal(i):
        """フレーム i の縦軸の換算式 {波形名: (a, b)}。"""
        out = {}
        for name in Settings.WAVES:
            c = axis_scale.decode(df_frames.at[i, f"Axis_{name}"]) if f"Axis_{name}" in df_frames else None
            if c is not None:
                out[name] = c[:2]
        return out

    df_breaths = _rename_aux(df_breaths, aux, to_output=False).copy()
    new_cols = {c: [] for c in ("Wave_T0", "Wave_Rate", "Wave_Points", "Wave_Missing", "Wave_Reread", "Wave_Popup", "Sweep_Start", "Sweep_End",
                                "Wrapped", "Wave_Trim_Head", "Wave_Trim_Tail", "Start_File_Name", "End_File_Name")}
    recs, items = [], []   # 呼吸ごとの Breaths.csv の値 / 波形（区切りを合わせてから書き出す）
    alerts = df_breaths["Alert"].fillna("").astype(str).tolist()
    written = 0

    for k, b in df_breaths.iterrows():
        rec = dict.fromkeys(new_cols, np.nan)
        s = pos_of_frame.get(int(b["Frame"]))
        e = pos_of_frame.get(int(b["End_Frame"]))
        if s is None or e is None:
            recs.append(rec)
            continue
        rec["Start_File_Name"] = os.path.join(frames_root, f"frame_{int(b['Frame']):08d}.jpg")
        rec["End_File_Name"] = os.path.join(frames_root, f"frame_{int(b['End_Frame']):08d}.jpg")

        # 波形の範囲（通しの位置）: (前のフレームのペン先, 最後のフレームのペン先]
        p0 = s - 1 if s > 0 else s
        first = int(np.floor(tip_u[p0])) + 1
        last = int(np.floor(tip_u[e]))
        # 0.8.0: Abnormal（ポップアップなど）を含む呼吸も読む。ポップアップの間も Hamilton は裏で波形を描き続けているので、
        # 列を読む画像（その周の最後のフレーム、または呼吸の最後のフレーム）でポップアップが消えていれば読める（B1019 など）。
        # 読む画像が Abnormal の列は下で飛ばす（空欄）。0.7.4 までは Abnormal を含む呼吸の波形を書き出さなかった
        if last < first:
            recs.append(rec)
            continue
        if last - first + 1 > max_len:
            alerts[k] = ",".join(filter(None, [alerts[k], "Long"]))

        # 各列（通しの位置 u）を描いたフレーム kk と、その画面の x。読む画像は「その周の最後のフレーム（呼吸の終わりまで）」。
        # 左端に戻った周をまたぐ呼吸は、戻る前の周の部分をその周の最後のフレームから、戻った後の部分を呼吸の最後のフレームから読む
        # （画面全体が消去されて戻った場合、戻る前の部分は最後のフレームには残っていない）
        u = np.arange(first, last + 1)
        kk = np.clip(np.searchsorted(tip_u, u, side="left"), p0 + 1 if s > 0 else s, e)
        x = pen[kk] - (tip_u[kk] - u)
        img = np.minimum(np.array([last_of_pass[p] for p in pas[kk]]), e)
        before = x < XL - 0.5                       # この列は kk の直前の周（左端に戻る前）に描かれた
        kb = np.maximum(kk - 1, 0)
        xb = pen[kb] + (u - tip_u[kb])
        x = np.where(before, xb, x)
        img = np.where(before, np.minimum(np.array([last_of_pass[p] for p in pas[kb]]), e), img)
        # 途中で消去されて戻った場合、戻る前のペン先より先の列は描かれていない
        valid = ~before | (wrap[kk] == 1) | (xb <= pen[kb] + 0.5)
        valid &= (x >= XL - 0.5) & (x <= XR - 0.5)
        xi = np.clip(np.round(x).astype(int), XL, XR - 1)

        vals = {name: np.full(len(u), np.nan) for name in Settings.WAVES}
        pass_col = np.where(before, pas[kb], pas[kk])
        reread = []
        popup = []
        ok_any = False

        def read(im, sel, names=None):
            path = os.path.join(frames_root, f"frame_{int(frame_no[im]):08d}.jpg")
            frame = cv2.imread(path) if os.path.isfile(path) else None
            if frame is None:
                return False
            got = extract_column_values(_normalize_frame(frame), xi[sel], frame_cal(im))
            for name in names or Settings.WAVES:
                vals[name][sel] = got[name]
            return True

        for im in np.unique(img):
            sel = (img == im) & valid
            if not sel.any():
                continue
            if not abn[im]:
                ok_any |= read(im, sel)
                continue
            # 0.8.0: 読む画像がポップアップの最中なら、ポップアップが消えた直後のフレームから4つの波形を読む（B1014〜B1019 など）。
            # そのフレームで上書きされていた列は、ポップアップの間も見えている気道内圧の欄から Paw だけを読む
            g, keep = _after_popup_frame(im, pass_col[sel], xi[sel], pas, pen, abn, wrap,
                                         int(round(Settings.SWEEP_REREAD_SEC * fps)))
            later = np.zeros(len(u), bool)
            if g is not None and keep.any():
                later[np.flatnonzero(sel)[keep]] = True
                if read(g, later):
                    img[later] = g
                    ok_any = True
                    popup.append("Reread")
                else:
                    later[:] = False
            rest = sel & ~later
            if rest.any() and paw_seen[im] and read(im, rest, ["Pressure"]):
                ok_any = True
                popup.append("PawOnly")
        if not ok_any:
            recs.append(rec)
            continue
        if popup:
            rec["Wave_Popup"] = ",".join(dict.fromkeys(popup))

        # 縦軸の範囲を超えた波形は上端・下端で切れて描かれる。その後 Hamilton が縦軸を広げて描き直すので、
        # 列がまだ画面に残っている間に縦軸が広がったフレームがあれば、そこから読み直す（無ければ Alert に Clipped）
        for name in Settings.WAVES:
            clipped = _clipped(vals[name], img, valid, name, df_frames)
            # 途中の空欄（2点以上）も、範囲の外へ出て線が読めなかった可能性があるので読み直しの対象にする
            gaps = np.zeros(len(u), bool)
            fin = np.isfinite(vals[name])
            if fin.any():
                inner = np.arange(len(u))
                inner = (inner > np.argmax(fin)) & (inner < len(u) - 1 - np.argmax(fin[::-1]))
                for a2, b2 in _runs(valid & ~fin & inner):
                    if b2 - a2 + 1 >= 2:
                        gaps[a2:b2 + 1] = True
            target = clipped | gaps
            if not target.any():
                continue
            g = _wider_axis_frame(name, img[target].max(), pass_col[target], xi[target],
                                  df_frames, pas, pen, abn, int(round(Settings.SWEEP_REREAD_SEC * fps)))
            if g is None:
                if clipped.any():
                    alerts[k] = ",".join(dict.fromkeys(filter(None, alerts[k].split(",") + ["Clipped"])))
                continue
            path = os.path.join(frames_root, f"frame_{int(frame_no[g]):08d}.jpg")
            frame = cv2.imread(path) if os.path.isfile(path) else None
            if frame is None:
                continue
            # 切れていた列を含む周の列をすべて読み直す（同じ呼吸の中で縦軸がそろうように）
            sel = valid & np.isin(pass_col, np.unique(pass_col[target]))
            got = extract_column_values(_normalize_frame(frame), xi[sel], {name: frame_cal(g)[name]})
            vals[name][sel] = got[name]
            reread.append(name)
        if reread:
            rec["Wave_Reread"] = ",".join(reread)
            reread = []
        wave = pd.DataFrame({"X": u})
        for name in Settings.WAVES:
            wave[name] = np.round(vals[name], 2)
        wave["_sx"] = xi   # 画面の列（区切りを動かした後の Sweep_Start / End 用。書き出さない）

        # 最初の点が描かれた時刻: ペン先が first に達した時刻（各フレームのペン先の位置から内挿）
        items.append({"wave": wave, "t0": float(np.interp(first, tip_u, times)), "rate": rate, "rec": rec, "k": len(recs),
                      "path": os.path.join(output_dir, f"{int(b['BreathNumber'])}.csv")})
        recs.append(rec)

    # 区切りを吸気の始まりへ合わせる（呼吸どうしの波形はつながっているので、点を前後の呼吸の間で移す）
    if opts.get("trim_to_breath", True):
        tri_u = df_frames["Trigger_U"].dropna().to_numpy(float) if "Trigger_U" in df_frames else ()
        _snap_to_breaths(items, tri_u)
    for it in items:
        wave, rec = it["wave"], it["rec"]
        if wave is None or len(wave) == 0:
            continue
        xs = wave["_sx"].to_numpy(int)
        rec["Wave_T0"] = round(it["t0"], 4)
        rec["Wave_Rate"] = round(it["rate"], 4)
        rec["Wave_Points"] = len(wave)
        rec["Sweep_Start"], rec["Sweep_End"] = int(xs[0]), int(xs[-1])
        rec["Wrapped"] = int(bool(np.any(np.diff(xs) < 0)))
        rec["Wave_Trim_Head"], rec["Wave_Trim_Tail"] = it.get("trim_head", 0), it.get("trim_tail", 0)
        rec["Wave_Missing"] = round(float(wave["Pressure"].isna().mean()), 3)
        if rec["Wave_Missing"] > 0.3:
            # 画面操作などで波形の描画が止まった・画面が消去された区間を含み、波形の3割以上が無い
            alerts[it["k"]] = ",".join(dict.fromkeys(filter(None, alerts[it["k"]].split(",") + ["Partial"])))
        if overwrite or not os.path.exists(it["path"]):
            wave.drop(columns=["_sx"]).rename(columns={"Pes": aux}).to_csv(it["path"], index=False)
        written += 1

    for c in new_cols:
        df_breaths[c] = [r[c] for r in recs]
    df_breaths["Alert"] = alerts

    if archive_dir is not None:
        os.makedirs(archive_dir, exist_ok=True)
        with open(os.path.join(archive_dir, "fields.txt"), "w", encoding="utf-8") as f:
            f.write("[waves]\n")
            for name in Settings.WAVES:
                f.write(f"{aux if name == 'Pes' else name}\n")
            f.write("\n[fields]\n")
            read = [k for k in OCR_KEYS if k in df_breaths.columns]
            for name in read + [f"{k}_displayed" for k in read if k in MEASURED_KEYS] + [
                    "Trigger", "Duration", "Sweep_Start", "Sweep_End", "Wrapped", "Alert"]:
                f.write(f"{name}\n")
        meta = {
            "units": {(aux if name == "Pes" else name).lower(): spec["unit"] for name, spec in Settings.WAVES.items()},
            "symmetric": {(aux if name == "Pes" else name).lower(): spec["axis"][0] == -spec["axis"][1] for name, spec in Settings.WAVES.items()},
        }
        with open(os.path.join(archive_dir, "scale_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"[extract_waves] {written} breaths written")
    return written, _rename_aux(df_breaths, aux)
