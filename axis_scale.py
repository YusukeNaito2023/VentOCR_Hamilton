"""Read the tick-label numbers on the waveforms' vertical axes and derive the screen y -> physical value conversion.

Hamilton frequently auto-rescales the vertical axis of each waveform (e.g. Paw 0-20 / 0-40, Flow ±45 / ±75 / ±150,
Volume 400 / 800 / 1600, Pes 0-20 / -10 to 20 / -40 to 40; the position of 0 also moves), so the ticks are read for every breath.

Tick labels consist only of 0-9 and "-", always drawn in the same font and size (12-14 px high). General-purpose OCR (EasyOCR)
misreads digits this small, so each character is matched against templates (axis_glyphs.npz).
Templates were made on 2026-10-12 by cutting characters out of the 14 tick-label sets seen in the Ptranspulm 2026-10-09 video,
grouping similar shapes, and assigning each group its character by eye (no template for "9", which never appeared).
Axes containing a character shape not in the templates are not read (that waveform is treated as unreadable).
"""
import os

import cv2
import numpy as np

from . import Settings

_GLYPH_SIZE = (10, 14)  # (width, height). Matching is done after resizing to this size


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
    """Image of one character -> character (None if the correlation with the templates is below AXIS_MATCH_MIN)."""
    v = _norm(img)
    score = (_TEMPLATES * v).sum(axis=(1, 2))
    score[np.abs(_WIDTHS - img.shape[1]) > 2] = -1
    i = int(np.argmax(score))
    return _CHARS[i] if score[i] >= Settings.AXIS_MATCH_MIN else None


def read_labels(gray, y0, y1):
    """Tick-label numbers in screen rows y0-y1 -> [(y of the number's center, value), ...]. Unreadable rows are omitted."""
    x0, x1 = Settings.AXIS_LABEL_X
    g = gray[y0:y1, x0:x1]
    m = g > Settings.AXIS_BIN_THRESH
    h0, h1 = Settings.AXIS_TEXT_HEIGHT
    out = []
    for a, b in _runs(m.any(axis=1)):
        if not (h0 <= b - a + 1 <= h1):
            continue  # tick dots, scroll bar, popups, etc.
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
    """Conversion for all waveforms -> {wave name: (a, b, min tick, max tick) or None}. value = a * y + b.

    Adopted only when at least 2 ticks are read, no tick deviates from the line by AXIS_FIT_TOL_PX or more, and values increase upward.
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
    """read_axis result -> string for Frames.csv (per waveform "a;b;min;max", empty if unreadable)."""
    return {f"Axis_{name}": ("" if r is None else f"{r[0]:.6f};{r[1]:.4f};{r[2]:g};{r[3]:g}") for name, r in axis.items()}


def decode(text):
    """encode string -> (a, b, min, max) or None."""
    if not isinstance(text, str) or text.count(";") != 3:
        return None
    try:
        a, b, lo, hi = (float(t) for t in text.split(";"))
    except ValueError:
        return None
    return a, b, lo, hi
