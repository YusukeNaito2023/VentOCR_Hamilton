# Hamilton Engine Settings
# All coordinates are screen coordinates at the reference resolution (1920x1080). Input frames are normalized to this resolution first.
# Reference image: layout.jpg (resource/Hamilton_Sample.mov, "PCV+" screen recorded 2026-10-07)

BASE_RESOLUTION = (1920, 1080)  # (width, height)

# ------------------------------------------------------------
# Values to OCR (column name -> read position (x, y, w, h) and how to read it)
# ------------------------------------------------------------
# kind:   "measured" = measured value on the left. It belongs to the previous breath, so a breath's value = the value shown during the next breath (*_displayed is also kept)
#         "setting"  = ventilation mode / setting value in the circles on the right. The value shown during that breath
# type:   "int" = integer, "dec1" = one decimal place (if the decimal point is missed and there are dot_min_digits or more digits, insert it before the last digit),
#         "text" = text (ventilation mode)
# dot_check: when dec1 reads dot_min_digits or more digits without a decimal point, check the ROI pixels for a decimal point before inserting one.
#         Without a decimal point it is treated as an integer (0.2.1. Compliance >= 100 is displayed as an integer without a point, like "126",
#         and < 100 always with one decimal, like "89.0". Up to 0.2.0, 126 became 12.6)
# small_decimal: for int values, if the characters read are "1 digit . 1 digit" (e.g. "9.6"), keep it as a decimal (0.2.5. Hamilton shows VTE / peak pressure
#         with one decimal below 10 and as integers at 10 or more. Confirmed on 36,881 Study Recorder records. Up to 0.2.4, 9.6 became 9)
# glyph_read: besides EasyOCR, match each digit against templates (value_glyphs.npz); if all digits match the templates, use that value.
#         Otherwise use the EasyOCR value (0.2.2. EasyOCR repeatedly drops the "2" of "41.2", reads "47.4" as "47..4", etc. for specific digits)
# For setting values in the circles on the right, the box covers only the digits so the unit (the "2" of cmH2O) is not read.
# VTE extends its left edge to x=110 (0.7.2. At 1000 mL or more it has 4 digits and the leading "1" appears at x≈140;
# alarm-limit numbers (700 / 250 etc.) reach x≈80). Widening other values slightly worsened Compliance reading, so they are not widened
# Which values to OCR is selected by engine_options initial_ocr.ocr_keys (the breath_trigger values used for breath segmentation are always read).
DIGITS = "0123456789."
TEXT_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-/() "
OCR_FIELDS = {
    "VTE":        {"roi": (110, 435, 275, 100), "kind": "measured", "type": "int", "small_decimal": True, "unit": "mL",
                   "label": "Expiratory VT"},
    "Compliance": {"roi": (160, 605, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 3,
                   "dot_check": True, "glyph_read": True, "unit": "mL/cmH2O", "label": "Compliance"},
    "Ppeak":      {"roi": (185, 95, 200, 100), "kind": "measured", "type": "int", "small_decimal": True, "unit": "cmH2O",
                   "label": "Peak pressure"},
    "MinVol":     {"roi": (160, 265, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 2,
                   "unit": "L/min", "label": "Minute volume"},
    "P01":        {"roi": (160, 775, 230, 100), "kind": "measured", "type": "dec1", "dot_min_digits": 2,
                   "allow": DIGITS + "-", "unit": "cmH2O", "label": "P0.1"},
    "Mode":       {"roi": (160, 18, 240, 55), "kind": "setting", "type": "text", "allow": TEXT_CHARS,
                   "label": "Ventilation mode (setting)"},
    "Set_Rate":   {"roi": (1768, 186, 60, 40), "kind": "setting", "type": "int", "unit": "b/min",
                   "label": "Rate (setting)"},
    "Set_Pinsp":  {"roi": (1768, 350, 60, 35), "kind": "setting", "type": "int", "unit": "cmH2O",
                   "label": "ΔPinsp (setting)"},
    "Set_PEEP":   {"roi": (1768, 512, 60, 34), "kind": "setting", "type": "int", "unit": "cmH2O",
                   "label": "PEEP/CPAP (setting)"},
    "Set_Oxygen": {"roi": (1768, 674, 60, 32), "kind": "setting", "type": "int", "unit": "%",
                   "label": "Oxygen (setting)"},
}
DEFAULT_OCR_KEYS = ["VTE", "Compliance", "Ppeak"]
ROIs = {key: spec["roi"] for key, spec in OCR_FIELDS.items()}  # old name (kept for compatibility)

# ------------------------------------------------------------
# Vertical-axis ticks of the waveforms (axis_scale.py)
# ------------------------------------------------------------
# Hamilton frequently auto-rescales each waveform's vertical axis, so the tick numbers are read and converted for every breath.
# Frames whose ticks cannot be read (popup, another screen) are Abnormal.
# Up to 0.4 the tick column was compared with the reference image and marked Abnormal "if different", so a scale change became Abnormal,
# display updates during it did not segment breaths, and no waveforms were output (Ptranspulm 2026-10-09, B251).
AXIS_LABEL_X = (414, 462)     # column of tick numbers (measured x=421-458. Right-aligned, so "1600" and "-150" fit. Excludes the scroll bar at x≈399-409 on the left and the axis line on the right)
AXIS_PANELS = {               # vertical range of the tick numbers for each waveform
    "Pressure": (95, 300),
    "Flow": (318, 548),
    "Volume": (550, 775),
    "Pes": (778, 1000),
}
AXIS_GLYPH_FILE = "axis_glyphs.npz"   # character templates
AXIS_BIN_THRESH = 150         # digits are white on black. Pixels brighter than this are characters (excludes the gray scroll bar)
AXIS_TEXT_HEIGHT = (10, 16)   # character height of one row (measured 12-14 px)
AXIS_MATCH_MIN = 0.85         # characters whose correlation with the templates is below this are unreadable
AXIS_FIT_TOL_PX = 2.0         # if a tick position deviates from the line by this (px) or more, that waveform is unreadable
AXIS_MIN_PANELS = 3           # frames with fewer waveforms with readable ticks than this are Abnormal (popup, another screen).
                              # when only one is unreadable (e.g. "9", not in the templates), that waveform inherits the last readable ticks

# Whether a settings dialog (e.g. ventilation mode settings) covers the waveform area (0.2.4. Wave_Covered in Frames.csv, Popup in Alert).
# Dialogs that do not hide the vertical-axis ticks do not make frames Abnormal (Ptranspulm 2026-10-09, B989-991: mode setting PCV+ -> SPONT)
POPUP_AREA = (467, 95, 1567, 1000)   # waveform area (x0, y0, x1, y1)
POPUP_GRID = 8                # measure on grid pixels at this spacing (px)
POPUP_BRIGHT = 180            # pixels brighter than this are the dialog (gray)
POPUP_COVER_MIN = 0.1         # frames with at least this fraction of bright pixels have the waveform area covered by a dialog (measured: normally <= 0.02, dialog 0.3-0.8)

# Read the large measured-value digits on the left by template matching (glyph_read in OCR_FIELDS)
VALUE_GLYPH_FILE = "value_glyphs.npz"  # digit templates (about 80 px high. Made from the Compliance display in the Ptranspulm 2026-10-09 video)
VALUE_BIN_THRESH = 40         # digits are white (≈255) or red (≈76 in grayscale) on black
VALUE_MATCH_MIN = 0.9         # if any digit correlates with the templates below this, do not use the template reading (measured: correct digits >= 0.985, other digits <= 0.65)
VALUE_WIDTH_TOL = 4           # if a digit's width (px) differs from the template by more than this, it is not that digit

# ------------------------------------------------------------
# Waveform drawing area and sweep
# ------------------------------------------------------------
# The sweep draws rightward from X_LEFT; on reaching X_RIGHT it returns to X_LEFT and overwrites the old waveform (wrap around).
# Drawing may also stop due to screen operations, the whole screen is cleared, and it returns to the left edge midway (core.track_sweep).
# Time axis is 50 px/s, one pass 1100 px = 22 s (tick 0 s = x467, 250 px per 5 s).
SWEEP_X_LEFT = 467
SWEEP_X_RIGHT = 1567          # this column is not drawn ([X_LEFT, X_RIGHT) is one pass)
SWEEP_PX_PER_SEC = 50.0       # default sweep speed (the actual value is estimated from the video)
SWEEP_ERASE_OFFSET = 3        # right edge of the difference from the previous frame − pen tip (width of the band erased ahead of the pen tip)
SWEEP_DIFF_ROWS = (95, 960)   # vertical range used to detect the sweep position (all 4 waveforms)
SWEEP_DIFF_THRESH = 40        # pixels whose grayscale difference exceeds this are "changed"
SWEEP_DIFF_MIN_PIXELS = 3     # a column is changed if at least this many pixels in it changed
SWEEP_RUN_MAX = 40            # frames whose run of changed columns is wider than this are full-screen redraws (e.g. axis auto-rescale) and are not used for the sweep position
                              # (if narrower than 3x the per-frame advance, use 3x that. For low fps)
UPDATE_MERGE_SEC = 0.1        # VTE and Compliance display changes within this time are one update (a display rewrite spans several frames; 1 frame at 10 fps)
SWEEP_WRAP_JUMP = 100         # if the pen tip moves left by this much or more, it is the next pass (drew to the right edge and returned, or was cleared midway and returned.
                              # 0.2.5: also the next pass when it moves back by SWEEP_RESTART_MARGIN or more to near the left edge)
SWEEP_RESTART_MARGIN = 15     # margin (px) for regarding drawing as started from the left edge / finished at the right edge
SWEEP_REREAD_SEC = 22.0       # time searched (one pass) when re-reading a waveform clipped beyond the vertical range from a frame where the axis widened
CLEARED_GAP_SEC = 0.3         # in a breath where the screen was cleared (Cleared in Alert), if an undrawn waveform section is this long (s) or more and there is no triangle,
                              # Trigger is left blank instead of C (0.2.3. Inspiration may have started during that section)

# ------------------------------------------------------------
# Patient trigger mark (0.8.0)
# ------------------------------------------------------------
# Breaths started by patient inspiratory effort (spontaneous trigger) get a pink triangle (about 16 px wide, 10 px high) just below the airway-pressure time axis
# at the start of inspiration, swept along with the waveform. The time axis is at airway-pressure 0, so it moves up and down with the axis scale
# (y≈282-291 at 0-40, y≈266-275 at 0-20. Measured on Ptranspulm 2026-10-09).
# Color BGR ≈ (86, 6, 248), hue ≈ 170-175 (same color as the flow trace, but above the flow band y>=325).
# The triangle appears only at the start of breaths Hamilton judged spontaneously triggered (Study Recorder IsPatientTriggeredBreath = 1).
TRIGGER_ROWS = (100, 315)     # vertical range to search for triangles (airway-pressure area, above the flow area)
TRIGGER_HUE = 172
TRIGGER_HUE_TOL = 8
TRIGGER_SAT_MIN = 120
TRIGGER_VAL_MIN = 120
TRIGGER_CLOSE = (5, 5)        # closing size to join fragments (height, width)
TRIGGER_HEIGHT = (7, 16)      # triangle height (px). Excludes the red upper-alarm-limit line (1-2 px high)
TRIGGER_WIDTH = (3, 24)       # triangle width (px). Includes ones cut at the left edge or partly erased by the erase band
TRIGGER_APEX_ROWS = 2         # triangle apex = mean x of the pixels in this many top rows
# Newly drawn triangle = absent in the previous frame (±TRIGGER_SAME_PX) and within TRIGGER_NEW_LAG_PX of the pen tip (pen tip − apex).
# When drawn, a triangle is about 4 px behind the pen tip. Triangles from one pass earlier (about 22 s ago) remain on screen right of the pen tip,
# so only new ones are selected by position (Ptranspulm 2026-10-09: of 1090 new triangles, 1089 were immediately followed by a Hamilton breath record)
TRIGGER_SAME_PX = 2
TRIGGER_NEW_LAG_PX = (-5, 40)
TRIGGER_DEDUP_SEC = 0.3       # two triangles closer than this are the same one
TRIGGER_KEEP_PX = 8           # a triangle redrawn at the same position as one from the previous pass must still remain after the pen tip has advanced this far
TRIGGER_KEEP_SEC = 0.5        # time (s) checked for that (5 frames up to 0.8.1)

# Additional segmentation by triangles (0.8.0): display updates occur 0.14-0.39 s after the triangle (median 0.27 s).
# If consecutive breaths happen to have the same VTE / Compliance, the display does not change and no boundary is found (Ptranspulm 2026-10-09, B87), so
# if there is no display update from TRIGGER_UPDATE_BEFORE_SEC before to TRIGGER_UPDATE_AFTER_SEC after the triangle,
# segment as if an update occurred TRIGGER_UPDATE_LAG_SEC after the triangle.
# Excluded near Abnormal (popups etc.; from TRIGGER_ABNORMAL_BEFORE_SEC before to TRIGGER_UPDATE_AFTER_SEC after) and at the end of the recording
TRIGGER_UPDATE_LAG_SEC = 0.3
TRIGGER_UPDATE_BEFORE_SEC = 0.5
TRIGGER_UPDATE_AFTER_SEC = 1.0
TRIGGER_ABNORMAL_BEFORE_SEC = 3.0

# Spontaneous breath boundary (0.8.1): when snapping to inspiration onset, place the boundary this long before the triangle apex (equivalent to TRIM_LEAD_SEC for flow).
# In breaths with a clean flow rise, the boundary was 2-5 px (0.04-0.1 s) before the triangle
TRIGGER_SNAP_LEAD_SEC = 0.08
SNAP_MAX_SEC = 0.8            # do not move a breath boundary further than this when snapping to the inspiration onset (0.2.5)

# ------------------------------------------------------------
# Waveforms (CSV column name -> on-screen band, color, ticks)
# ------------------------------------------------------------
# hue is OpenCV HSV (0-179). Conversion uses the ticks read for each breath (axis_scale.py).
# ticks / axis are the default scale of Hamilton_Sample.mov (for conversion when ticks are unreadable, and scale_meta defaults).
# area is the top/bottom y of the drawing area (extent of the vertical axis line at x≈465) (2026-10-18, constant over 1 h of Ptranspulm 2026-10-09).
# P_min / P_max etc. in Breaths.csv are this top/bottom converted to values with the axis conversion (0.8.1).
# plot is the top/bottom y of the drawing area (2026-10-12, measured on Ptranspulm 2026-10-09). The top is at the max tick value;
# the bottom extends below the min tick value. Waveforms beyond the axis range are clipped here (core._clipped).
# Paw uses the column name "Pressure" for compatibility with existing engines and apps.
WAVES = {
    "Pressure": {  # airway pressure (yellow)
        "band": (100, 285), "hue": 29,
        "ticks": [(122, 40), (199, 20), (278, 0)],
        "unit": "cmH2O", "scale_prefix": "P", "axis": (0, 40),
        "plot": (123, None),   # top/bottom y of the drawing area (clipped here beyond the axis range; None = not checked)
        "area": (121, 317),    # top/bottom y of the drawing area (vertical axis line). P_min / P_max in Breaths.csv (0.8.1)
    },
    "Flow": {      # flow (red)
        "band": (325, 545), "hue": 171,
        "ticks": [(346, 75), (376, 50), (405, 25), (434, 0), (464, -25), (493, -50), (522, -75)],
        "unit": "L/min", "scale_prefix": "F", "axis": (-75, 75),
        "plot": (346, 542),
        "area": (346, 542),
    },
    "Volume": {    # volume (green)
        "band": (560, 770), "hue": 58,
        "ticks": [(575.5, 800), (620, 600), (663, 400), (707, 200), (751, 0)],
        "unit": "mL", "scale_prefix": "V", "axis": (-100, 800),  # goes below 0 at end of expiration
        "plot": (576, None),   # the part below 0 is often clipped at the bottom (y769) too, but it is a small negative value at end of expiration, so not checked
        "area": (576, 772),
    },
    "Pes": {       # esophageal pressure (orange)
        "band": (785, 990), "hue": 7,   # drawn down to y≈977 when the ticks go to -40
        "ticks": [(800, 20), (871, 10), (941, 0)],
        "unit": "cmH2O", "scale_prefix": "Pes", "axis": (0, 20),
        "plot": (800, 989),
        "area": (801, 997),
    },
}
WAVE_HUE_TOL = 12     # hue tolerance (circular distance)
WAVE_SAT_MIN = 60     # saturation below this is background / gray axis line
WAVE_VAL_MIN = 60     # value (brightness) below this is background
WAVE_GAP_FILL = 6     # gaps of this width (px) or less are linearly interpolated
