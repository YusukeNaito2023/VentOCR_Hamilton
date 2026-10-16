"""波形の縦軸の目盛りの数字を読み、画面の y → 物理量 の換算式を求める。

Hamilton は波形ごとに縦軸のスケールを頻繁に自動で変える（例: Paw 0〜20 / 0〜40、Flow ±45 / ±75 / ±150、
Volume 400 / 800 / 1600、Pes 0〜20 / -10〜20 / -40〜40。0 の位置も動く）。そのため呼吸ごとに目盛りを読む。

目盛りの数字は 0〜9 と「-」だけで、毎回同じフォント・大きさ（高さ12〜14px）で描かれる。汎用の OCR（EasyOCR）は
この大きさの数字を読み違えるので、1文字ずつ見本（axis_glyphs.npz）と照合する。
見本は 2026-10-12、Ptranspulm 2026-10-09 の動画に出てきた14通りの目盛りから文字を切り出し、
形の似たものをまとめて、それぞれ何の文字かを目視で決めて作った（「9」は出てこなかったので見本が無い）。
見本に無い形の文字がある目盛りは読まない（その波形は読めなかった扱い）。
"""
import os

import cv2
import numpy as np

from . import Settings

_GLYPH_SIZE = (10, 14)  # (幅, 高さ)。照合はこの大きさにそろえて行う


def _load_glyphs():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), Settings.AXIS_GLYPH_FILE)
    z = np.load(path)
    return z["templates"], z["widths"], [str(c) for c in z["chars"]]


_TEMPLATES, _WIDTHS, _CHARS = _load_glyphs()


def _runs(mask):
    idx = np.flatnonzero(np.diff(np.r_[0, mask.astype(np.int8), 0]))
    return list(zip(idx[0::2], idx[1::2] - 1))


def _norm(img):
    v = cv2.resize(img.astype(np.float32), _GLYPH_SIZE, interpolation=cv2.INTER_AREA)
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n else v


def _read_char(img):
    """1文字の画像 → 文字（見本との相関が AXIS_MATCH_MIN 未満なら None）。"""
    v = _norm(img)
    score = (_TEMPLATES * v).sum(axis=(1, 2))
    score[np.abs(_WIDTHS - img.shape[1]) > 2] = -1
    i = int(np.argmax(score))
    return _CHARS[i] if score[i] >= Settings.AXIS_MATCH_MIN else None


def read_labels(gray, y0, y1):
    """画面の y0〜y1 の範囲にある目盛りの数字 → [(数字の中心の y, 値), ...]。読めない行は含めない。"""
    x0, x1 = Settings.AXIS_LABEL_X
    g = gray[y0:y1, x0:x1]
    m = g > Settings.AXIS_BIN_THRESH
    h0, h1 = Settings.AXIS_TEXT_HEIGHT
    out = []
    for a, b in _runs(m.any(axis=1)):
        if not (h0 <= b - a + 1 <= h1):
            continue  # 目盛りの点・スクロールバー・ポップアップなど
        line = m[a:b + 1]
        text = ""
        for c0, c1 in _runs(line.any(axis=0)):
            ch = _read_char(g[a:b + 1, c0:c1 + 1])
            if ch is None:
                text = None
                break
            text += ch
        if not text or not (text.lstrip("-").isdigit()) or text.count("-") > 1 or ("-" in text and not text.startswith("-")):
            continue
        out.append((y0 + (a + b) / 2.0, float(text)))
    return out


def read_axis(gray):
    """全波形の換算式 → {波形名: (a, b, 最小の目盛り, 最大の目盛り) または None}。値 = a * y + b。

    目盛りが2つ以上読め、直線から AXIS_FIT_TOL_PX 以上外れる目盛りが無く、上ほど値が大きいときだけ採用する。
    """
    out = {}
    for name, (y0, y1) in Settings.AXIS_PANELS.items():
        ticks = read_labels(gray, y0, y1)
        res = None
        if len(ticks) >= 2:
            ys = np.array([t[0] for t in ticks])
            vs = np.array([t[1] for t in ticks])
            if np.ptp(vs) > 0:
                a, b = np.polyfit(ys, vs, 1)
                if a < 0 and np.max(np.abs((vs - b) / a - ys)) <= Settings.AXIS_FIT_TOL_PX:
                    res = (float(a), float(b), float(vs.min()), float(vs.max()))
        out[name] = res
    return out


def encode(axis):
    """read_axis の結果 → Frames.csv に入れる文字列（波形ごと "a;b;min;max"、読めなければ空）。"""
    return {f"Axis_{name}": ("" if r is None else f"{r[0]:.6f};{r[1]:.4f};{r[2]:g};{r[3]:g}") for name, r in axis.items()}


def decode(text):
    """encode の文字列 → (a, b, min, max) または None。"""
    if not isinstance(text, str) or text.count(";") != 3:
        return None
    try:
        a, b, lo, hi = (float(t) for t in text.split(";"))
    except ValueError:
        return None
    return a, b, lo, hi
