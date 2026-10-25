"""Hamilton (C6 / G5 family screen) VentOCR Engine

- 1 breath = from an update of the numbers on the left (Compliance, expiratory VT) until the next update
  (update = a new value persisting for 2 or more frames. Blank is also a value. No thinning by time)
- OCR: expiratory VT (VTE), Compliance, peak pressure, minute volume, P0.1, ventilation mode, settings (rate, ΔPinsp, PEEP, oxygen)
  whichever are selected by engine_options initial_ocr.ocr_keys (default: VTE, Compliance, peak pressure. Settings.OCR_FIELDS)
- Waveforms: airway pressure (Pressure), flow (Flow), volume (Volume), esophageal pressure (Pes).
  The vertical-axis scale changes automatically and often, so the tick numbers are read and converted for every breath (axis_scale.py)

The waveform screen sweeps left to right at constant speed; on reaching the right edge it returns to the left edge and overwrites the old waveform (wrap around).
In each frame, the right edge of the "columns changed since the previous frame" is recorded as the sweep position (initial_ocr);
sweep speed and phase are estimated from the whole video to determine the pen-tip position in each frame (post_ocr).
A breath's waveform is cut out of the image of its last frame, from the pen-tip column at its start to the one at its end (extract_waves).
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

ENGINE_VERSION = "0.2.5"
VENTOCR_TESTED = "0.5.5"

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_KEYS = list(Settings.OCR_FIELDS)
TEXT_KEYS = [k for k, f in Settings.OCR_FIELDS.items() if f["type"] == "text"]
MEASURED_KEYS = [k for k, f in Settings.OCR_FIELDS.items() if f["kind"] == "measured"]


def selected_ocr_keys(settings):
    """Values to OCR: those selected by ocr_keys + values used for breath segmentation (breath_trigger). skip_keys (settings of 0.3 and earlier) are excluded."""
    init = settings.get("initial_ocr", {})
    chosen = set(init.get("ocr_keys", Settings.DEFAULT_OCR_KEYS))
    chosen |= set(settings.get("post_ocr", {}).get("breath_trigger", ["Compliance", "VTE"]))
    chosen -= set(init.get("skip_keys", []))
    return [k for k in OCR_KEYS if k in chosen]


# ============================================================
# EasyOCR initialization (same as V600_ventocr)
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
# Common
# ============================================================

def _normalize_frame(frame):
    """Normalize to the reference resolution (1920x1080)."""
    w, h = Settings.BASE_RESOLUTION
    if frame.shape[1] != w or frame.shape[0] != h:
        frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    return frame


def _to_gray(frame):
    # 0.8.3: cv2.cvtColor (up to 0.8.2, np.dot was computed over the whole screen in float64, taking about 70% of initial_ocr time.
    # Pixel values differ by at most 1 due to rounding vs. truncation)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _crop(img, roi):
    x, y, w, h = roi
    return img[y:y + h, x:x + w]


def _runs(mask):
    """List of (start, end) of runs of True (end inclusive)."""
    idx = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _format_time(frame, fps):
    """Build an HH:MM:SS:CC timestamp from a frame number"""
    total_seconds = frame / fps
    s = int(total_seconds)
    centiseconds = int((total_seconds - s) * 100)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{ss:02d}:{centiseconds:02d}"


# ============================================================
# OCR result parsing
# ============================================================

def _load_value_glyphs():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), Settings.VALUE_GLYPH_FILE)
    z = np.load(path)
    return z["templates"], z["widths"], [str(c) for c in z["chars"]]


_VALUE_TEMPLATES, _VALUE_WIDTHS, _VALUE_CHARS = _load_value_glyphs()


def _value_blobs(roi):
    """Measured-value ROI (gray) -> (label image, blob stats [x, y, w, h, area] (excluding background), digit blob indices (left to right), decimal-point blob index or None).
    If no digits are found, the digit indices are empty. Characters are white (≈255) or red (≈76 in grayscale), background is black."""
    _, bw = cv2.threshold(np.asarray(roi, np.uint8), Settings.VALUE_BIN_THRESH, 255, cv2.THRESH_BINARY)
    _, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    st = st[1:]
    digits = sorted(np.flatnonzero(st[:, cv2.CC_STAT_HEIGHT] >= 0.4 * bw.shape[0]), key=lambda i: st[i, 0])  # at least 40% of the ROI height
    if not digits:
        return lab, st, [], None
    big = st[digits]
    bottom = (big[:, 1] + big[:, 3]).max()
    h = bottom - big[:, 1].min()          # digit height (about 80 px at 1080p; the decimal point is about 14 px square)
    for i, (x, y, w, hh, a) in enumerate(st):
        if 0.08 * h <= w < 0.3 * h and 0.08 * h <= hh < 0.3 * h and y + hh >= bottom - 0.15 * h:
            return lab, st, digits, i
    return lab, st, digits, None


def _has_dot(roi):
    """Whether the ROI (gray) has a decimal point: look for a small blob near the bottom of the digits. None if no digits are found."""
    _, _, digits, dot = _value_blobs(roi)
    return None if not digits else dot is not None


def _read_glyphs(roi):
    """Read the digits of a measured-value ROI (gray) one by one by template matching -> value string ("41.2", "126", etc.).
    None (use the EasyOCR value) when a digit's template correlation is below VALUE_MATCH_MIN, the decimal point is not between the last two digits, etc."""
    lab, st, digits, dot = _value_blobs(roi)
    if not digits:
        return None
    text = ""
    for i in digits:
        x, y, w, h, _ = st[i]
        v = cv2.resize((lab[y:y + h, x:x + w] == i + 1).astype(np.float32), (_VALUE_TEMPLATES.shape[2], _VALUE_TEMPLATES.shape[1]),
                       interpolation=cv2.INTER_AREA)
        v -= v.mean()
        n = np.linalg.norm(v)
        if not n:
            return None
        score = (_VALUE_TEMPLATES * (v / n)).sum(axis=(1, 2))
        score[np.abs(_VALUE_WIDTHS - w) > Settings.VALUE_WIDTH_TOL] = -1
        k = int(np.argmax(score))
        if score[k] < Settings.VALUE_MATCH_MIN:
            return None
        text += _VALUE_CHARS[k]
    if dot is not None:
        if len(digits) < 2 or not (st[digits[-2], 0] + st[digits[-2], 2] <= st[dot, 0] and st[dot, 0] + st[dot, 2] <= st[digits[-1], 0]):
            return None
        text = text[:-1] + "." + text[-1]
    return text


def _parse_ocr(result, key, roi=None):
    """EasyOCR result -> (value string, raw string, confidence string).

    How to read follows type in Settings.OCR_FIELDS: int = integer, text = text (ventilation mode),
    dec1 = displayed with one decimal, but the decimal point can be missed, so numbers of dot_min_digits or more digits without a point
    get a point inserted before the last digit (the raw string is kept in *_raw). P0.1 also reads negative values (-).
    For values with dot_check, the ROI pixels are checked for a decimal point before inserting; if none, treated as an integer (only when roi is given).
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
    digits = re.sub(r"\.{2,}", ".", re.sub(r"[^0-9.]", "", joined))   # 0.2.2: "47..4" (point read twice) -> 47.4
    if not re.fullmatch(r"\d+\.?\d*", digits or ""):
        return "", raw, conf
    if field["type"] == "int":
        # 0.2.5: small_decimal values keep a one-decimal display below 10 ("9.6") as a decimal
        value = digits if field.get("small_decimal") and re.fullmatch(r"\d\.\d", digits) else digits.split(".")[0]
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
    Read the selected values (selected_ocr_keys) in each frame and record the sweep position (right edge of the columns changed since the previous frame).

    Numbers are OCRed only in frames where the image at the read position changed since the last OCR; otherwise the previous value is carried over.

    Returns:
        pd.DataFrame (columns: Frame, BreathMark, <value>, <value>_raw, <value>_conf (for each selected value),
                      OCR_Done, Sweep_Raw, Sweep_Run, Trigger_Apexes, Axis_OK, Axis_Pressure, Axis_Flow, Axis_Volume, Axis_Pes)
        Axis_<wave> is the conversion derived from the vertical-axis ticks "a;b;min tick;max tick" (value = a*y + b). Empty if unreadable
    """
    opts = settings.get("initial_ocr", {})
    keys = selected_ocr_keys(settings)
    gpu_enabled = opts.get("use_gpu", True)
    diff_thresh = float(opts.get("diff_thresh", 3.0))

    reader = _ensure_easyocr_reader_ready(gpu_enabled)
    print(f"GPU:{gpu_enabled}  OCR: {', '.join(keys)}")

    ax_x0, ax_x1 = Settings.AXIS_LABEL_X
    last_axis_img, last_axis = None, None
    axis_retry = 0   # frames left to re-read after a read that missed a waveform's labels (0.2.5)
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

            # --- numbers (OCR only when the display changed) ---
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
                    if field.get("glyph_read"):
                        g = _read_glyphs(roi)
                        if g is not None:
                            last_val[key] = (g,) + last_val[key][1:]   # *_raw / *_conf stay as EasyOCR
                    last_roi[key] = roi
                    row["OCR_Done"] = 1
                value, raw, conf = last_val[key]
                row[key], row[f"{key}_raw"], row[f"{key}_conf"] = value, raw, conf

            # --- sweep position: right edge of the largest run of columns changed since the previous frame ---
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
            row["Sweep_Run"] = sweep_run   # width of the changed columns (1000+ columns on a full-screen redraw, which is not a sweep position)

            # --- x of the apexes of patient-trigger triangles (";"-separated). Whether they are newly drawn is judged in post_ocr from the pen-tip position ---
            row["Trigger_Apexes"] = ";".join(f"{x:.1f}" for x in trigger_apexes(frame))

            # --- fraction of bright pixels covering the waveform area (0.2.4. Settings dialogs etc. The area is thin lines on black, normally <= 0.02) ---
            (px0, py0, px1, py1), st = Settings.POPUP_AREA, Settings.POPUP_GRID
            row["Wave_Covered"] = round(float((gray[py0:py1:st, px0:px1:st] > Settings.POPUP_BRIGHT).mean()), 3)

            # --- vertical-axis ticks (read only when the number column changed). Axis_OK = number of waveforms with readable ticks ---
            axis_img = gray[:, ax_x0:ax_x1].astype(np.int16)
            # 0.2.5: after a read that missed a waveform's labels (e.g. read mid-change), also re-read the next AXIS_RETRY_FRAMES frames;
            # otherwise the failure stayed until the label column changed again (Ptranspulm 2026-10-09, B829-B832 with undecoded frames:
            # 9.5 s on the old scale). Limited so a screen that never shows all labels does not re-read every frame
            if last_axis_img is None or axis_retry > 0 or np.abs(axis_img - last_axis_img).mean() > 1.0:
                changed = last_axis_img is None or np.abs(axis_img - last_axis_img).mean() > 1.0
                axis = axis_scale.read_axis(gray)
                last_axis = (axis_scale.encode(axis), sum(v is not None for v in axis.values()))
                last_axis_img = axis_img
                if last_axis[1] >= len(Settings.AXIS_PANELS):
                    axis_retry = 0
                elif changed:
                    axis_retry = Settings.AXIS_RETRY_FRAMES
                else:
                    axis_retry -= 1
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
# Sweep tracking
# ============================================================
# Up to 0.5, a model "constant speed, returns to the left edge at x=1567 every 22 s" was fitted to the whole video, using only nearby detections.
# In practice
#   - drawing stops due to screen operations, the whole screen is cleared, and it returns to the left edge midway (Ptranspulm 2026-10-09, B243, frame 8293)
#   - frame timestamps shift at the boundaries of 1-minute segments, and the sweep position jumps by about 10 px
# and since the model smoothed over +-30 s, the pen-tip position was off around those points and the waveforms became garbage.
# So, as in V600, the pen tip is tracked directly in every frame.

def track_sweep(sweep_raw, sweep_run, fps, abnormal=None):
    """Track the pen tip in each frame.

    Args:
        sweep_raw: sweep position detected in each frame (right edge of the run of columns changed since the previous frame, screen x). NaN if none
        sweep_run: width of that run (runs exceeding SWEEP_RUN_MAX are full-screen redraws, not sweep positions)
        abnormal: Abnormal flag of each frame (popups etc.). The sweep continues during popups and is detectable in the airway-pressure area at the top, but
            popup drawing is also detected as change, so a restart from the left edge is accepted only when drawing should have reached the right edge (0.8.0)
    Returns:
        dict(
            pen:   pen tip in each frame (screen x),
            u:     cumulative position (total columns drawn; continuous across returns to the left edge),
            pass_: pass index (+1 on each return to the left edge),
            wrap:  whether it returned to the left edge just before this frame (0: no, 1: drew to the right edge and returned, 2: cleared midway and returned),
            detected: whether the frame used a detected value,
            speed: sweep speed (px/frame),
        )
    """
    XL, XR = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT
    raw = np.asarray(sweep_raw, float) - Settings.SWEEP_ERASE_OFFSET
    run = np.asarray(sweep_run, float) if sweep_run is not None else np.full(len(raw), np.nan)
    # only runs wider than the per-frame advance (sweep speed / fps) count as redraws (at low fps it advances 40 px or more per frame. engine_api 2)
    raw[run > max(Settings.SWEEP_RUN_MAX, 3 * Settings.SWEEP_PX_PER_SEC / fps)] = np.nan
    n = len(raw)
    speed = Settings.SWEEP_PX_PER_SEC / fps
    if n == 0:
        return {"pen": raw, "u": raw, "pass_": np.zeros(0, int), "wrap": np.zeros(0, int),
                "detected": np.zeros(0, bool), "speed": speed}

    # speed: median of (advance between consecutively detected values / frames between them) (per frame 0 < d < 4x the default).
    # Up to 0.8.1 only differences between detections in adjacent frames were used. At high fps the per-frame advance is small (1.7 px at 30 fps),
    # and detections are biased to frames with large advances, so speed was overestimated (90 px/s at 30 fps. engine_api 2)
    vi = np.flatnonzero(np.isfinite(raw))
    if len(vi) >= 2:
        gap = np.diff(vi)
        step = np.diff(raw[vi]) / gap
        ok = (gap <= max(1, int(round(fps)))) & (step > 0) & (step < 4 * speed)
        if ok.sum() >= 20:
            speed = float(np.median(step[ok]))
    margin = Settings.SWEEP_RESTART_MARGIN

    # check detections in order: adopt only those that advance naturally from the last adopted position, or restart from the left edge
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
                # do not mistake popup drawing for a restart from the left edge (Ptranspulm 2026-10-09, frame 30471: up to 0.7.4
                # this was taken as the next pass, and the pen tip was off during the following popup)
                restart = False
            if not (-1 <= dd <= 2 * speed * gap + 8 or restart):
                continue
        acc[i] = p
        last = (i, p)

    # pass index: next pass when an adopted detection moves left by SWEEP_WRAP_JUMP or more
    pass_ = np.zeros(n, int)
    wrap = np.zeros(n, int)
    vi = np.flatnonzero(np.isfinite(acc))
    for a, b in zip(vi[:-1], vi[1:]):
        # 0.2.5: also the next pass when it moves back by SWEEP_RESTART_MARGIN or more to near the left edge (the screen was cleared right after a normal wrap,
        # and the jump did not reach SWEEP_WRAP_JUMP. Ptranspulm 2026-10-09, B854: x 523 -> 472)
        back_to_left = acc[b] < acc[a] - margin and acc[b] <= XL + speed * (b - a) + margin
        if acc[b] < acc[a] - Settings.SWEEP_WRAP_JUMP or back_to_left:
            near_right = acc[a] + speed * (b - a) >= XR - 1 - margin
            pass_[b:] += 1
            wrap[b] = 1 if near_right else 2
    # fill gaps: linearly from neighboring detections within a pass; after the end of a pass, advance by speed (stop at the right edge)
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

    # cumulative position: within a pass, the pen-tip advance. When it drew to the right edge and returned, the columns up to the right edge plus those from the left edge;
    # when cleared midway and returned, nothing was drawn, so advance only by the elapsed time (speed x 1 frame)
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


def _clear_events(pass_, wrap, detected, sweep_run, fps):
    """When the screen was cleared midway and returned to the left edge (wrap = 2): list of (frame where the pen tip was last found, frame where it was cleared, frame where the return to the left edge was recognized)
    (row indices. 0.2.3).

    The return is recognized when the pen tip is found at the left edge, so the few frames before stay in the same pass although the screen is already cleared
    (Ptranspulm 2026-10-09, B541: pen tip found up to 17392, cleared at 17393, return recognized at 17395).
    Cleared frame = the first frame after the last pen-tip detection of the pass before the return in which the whole screen changed (the run of changed columns
    exceeded the redraw width). There may be a delay between drawing stopping and clearing (B989-991: stopped at 30069, cleared at 30120).
    If not found, the frame after the last pen-tip detection"""
    pass_, wrap, detected = (np.asarray(a) for a in (pass_, wrap, detected))
    run = np.asarray(sweep_run, float) if sweep_run is not None else np.full(len(pass_), np.nan)
    big = run > max(Settings.SWEEP_RUN_MAX, 3 * Settings.SWEEP_PX_PER_SEC / fps)
    out = []
    for i in np.flatnonzero(wrap == 2):
        seen = np.flatnonzero((pass_[:i] == pass_[i - 1]) & detected[:i].astype(bool))
        last = int(seen[-1]) if len(seen) else i - 1
        jumps = last + 1 + np.flatnonzero(big[last + 1:i + 1])
        out.append((last, int(jumps[0]) if len(jumps) else last + 1, int(i)))
    return out


# ============================================================
# post_ocr
# ============================================================

BLANK = "<blank>"   # display is blank ("---" / unreadable)


def confirmed_changes(values, stable=2):
    """Frames (row indices) where a change begins in which a new value persists for stable frames or more in a value column.

    Values lasting only 1 frame (fewer than stable) are ignored: A->B (1 frame)->C gives the start of C; A->B (1 frame)->A is no change.
    A value lasting until the end counts as a change even if shorter than stable (end of recording).
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
    """Value shown between one display update and the next (mode; on a tie, the one shown later).
    The later value is preferred so that a short interval does not pick the previous value when some columns update 1 frame late."""
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
    """Positions of newly drawn patient-trigger triangles = inspiration onsets. (list of row indices, list of apex cumulative positions u) (in time order).
    sweep_u: cumulative position of each frame (Sweep_U). u is NaN if absent.

    apexes: Trigger_Apexes of each frame (apex x values joined with ";"), pen: pen tip of each frame (screen x),
    hidden: frames where the airway-pressure area is not visible (popups etc.), speed: sweep speed (px/frame).
    An apex absent in the previous frame and just behind the pen tip (TRIGGER_NEW_LAG_PX) is a new triangle,
    and the time (row index) when the pen tip passed its position is returned. Triangles from the previous pass (remaining right of the pen tip) and
    frames where the airway-pressure area is not visible (and the frame after) are not used."""
    lo, hi = Settings.TRIGGER_NEW_LAG_PX
    hi = max(hi, 4 + 2 * speed)   # at low fps the pen tip has already advanced one frame (speed px) when the triangle is seen (engine_api 2)
    keep_frames = max(2, int(round(Settings.TRIGGER_KEEP_SEC * fps)))
    width = Settings.SWEEP_X_RIGHT - Settings.SWEEP_X_LEFT   # one pass (px)
    half_w = width / 2
    xs = [[float(v) for v in str(text).split(";") if v not in ("", "nan")] for text in apexes]
    n = len(xs)

    def stays(i, x):
        """Whether apex x still remains in a frame after the pen tip has advanced TRIGGER_KEEP_PX or more (redrawn, not erased)"""
        for j in range(i + 1, min(i + 1 + keep_frames, n)):
            if hidden[j] or not np.isfinite(pen[j]):
                return False
            ahead = pen[j] - x if pen[j] - x > -half_w else pen[j] - x + width   # 0.2.5: wrap-aware
            if ahead >= Settings.TRIGGER_KEEP_PX:
                return any(abs(x - q) <= Settings.TRIGGER_SAME_PX for q in xs[j])
        return False

    found = []
    for i in range(1, n):
        # the first frame has no previous frame, so new triangles cannot be judged (triangles remaining on screen were counted, their time became a negative
        # row index before the start of the video and post_ocr stopped: KeyError '[-2] not in index' when analyzing a clip cut from mid-video. 0.8.2)
        if hidden[i] or hidden[i - 1] or not np.isfinite(pen[i]):
            continue
        prev = xs[i - 1]
        for x in xs[i]:
            lag = pen[i] - x
            # 0.2.5: wrap-aware (a triangle drawn at the left edge while the pen is still recorded at the right edge, or the reverse;
            # Ptranspulm 2026-10-09, B911: apex x 468.5 seen at frame 27994 with the pen at x 1566)
            if lag > half_w:
                lag -= width
            elif lag < -half_w:
                lag += width
            if not lo <= lag <= hi:
                continue
            same = [q for q in prev if abs(x - q) <= Settings.TRIGGER_SAME_PX]
            if same:
                # a new triangle may be drawn at almost the same position as one from the previous pass (Ptranspulm 2026-10-09, B237, B452).
                # if it was right of the pen tip (previous pass) in the previous frame and still remains after the pen tip passed, it was redrawn
                # (columns the pen tip passes are erased, so a triangle from the previous pass disappears)
                # 0.2.5: also from the previous pass when the pen is at the right edge and the triangle at the left edge (within
                # TRIGGER_WRAP_PX across the wrap). Ptranspulm 2026-10-09, B417: redrawn at x 472 while the pen was at x 1562
                if not (all(q > pen[i - 1] or pen[i - 1] - q > width - Settings.TRIGGER_WRAP_PX for q in same) and stays(i, x)):
                    continue
            found.append((max(i - max(lag, 0) / speed, 0.0), sweep_u[i] - lag if sweep_u is not None else np.nan))
    out = []
    for t, uu in sorted(found):
        if not out or t - out[-1][0] > Settings.TRIGGER_DEDUP_SEC * fps:
            out.append((t, uu))
    return [int(round(t)) for t, _ in out], [uu for _, uu in out]


def aux_wave_name(settings):
    """Output name of the 4th-row pressure (0.2.0). Hamilton can show Pes or Ptranspulm (transpulmonary pressure = Paw − Pes) in the 4th row,
    and the user selects which in the settings (post_ocr.ptranspulm) (assumed not to switch mid-recording). Inside the engine both are handled as "Pes";
    only the output names change (BreathCSVs columns, Axis_<wave> / <wave>_min/_max in Breaths.csv and Frames.csv, fields.txt, scale_meta.json)"""
    return "Ptranspulm" if settings.get("post_ocr", {}).get("ptranspulm", False) else "Pes"


def _rename_aux(df, aux, to_output=True):
    """Rename the 4th-row pressure columns between the internal name (Pes) and the output name (aux)."""
    if aux == "Pes":
        return df
    a, b = ("Pes", aux) if to_output else (aux, "Pes")
    names = {f"Axis_{a}": f"Axis_{b}", f"Axis_Filled_{a}": f"Axis_Filled_{b}", f"{a}_min": f"{b}_min", f"{a}_max": f"{b}_max"}
    return df.rename(columns={k: v for k, v in names.items() if k in df.columns})


def post_ocr(df, Analyze_Id, user_ID, settings={}):
    """
    Determine breath boundaries (numeric display updates) and assign breath numbers and pen-tip positions.

    Returns:
        (df_frames, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    triggers = [k for k in opts.get("breath_trigger", ["Compliance", "VTE"]) if k in df.columns]
    # stable_frames is a frame count at 10 fps. At other fps it is converted to the same duration (engine_api 2)
    stable = max(1, int(round(int(opts.get("stable_frames", 2)) * fps / 10)))
    merge = max(1, int(round(Settings.UPDATE_MERGE_SEC * fps)))
    shift = int(round(float(opts.get("boundary_shift_sec", -0.5)) * fps))

    aux = aux_wave_name(settings)
    df = _rename_aux(df, aux, to_output=False).sort_values("Frame").reset_index(drop=True).copy()
    n = len(df)
    keys = [k for k in OCR_KEYS if k in df.columns]   # values read in initial_ocr
    for col in keys:
        if col in TEXT_KEYS:
            df[col] = df[col].fillna("").astype(str).str.strip().replace("", np.nan)
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # non-waveform frames = fewer than AXIS_MIN_PANELS waveforms with readable ticks (popup, another screen).
    # Up to 0.4 the tick column was compared with the reference image, so a mere axis scale change became Abnormal
    n_axis = pd.to_numeric(df["Axis_OK"], errors="coerce").fillna(0) if "Axis_OK" in df else pd.Series(0, index=df.index)
    df["Abnormal"] = (n_axis < Settings.AXIS_MIN_PANELS).astype(int)
    # when ticks are unreadable for only one waveform (e.g. characters not in the templates), inherit the last readable ticks (Axis_Filled_<wave> = 1)
    for name in Settings.WAVES:
        col = f"Axis_{name}"
        if col in df:
            read = df[col].map(lambda t: axis_scale.decode(t) is not None)
            df[f"Axis_Filled_{name}"] = (~read).astype(int)
            df[col] = df[col].where(read).ffill().fillna("")

    # --- pen-tip position ---
    sw = track_sweep(df["Sweep_Raw"].to_numpy(float) if "Sweep_Raw" in df else np.full(n, np.nan),
                     df["Sweep_Run"].to_numpy(float) if "Sweep_Run" in df else None, fps, df["Abnormal"].to_numpy(int))
    df["Sweep_U"] = np.round(sw["u"], 2)
    df["Sweep_X"] = np.round(sw["pen"], 1)
    df["Sweep_Pass"] = sw["pass_"]
    df["Sweep_Wrap"] = sw["wrap"]
    df["Sweep_Detected"] = sw["detected"].astype(int)
    df["Sweep_Rate"] = round(sw["speed"] * fps, 4)  # px/s
    cleared = np.zeros(n, bool)   # from the screen being cleared until the return to the left edge is recognized
    for _, j, i in _clear_events(sw["pass_"], sw["wrap"], sw["detected"], df["Sweep_Run"] if "Sweep_Run" in df else None, fps):
        cleared[j:i + 1] = True
    print(f"[post_ocr] sweep {sw['speed'] * fps:.2f} px/s, detected {sw['detected'].mean() * 100:.1f}% of frames, "
          f"{int((sw['wrap'] == 1).sum())} wraps, {int((sw['wrap'] == 2).sum())} restarts")

    # --- breath boundaries: display updates ---
    # 0.7.0: for each trigger column (VTE, Compliance), a new value persisting for stable_frames (2) frames or more is a "change".
    # The boundary is the first frame the value appeared. Blank ("---" / unreadable) is also treated as a value. Values lasting only 1 frame are ignored.
    # Changes in the two columns 1 frame apart are one update (the display rewrite spanned 2 frames) and segment once, at the first frame.
    # Up to 0.6, updates closer than min_breath_sec (1 s) were merged, but thinning by time was dropped
    # 0.7.3: even in Abnormal frames (waveform axis unreadable), updates are used if the numeric fields are readable.
    # Up to 0.7.2 all changes during Abnormal were ignored, so breaths during popups etc. were merged into one
    # (Ptranspulm 2026-10-09, B1012: about 13 s, 6 breaths). To avoid false boundaries from runs of blanks when the numeric fields are also covered
    # by a popup, only blanks (unreadable) in Abnormal frames inherit the previous value
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

    # 0.8.0: if there is no display update near a patient-trigger triangle (inspiration onset), segment as if an update occurred shortly after the triangle.
    # If consecutive breaths happen to have the same VTE / Compliance, the display does not change even though Hamilton records 2 breaths
    # (Ptranspulm 2026-10-09, B87: Study Recorder 8899 and 8900 both 404 mL / 52.8)
    abn = df["Abnormal"].to_numpy(int)
    # triangles are used even in Abnormal frames if the airway-pressure axis was readable (airway-pressure area visible) (popups often cover the lower areas)
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
            continue   # near the end of the recording or Abnormal, the display update may just not be visible
        if any(t - before <= u <= t + after for u in display_updates):
            continue
        split_updates.append(t + lag)
    updates = sorted(set(updates) | set(split_updates))

    # shift boundaries by boundary_shift_sec (display updates lag inspiration onset slightly)
    bounds = sorted({min(max(u + shift, 0), n - 1) for u in updates})
    df["BreathMark"] = 0
    df.loc[bounds, "BreathMark"] = 1
    df["BreathNumber"] = df["BreathMark"].cumsum()
    if not bounds or bounds[0] > 0:
        df["BreathNumber"] += 1          # before the first boundary is breath 1 (a breath starting mid-way)
    df["Display_Update"] = 0
    df.loc[display_updates, "Display_Update"] = 1
    df["Trigger_New"] = 0
    df.loc[triggers_at, "Trigger_New"] = 1     # newly drawn triangle (inspiration onset)
    df["Trigger_U"] = np.nan
    df.loc[triggers_at, "Trigger_U"] = np.round(triggers_u, 1)   # cumulative position of the triangle apex (used to align boundaries in extract_waves)
    df["Trigger_Split"] = 0
    df.loc[split_updates, "Trigger_Split"] = 1  # boundary added by a triangle (position treated as having a display update)

    df["Time"] = df["Frame"].apply(lambda f: _format_time(f, fps))

    # --- per-breath table ---
    # 0.7.1: measured values are assigned using display-update (Display_Update) intervals.
    # Value of breath i = value shown from the update that started breath i+1 until the next update.
    # Up to 0.7.0 it was "the mode over the next breath's interval (from 0.5 s before the update)", so when the next breath was 1 s or shorter
    # the pre-update (previous breath's) value dominated and the value one breath earlier was used (Ptranspulm 2026-10-09, B990 etc.)
    start_update = {}   # first row of a breath -> row of the display update that started it
    for u in updates:
        start_update.setdefault(min(max(u + shift, 0), n - 1), u)
    rows = []
    groups = list(df.groupby("BreathNumber", sort=True))
    windows = []        # per-breath row range "update that started the breath - next update" (None for the first breath if not started by an update)
    for k, (_no, g) in enumerate(groups):
        u0 = start_update.get(int(g.index[0]))
        if u0 is None:
            windows.append(None)
            continue
        u1 = start_update.get(int(groups[k + 1][1].index[0]), n) if k + 1 < len(groups) else n
        windows.append((u0, u1))

    def shown(key, window):
        # Abnormal frames are used too (the numeric fields are readable; unreadable blanks are excluded by _shown_value)
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
        # the measured values on the left belong to the previous breath: this breath's value = value shown at the update that started the next breath (regardless of the next breath's length).
        # *_displayed = value shown at the update that started this breath (= previous breath's value; for the first breath, the display in its interval)
        # ventilation mode and settings are the values shown during this breath
        for key in keys:
            if key in MEASURED_KEYS:
                row[key] = shown(key, windows[k + 1]) if k + 1 < len(groups) else np.nan
                row[f"{key}_displayed"] = shown(key, windows[k]) if windows[k] is not None else _mode(normal[key])
            else:
                row[key] = _mode(normal[key])
        # patient trigger: A (spontaneous) if a new triangle was drawn during this breath, otherwise C (controlled).
        # if the breath contains frames where the airway-pressure area is not visible and there is no triangle, blank, since it may have been hidden
        # 0.2.4: breaths containing frames where a settings dialog covered the waveform area are also blank, since the triangle may have been hidden
        covered = bool((g["Wave_Covered"] >= Settings.POPUP_COVER_MIN).any()) if "Wave_Covered" in g else False
        if g["Trigger_New"].any():
            row["Trigger"] = "A"
        else:
            row["Trigger"] = "" if g["Paw_Hidden"].any() or covered else "C"
        alerts = []
        # 0.2.5 (A61): splitting by a triangle without a display update is part of the algorithm, not an alert (no TriggerSplit any more;
        # Frames.csv Trigger_Split still marks the position)
        if k == 0 and (not bounds or bounds[0] > 0):
            alerts.append("Fragment")   # breath starting mid-recording
        if nxt is None:
            alerts.append("Fragment")   # breath cut off at the end of the recording
        # 0.2.5 (A61): Abnormal (axis labels unreadable: popup or another screen) and the settings-dialog check (Wave_Covered) are both
        # reported as one alert, Hidden; the two checks are unchanged (the Abnormal column is kept)
        if g["Abnormal"].any():
            alerts.append("Hidden")
        if cleared[s:e + 1].any():
            alerts.append("Cleared")   # 0.2.3: the ventilator screen (waveforms) was cleared during this breath
        if covered:
            alerts.append("Hidden")    # 0.2.4: a settings dialog etc. covered the waveform area (excluded from accuracy evaluation)
        row["Abnormal"] = int(g["Abnormal"].any())
        # axis conversion = that of the last frame from which the waveform is cut.
        # 0.8.1: P_min / P_max etc. are the values at the bottom/top of the drawing area (the range visible on the ventilator screen). Up to 0.8.0 they were the min/max tick numbers,
        # and since the drawing area extends below the lowest tick, waveforms in that range (Ptranspulm, negative Volume, etc.) fell outside the chart.
        # the min/max ticks remain as the 3rd and 4th fields of Axis_<wave>
        for name, spec in Settings.WAVES.items():
            text = df.at[e, f"Axis_{name}"] if f"Axis_{name}" in df else None
            cal = axis_scale.decode(text)
            row[f"Axis_{name}"] = text if cal is not None else ""
            a, b = cal[:2] if cal else _CAL[name]
            top, bottom = spec["area"]
            row[f"{spec['scale_prefix']}_min"], row[f"{spec['scale_prefix']}_max"] = round(a * bottom + b, 2), round(a * top + b, 2)
            filled = cal is None or (f"Axis_Filled_{name}" in df and df.at[e, f"Axis_Filled_{name}"] == 1)
            if filled and not row["Abnormal"]:
                alerts.append("Axis")    # ticks unreadable in this frame; convert with the previous ticks (or defaults). "Axis?" up to 0.2.4
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
    """Line through ticks (y, value) -> (slope, intercept). value = slope*y + intercept"""
    ys, vs = zip(*spec["ticks"])
    a, b = np.polyfit(ys, vs, 1)
    return float(a), float(b)


_CAL = {name: _calibration(spec) for name, spec in Settings.WAVES.items()}


# ============================================================
# How waveform points are chosen (same approach as NAVA v1.3)
# ============================================================
# Choosing one point per column, at steep spots (vertical strokes within a column) the chosen height is random, producing fake spikes
# (when picking e.g. the brightest pixel), or lands mid-stroke and makes V-shaped tips shallow (with the median).
# For each column, take the "range the line passed through", then
#   1) among vertical pixel runs, globally optimally choose the line connecting to neighboring columns (removes noise and text)
#   2) globally optimally draw a smooth path within the ranges (vertical strokes become slopes, no jumps)
#   3) where a range locally protrudes from the path (tips of V-shaped valleys/peaks), pin to that value and redraw
# px values are for a 1280x720 screen. Scaled via trace_columns(scale=screen height/720).
TRACE_RUN_JOIN = 4          # in a column, gaps shorter than this many pixels are joined into one run
TRACE_SEG_GAP = 12          # if column indices differ by more than this, treat as a separate line and cut the path (columns)
TRACE_HUE_WEIGHT = 0.5      # weight applied to color deviation (cost) in run selection
TRACE_STEP = 0.5            # height step of the path
TRACE_TIP_MARGIN = 2.0      # columns where the range protrudes from the path by this much or more are tip candidates
TRACE_TIP_DROP = 2.0        # on both sides of a tip, the range edge must recede by this much or more
TRACE_TIP_WIN = 8           # number of columns checked on each side of a tip
TRACE_TIP_SUPPORT = 2       # at least this many columns must reach the tip depth (±TRACE_TIP_SUPPORT_PX)
TRACE_TIP_SUPPORT_PX = 1.5
TRACE_MAX_WIDTH = 5         # maximum line thickness


def _trace_runs(mask, cost, join):
    """Vertical runs per column [(top row, bottom row, color deviation)]."""
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
    """Choose the runs of a single line connecting to neighboring columns (Viterbi). ((top, bottom) or None per column, list of segment column indices)"""
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
    """Path minimizing sum(difference to neighboring column^2 / column spacing) within each column's range [lo_i, hi_i]."""
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
    """Tips {position: value} where the range (upper a / lower b, relative to the center line) locally protrudes from path y."""
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
    """mask: waveform-colored pixels (row, column). cost: color deviation of the same shape (smaller = more waveform-like).
    Returns: center row of the line per column (float, NaN for columns without pixels). scale = screen height / 720."""
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
    """Per waveform (band top y, mask of pixels of its color, hue difference)."""
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
    """List of x (screen columns) of patient-trigger triangle apexes. frame is BGR already passed through _normalize_frame."""
    y0, y1 = Settings.TRIGGER_ROWS
    # 0.2.5: search only the waveform area (plus a margin for the triangle width). Undecoded (not re-encoded) frames show pink in the
    # numbers panel on the left (x about 52), which was picked up as a triangle
    xa, xb = Settings.SWEEP_X_LEFT - Settings.TRIGGER_X_MARGIN, Settings.SWEEP_X_RIGHT + Settings.TRIGGER_X_MARGIN
    hsv = cv2.cvtColor(frame[y0:y1, xa:xb], cv2.COLOR_BGR2HSV)
    d = np.abs(hsv[..., 0].astype(np.int16) - Settings.TRIGGER_HUE)
    d = np.minimum(d, 180 - d)
    mask = ((d <= Settings.TRIGGER_HUE_TOL) & (hsv[..., 1] >= Settings.TRIGGER_SAT_MIN)
            & (hsv[..., 2] >= Settings.TRIGGER_VAL_MIN)).astype(np.uint8)
    # triangles overlapping the white time-axis tick numbers bleed in color from video compression and split into fragments, so close them into one run
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
        out.append(float(xa + x + xs[top].mean()))
    return out


def _fill_small_gaps(v, max_gap):
    v = v.copy()
    nan = ~np.isfinite(v)
    for a, b in _runs(nan):
        if a > 0 and b < len(v) - 1 and b - a + 1 <= max_gap:
            v[a:b + 1] = np.interp(np.arange(a, b + 1), [a - 1, b + 1], [v[a - 1], v[b + 1]])
    return v


def extract_column_values(frame, xs, cal=None):
    """For each screen column xs (screen x, in time order), compute each waveform's value (physical units).
    cal: {wave name: (a, b)} (value = a*y + b, read from the vertical-axis ticks). Waveforms not present use the default ticks (ticks in Settings.WAVES).
    0.3.0: draw a globally optimal path through each column's "range the line passed through", keeping V-shaped tips (trace_columns).
    Previously the median y of each column's pixels was used, which landed mid-stroke in columns with vertical strokes (e.g. Flow inspiration->expiration switch) and made tips shallow."""
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
# Align breath boundaries to inspiration onset (same idea as the trimming in NAVA v1.3)
# ============================================================
# 1 breath = inspiration onset to next inspiration onset.
# For breaths whose waveforms join without gaps in time, move the boundary to "TRIM_LEAD_SEC before the nearby inspiration onset"
# and move rows between the adjacent breaths (no data is lost). At the ends of a joined group (no waveform in the neighboring breath),
# trim what precedes inspiration onset (end of the previous expiration) and a final inspiration without expiration (part of the next breath's inspiration).
ONSET_FLOW = 4.0        # Flow above this (L/min) is inspiratory flow
ONSET_RUN_SEC = 0.06    # inspiratory flow must last at least this long
ONSET_PEAK = 12.0       # that inspiration must reach at least this (L/min)
QUIET_SEC = 0.06        # before inspiration, there must be a non-inspiratory section at least this long
ONSET_FRAC = 0.2        # from the point exceeding this fraction of the inspiratory peak, trace back to the foot of the rise
EXP_FLOW = 4.0          # expiratory flow (L/min, at least this much below 0)
BACKTRACK_TOL = 0.3     # when tracing back to the foot, keep going while it keeps dropping by this (L/min) or more
TRIM_LEAD_SEC = 0.04    # keep from this long before inspiration onset
CONTIG_TOL_SEC = 0.15   # joined if the time after the previous breath's last point and the time of the next breath's first point differ by at most this (s) (allows for timing-estimate error)


def _inspiration_onsets(flow, rate):
    """flow: Flow (L/min, NaN allowed), rate: points/s. List of row indices of inspiration onsets (foot of the rise)."""
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


def _gap_between(valid, i, j, gap_pts):
    """Whether rows i..j (either order) contain a run of gap_pts or more rows with no waveform value."""
    a, b = sorted((int(i), int(j)))
    runs = _runs(~np.asarray(valid[a:b + 1], bool))
    return any(e - s + 1 >= gap_pts for s, e in runs)


def _snap_to_breaths(items, trigger_u=()):
    """items: list of dicts in time order {"wave": DataFrame (X, Flow columns, rows = points) or None, "t0": s, "rate": points/s}.
    Writes into each item "wave" (re-cut waveform), "t0", "rate", "trim_head", "trim_tail" (points trimmed/given away from the original range; negative = points received from neighbors).
    trigger_u: cumulative positions of patient-trigger triangle apexes (same unit as X). 0.8.1: spontaneous breath boundaries snap to the triangle, not the flow inspiration onset"""
    trigger_u = np.sort(np.asarray([t for t in trigger_u if np.isfinite(t)], float))
    # split into groups of joined breaths
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
        # triangle positions (rows of cat). The triangle is exactly where Hamilton judged a spontaneous trigger; the flow rise is not found when the previous inspiratory flow
        # continues into the next breath, and when triggered mid-expiration it traces back to the bottom of expiration
        # (Ptranspulm 2026-10-09, B79/80, B94, B96: boundaries were 0.3-0.5 s earlier than Hamilton's breath start)
        xs_cat = cat["X"].to_numpy(float)
        tri = trigger_u[(trigger_u >= xs_cat[0]) & (trigger_u <= xs_cat[-1])]
        tri = np.array([int(np.argmin(np.abs(xs_cat - t))) for t in tri], int)
        tri_lead = int(round(Settings.TRIGGER_SNAP_LEAD_SEC * rate))
        gap_pts = max(1, int(round(Settings.CLEARED_GAP_SEC * rate)))
        snap_max = int(round(Settings.SNAP_MAX_SEC * rate))
        valid = cat[[c for c in cat.columns if c != "X" and not c.startswith("_")]].notna().any(axis=1).to_numpy()
        new = bounds.copy()
        # head: empty rows and anything before the first breath's inspiration onset
        lo = int(np.argmax(valid)) if valid.any() else 0
        first = on[on < (bounds[0] + bounds[1]) // 2]   # inspiration onsets in the first half of the first breath
        first_tri = tri[tri < (bounds[0] + bounds[1]) // 2]
        if len(first_tri):
            new[0] = max(lo, int(first_tri[0]) - tri_lead)
        else:
            new[0] = max(lo, int(first[0]) - lead) if len(first) else lo
        # boundary between breaths: among inspiration onsets inside the midpoints of the adjacent breaths, the one closest to the original boundary
        for j in range(1, len(g)):
            a = (bounds[j - 1] + bounds[j]) // 2
            b = (bounds[j] + bounds[j + 1]) // 2
            cand_tri = tri[(tri > a) & (tri < b)]
            cand = on[(on > a) & (on < b)]
            # 0.2.5: do not snap across an undrawn stretch (screen cleared / drawing stopped, CLEARED_GAP_SEC or longer):
            # the true onset may lie inside it (Ptranspulm 2026-10-09, B853/B854: the boundary jumped back 1.75 s onto B853's second inspiration)
            cand_tri = np.array([o for o in cand_tri if not _gap_between(valid, o, bounds[j], gap_pts)], int)
            cand = np.array([o for o in cand if not _gap_between(valid, o, bounds[j], gap_pts)], int)
            # 0.2.5: do not move a boundary by more than SNAP_MAX_SEC (0.8 s; normally a median of 0.28 s and 99% within 0.39 s).
            # When the inspiration lies in a stretch the ventilator drew as a flat line (no data), the nearest onset can be in the previous breath
            # (Ptranspulm 2026-10-09, B853/B854: 1.1 s)
            cand_tri = cand_tri[np.abs(cand_tri - tri_lead - bounds[j]) <= snap_max]
            cand = cand[np.abs(cand - lead - bounds[j]) <= snap_max]
            if len(cand_tri):   # spontaneous breath: snap to the triangle
                o = int(cand_tri[np.argmin(np.abs(cand_tri - bounds[j]))])
                new[j] = o - tri_lead
            elif len(cand):     # controlled breath: snap to the flow inspiration onset (as before)
                o = int(cand[np.argmin(np.abs(cand - bounds[j]))])
                new[j] = o - lead
        # tail: empty rows and a final inspiration without expiration (part of the next breath's inspiration)
        hi = len(cat) - int(np.argmax(valid[::-1])) if valid.any() else len(cat)
        new[-1] = hi
        last_on = on[(on > new[-2] + lead) & (on < hi)]
        if len(last_on) and not (np.nan_to_num(flow[last_on[-1]:hi]) < -EXP_FLOW).any():
            new[-1] = max(new[-2] + 1, int(last_on[-1]) - lead)
        # keep order (boundaries do not overtake each other)
        for j in range(1, len(new)):
            new[j] = max(new[j], new[j - 1] + 1)
        for j, i in enumerate(g):
            a, b = int(new[j]), int(new[j + 1])
            items[i]["trim_head"] = a - int(bounds[j])
            items[i]["trim_tail"] = int(bounds[j + 1]) - b
            items[i]["wave"] = cat.iloc[a:b].reset_index(drop=True)
            items[i]["t0"] = float(times[a])
            # points received from neighboring breaths are timed at the original breath's rate, so recompute the rate from the times of the end points
            if b - a >= 2 and times[b - 1] > times[a]:
                items[i]["rate"] = float((b - a - 1) / (times[b - 1] - times[a]))
    return items

def _axis_range(df_frames, i, name):
    """(min tick, max tick) of the vertical axis at frame i. None if unreadable."""
    c = axis_scale.decode(df_frames.at[i, f"Axis_{name}"]) if f"Axis_{name}" in df_frames else None
    return None if c is None else (c[2], c[3])


def _clipped(values, img, valid, name, df_frames, run=3):
    """Points stuck at the top/bottom of the drawing area (Settings.WAVES[name]["plot"]) (lasting run points or more, or next to a blank).
    The edge value is computed with the conversion of the frame whose image the point was read from."""
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
        tol[sel] = 2.5 * abs(a)   # line thickness (the center of a line stuck at the edge is 1-2 px inside the edge)
    with np.errstate(invalid="ignore"):
        at = valid & np.isfinite(v) & ((v >= hi - tol) | (v <= lo + tol))
    out = np.zeros(len(v), bool)
    gap = valid & ~np.isfinite(v)   # lines stuck at the edge can be unreadable and become blank
    for a2, b2 in _runs(at):
        next_to_gap = (a2 >= 2 and gap[a2 - 2:a2].all()) or (b2 + 2 < len(v) and gap[b2 + 1:b2 + 3].all())
        if b2 - a2 + 1 >= run or next_to_gap:
            out[a2:b2 + 1] = True
    return out


def _wider_axis_frame(name, after, passes, xs, df_frames, pas, pen, abn, max_frames):
    """First frame after `after` in which name's vertical axis is wider than at `after` and columns xs (passes) are still on screen."""
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
        # columns remain: left of the pen tip in the same pass, or right of the pen tip (and erase band) in the next pass
        same = (passes == pas[g]) & (xs <= pen[g])
        prev = (passes == pas[g] - 1) & (xs > pen[g] + 8)
        if np.all(same | prev):
            return g
        return None   # already overwritten by the time it widened
    return None


def _after_popup_frame(after, passes, xs, pas, pen, abn, wrap, max_frames):
    """First non-Abnormal frame after `after` (an Abnormal frame), and the columns of xs (passes) still remaining in that frame.
    Hamilton keeps drawing waveforms behind popups, so the frame right after a popup disappears still has the columns drawn during the popup
    (unless overwritten). Not if the screen was cleared midway and returned to the left edge (wrap = 2). (None, None) if not found"""
    n = len(abn)
    for g in range(after + 1, min(after + 1 + max_frames, n)):
        if wrap[g] == 2:
            return None, None
        if abn[g]:
            continue
        # columns remain: left of the pen tip in the same pass, or right of the pen tip (and erase band) in the next pass
        same = (passes == pas[g]) & (xs <= pen[g])
        prev = (passes == pas[g] - 1) & (xs > pen[g] + 8)
        return g, same | prev
    return None, None


def extract_waves(df_frames, df_breaths, frames_root, output_dir, user_ID,
                  settings={}, overwrite=False, archive_dir=None):
    """
    Write per-breath waveform CSVs (columns: X, Pressure, Flow, Volume, Pes or Ptranspulm (aux_wave_name)) to output_dir/<BreathNumber>.csv.

    Cut from the column after the pen tip at breath start to the pen-tip column at breath end. Each column is read from the last frame of the pass that drew it (up to the breath end).
    Breaths that returned to the left edge join the parts before and after the return (also when the screen was cleared midway; the part before is read from the frame just before clearing).
    Time of each point = Wave_T0 + row index / Wave_Rate (s, from the start of the video).
    0.2.0: if trim_to_breath (default ON), align waveform boundaries to inspiration onset (_snap_to_breaths).
    Row X stays the cumulative sweep position. Numbers of moved points are Wave_Trim_Head / Wave_Trim_Tail.

    Returns:
        (number of breaths whose CSV was written, df_breaths)
    """
    opts = settings.get("post_ocr", {})
    fps = float(opts.get("fps", 10))
    aux = aux_wave_name(settings)
    os.makedirs(output_dir, exist_ok=True)
    W = Settings.SWEEP_X_RIGHT - Settings.SWEEP_X_LEFT
    max_len = W - 15  # margin to exclude the erase band ahead of the pen tip and the previous pass's waveform

    df_frames = _rename_aux(df_frames, aux, to_output=False).reset_index(drop=True)
    XL, XR = Settings.SWEEP_X_LEFT, Settings.SWEEP_X_RIGHT
    tip_u = df_frames["Sweep_U"].to_numpy(float)
    pen = df_frames["Sweep_X"].to_numpy(float)
    pas = df_frames["Sweep_Pass"].to_numpy(int) if "Sweep_Pass" in df_frames else np.zeros(len(df_frames), int)
    wrap = df_frames["Sweep_Wrap"].to_numpy(int) if "Sweep_Wrap" in df_frames else np.zeros(len(df_frames), int)
    abn = df_frames["Abnormal"].to_numpy(int) if "Abnormal" in df_frames else np.zeros(len(df_frames), int)
    # airway-pressure axis ticks readable in that frame = airway-pressure area visible (often visible even during popups)
    paw_seen = (df_frames["Axis_Filled_Pressure"].to_numpy(int) == 0) if "Axis_Filled_Pressure" in df_frames \
        else np.zeros(len(df_frames), bool)
    frame_no = df_frames["Frame"].to_numpy(int)
    last_of_pass = pd.Series(np.arange(len(df_frames))).groupby(pas).max().to_dict()
    # 0.2.3: passes that were cleared midway and returned are read from the frame just before clearing.
    # Up to 0.2.2 the last frame of the pass (already cleared) was read, so even parts drawn before clearing became blank
    detected = df_frames["Sweep_Detected"].to_numpy(int) if "Sweep_Detected" in df_frames else np.ones(len(df_frames), int)
    capped = {}   # pass whose read frame was moved before clearing -> (that frame, frame where the pen tip was last found)
    for last, j, i in _clear_events(pas, wrap, detected, df_frames["Sweep_Run"] if "Sweep_Run" in df_frames else None, fps):
        if j - 1 >= 0 and pas[j - 1] == pas[i - 1]:
            last_of_pass[pas[i - 1]] = j - 1
            capped[int(pas[i - 1])] = (j - 1, last)
    rate = float(df_frames["Sweep_Rate"].iloc[0]) if len(df_frames) else Settings.SWEEP_PX_PER_SEC
    times = frame_no / fps
    pos_of_frame = {int(f): i for i, f in enumerate(frame_no)}

    def frame_cal(i):
        """Vertical-axis conversion at frame i {wave name: (a, b)}."""
        out = {}
        for name in Settings.WAVES:
            c = axis_scale.decode(df_frames.at[i, f"Axis_{name}"]) if f"Axis_{name}" in df_frames else None
            if c is not None:
                out[name] = c[:2]
        return out

    df_breaths = _rename_aux(df_breaths, aux, to_output=False).copy()
    new_cols = {c: [] for c in ("Wave_T0", "Wave_Rate", "Wave_Points", "Wave_Missing", "Wave_Reread", "Wave_Popup", "Sweep_Start", "Sweep_End",
                                "Wrapped", "Wave_Trim_Head", "Wave_Trim_Tail", "Start_File_Name", "End_File_Name")}
    recs, items = [], []   # per-breath Breaths.csv values / waveforms (written after aligning boundaries)
    alerts = df_breaths["Alert"].fillna("").astype(str).tolist()
    triggers = df_breaths["Trigger"].fillna("").astype(str).tolist() if "Trigger" in df_breaths else None
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

        # waveform range (cumulative position): (pen tip at previous frame, pen tip at last frame]
        p0 = s - 1 if s > 0 else s
        first = int(np.floor(tip_u[p0])) + 1
        last = int(np.floor(tip_u[e]))
        # 0.8.0: breaths containing Abnormal (popups etc.) are read too. Hamilton keeps drawing waveforms behind popups, so
        # they are readable if the popup is gone in the image the columns are read from (last frame of the pass, or of the breath) (B1019 etc.).
        # columns whose read image is Abnormal are skipped below (blank). Up to 0.7.4 waveforms of breaths containing Abnormal were not written
        if last < first:
            recs.append(rec)
            continue
        if last - first + 1 > max_len:
            alerts[k] = ",".join(filter(None, [alerts[k], "Long"]))

        # frame kk that drew each column (cumulative position u) and its screen x. The read image is "the last frame of that pass (up to the breath end)".
        # for breaths spanning a return to the left edge, the part of the pass before the return is read from that pass's last frame, the part after from the breath's last frame
        # (if the whole screen was cleared and it returned, the part before is not in the last frame)
        u = np.arange(first, last + 1)
        kk = np.clip(np.searchsorted(tip_u, u, side="left"), p0 + 1 if s > 0 else s, e)
        x = pen[kk] - (tip_u[kk] - u)
        img = np.minimum(np.array([last_of_pass[p] for p in pas[kk]]), e)
        before = x < XL - 0.5                       # this column was drawn in the pass just before kk (before returning to the left edge)
        kb = np.maximum(kk - 1, 0)
        xb = pen[kb] + (u - tip_u[kb])
        x = np.where(before, xb, x)
        img = np.where(before, np.minimum(np.array([last_of_pass[p] for p in pas[kb]]), e), img)
        # if cleared midway and returned, columns beyond the pen tip before the return were not drawn
        valid = ~before | (wrap[kk] == 1) | (xb <= pen[kb] + 0.5)
        valid &= (x >= XL - 0.5) & (x <= XR - 0.5)
        # among columns read from the frame just before clearing, those right of the last found pen-tip position are blank (not yet drawn; the previous pass's waveform remains on screen.
        # pen tips in frames where it was not found are speed-extrapolated estimates, so not used. B1238). Up to 0.2.2 they were read from the already-cleared screen and were blank
        blank = np.zeros(len(u), bool)
        if capped:
            pc = np.where(before, pas[kb], pas[kk])
            for q, (cf, seen) in capped.items():
                blank |= (pc == q) & (img == cf) & (x > pen[seen] + 0.5) & valid
            valid &= ~blank
        xi = np.clip(np.round(x).astype(int), XL, XR - 1)

        vals = {name: np.full(len(u), np.nan) for name in Settings.WAVES}
        pass_col = np.where(before, pas[kb], pas[kk])
        reread = []
        popup = []
        ok_any = bool(blank.any())

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
            # 0.8.0: if the read image is mid-popup, read the 4 waveforms from the frame right after the popup disappears (B1014-B1019 etc.).
            # columns overwritten in that frame get only Paw, from the airway-pressure area that stays visible during the popup
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

        # waveforms beyond the axis range are drawn clipped at the top/bottom. Hamilton then widens the axis and redraws, so
        # if there is a frame with a widened axis while the columns are still on screen, re-read from it (otherwise Clipped in Alert)
        for name in Settings.WAVES:
            clipped = _clipped(vals[name], img, valid, name, df_frames)
            # interior blanks (2+ points) are also re-read candidates, since the line may have gone out of range and been unreadable
            gaps = np.zeros(len(u), bool)
            fin = np.isfinite(vals[name])
            if fin.any():
                inner = np.arange(len(u))
                inner = (inner > np.argmax(fin)) & (inner < len(u) - 1 - np.argmax(fin[::-1]))
                for a2, b2 in _runs(valid & ~fin & inner):
                    if b2 - a2 + 1 >= 2:
                        gaps[a2:b2 + 1] = True
            target = clipped | gaps
            lost = np.zeros(len(u), bool)
            if capped:
                # 0.2.3: columns of a pass whose screen was cleared are not on screen even after the axis widens, so not re-read (Clipped if clipped).
                # including them in the re-read would block re-reading of columns of other passes too (B244)
                lost = target & np.isin(pass_col, list(capped))
                target &= ~lost
                if (clipped & lost).any():
                    alerts[k] = ",".join(dict.fromkeys(filter(None, alerts[k].split(",") + ["Clipped"])))
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
            # re-read all columns of the passes containing clipped columns (so the axis is consistent within the breath)
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
        wave["_sx"] = xi   # screen column (for Sweep_Start / End after moving boundaries; not written)

        # time the first point was drawn: when the pen tip reached first (interpolated from per-frame pen-tip positions)
        items.append({"wave": wave, "t0": float(np.interp(first, tip_u, times)), "rate": rate, "rec": rec, "k": len(recs),
                      "path": os.path.join(output_dir, f"{int(b['BreathNumber'])}.csv")})
        recs.append(rec)

    # align boundaries to inspiration onset (waveforms of adjacent breaths are joined, so move points between them)
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
            # contains a section where drawing stopped / the screen was cleared due to screen operations, and 30% or more of the waveform is missing
            alerts[it["k"]] = ",".join(dict.fromkeys(filter(None, alerts[it["k"]].split(",") + ["Partial"])))
        if triggers is not None and triggers[it["k"]] == "C" and "Cleared" in alerts[it["k"]].split(","):
            # 0.2.3: in a breath where the screen was cleared, if there is an undrawn section (drawing stopped / cleared),
            # inspiration may have started there without a triangle being drawn, so blank (unknown) instead of C
            gaps = _runs(wave["Pressure"].isna().to_numpy())
            if gaps and max(b - a + 1 for a, b in gaps) >= Settings.CLEARED_GAP_SEC * it["rate"]:
                triggers[it["k"]] = ""
        if overwrite or not os.path.exists(it["path"]):
            wave.drop(columns=["_sx"]).rename(columns={"Pes": aux}).to_csv(it["path"], index=False)
        written += 1

    for c in new_cols:
        df_breaths[c] = [r[c] for r in recs]
    df_breaths["Alert"] = alerts
    if triggers is not None:
        df_breaths["Trigger"] = triggers

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
