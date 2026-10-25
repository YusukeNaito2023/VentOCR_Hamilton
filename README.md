# Hamilton VentOCR Engine

## Supported Devices
- Hamilton ventilators (screen with numeric values on the left and four waveforms in the center: airway pressure / flow / volume / Pes or Ptranspulm)
- Videos recorded at 1920x1080 (other resolutions are scaled to 1920x1080 before reading)

## Description
- **One breath** = from one update of the numeric values on the left (compliance, expiratory VT) to the next update.
  - The display update lags the start of inspiration by about 0.16 s, so the boundary is shifted from the update by `boundary_shift_sec` (default -0.5 s, i.e. 0.5 s earlier), so that each breath ends at the end of expiration
  - Waveform boundaries are aligned to the start of inspiration (`trim_to_breath`): for spontaneous breaths, 0.08 s before the patient-trigger triangle (0.8.1);
    for mandatory breaths, 0.04 s before the foot of the flow upstroke. The triangle marks exactly where Hamilton started the breath and coincides with the point where Volume returns to 0
  - So that a boundary is detected even when compliance has the same value as in the previous breath, VTE updates are also used as boundaries by default (`breath_trigger`)
  - Update = a new value of VTE or compliance persists for at least `stable_frames` (default 2) frames (0.7.0). The boundary is the first frame showing that value.
    A blank ("---" or unreadable) also counts as a value. A value lasting only one frame is ignored. If the two values change one frame apart, this counts as a single update.
    There is no minimum breath duration (the min_breath_sec = 1 s used up to 0.6 has been removed; short breaths under 1 s are kept as one breath each)
  - If VTE and compliance happen to be identical in consecutive breaths, the display does not change. Therefore, if there is no display update from 0.5 s before to 1 s after
    a patient-trigger triangle (see below), a boundary is placed as if an update had occurred 0.3 s after the triangle (0.8.0; marked in `Trigger_Split` of Frames.csv; no alert since 0.2.5).
    This is not applied near Abnormal frames (3 s before to 1 s after) or at the end of the recording. Consecutive mandatory breaths (no triangle) with identical values cannot be separated
- **OCR** (selected under "Values to OCR" in the analysis dialog; default: VTE, Compliance, Ppeak):
  - Measured values: expiratory VT (`VTE`), compliance (`Compliance`), peak pressure (`Ppeak`), minute volume (`MinVol`), P0.1 (`P01`).
    The numbers on screen are the measurements of the "previous breath", so the value in Breaths.csv is the value displayed during the next breath, and `*_displayed` is the value displayed during that breath itself
  - Settings: ventilation mode (`Mode`, text), respiratory rate (`Set_Rate`), Δ inspiratory pressure (`Set_Pinsp`), PEEP/CPAP (`Set_PEEP`), oxygen concentration (`Set_Oxygen`).
    These are the values displayed during that breath
  - Columns for values that are not selected are not output
- **Waveforms**: Pressure (airway pressure, Paw), Flow, Volume, and the pressure in the 4th panel. The columns of BreathCSVs/<n>.csv are X, Pressure, Flow, Volume, Pes (or Ptranspulm)
  - The 4th panel can show either Pes (esophageal pressure) or Ptranspulm (transpulmonary pressure = Paw − Pes), depending on the Hamilton settings. Which one is chosen with the analysis setting
    `post_ocr.ptranspulm` (default off = Pes) (0.2.0). When on, the column is named Ptranspulm, and the names in Breaths.csv and Frames.csv
    (Axis_Ptranspulm, Ptranspulm_min / Ptranspulm_max), fields.txt and scale_meta.json change accordingly (the values are the same).
    The panel name is not read, so choose the correct setting for each video. Videos in which the display was switched during the recording are not supported
  - The sweep draws from left to right at 50 px/s; on reaching x=1567 it returns to x=467 and overwrites (one sweep 22 s, wrap around)
  - In each frame, the right edge of the columns that changed since the previous frame (= pen (sweep head) + 3 px erase band) is recorded, and the pen is tracked in every frame (0.6.0; core.track_sweep)
  - Drawing can stop (e.g. due to screen operations), after which the whole screen is cleared and drawing restarts from the left edge mid-breath. The part before that is read from the last frame
    in which the pen was found before the clearing (0.2.3; the return to the left edge is only recognized a few frames later, and up to 0.2.2 the cleared screen was read).
    The period during which drawing was stopped is blank (breaths with 30% or more blank get Partial in Alert). Breaths during which the screen was cleared get Cleared in Alert (0.2.3).
    While Hamilton is not outputting measurements (about 1.5 s in B541 of Ptranspulm 2026-10-09; the Study Recorder also shows Paw 0 and no Flow),
    no waveform is drawn on screen either, so that part cannot be recovered
  - Waveforms exceeding the vertical axis range are cut off at the edge of the plot area. If Hamilton then widens the axis, the columns are re-read while they are still on screen
    (the re-read waveform is indicated in Wave_Reread of Breaths.csv; if it cannot be re-read, Alert gets Clipped. The lower edge of Volume (the part below 0) is not checked)
  - A breath's waveform is cut out of the last frame of that breath, from "the column after the pen position at the start" to "the pen column at the end" (breaths spanning a wrap-around are joined)
  - Time of each point = `Wave_T0` + row number / `Wave_Rate` (Breaths.csv)
- **Trigger** (0.8.0): `Trigger` in Breaths.csv = `A` (patient-triggered, spontaneous) / `C` (mandatory, machine-triggered). It is also shown in the breath property summary
  - At the start of a breath that Hamilton classifies as patient-triggered, a pink triangle (▲) is drawn just below the time axis of the airway pressure panel.
    If a new triangle is drawn during the breath, it is A; otherwise C. During a popup, the triangle is still used as long as the airway pressure panel is visible (its axis labels can be read).
    A breath that contains frames in which the airway pressure panel is not visible and has no triangle is blank (the triangle may have been hidden).
    A breath during which the screen was cleared (Cleared in Alert), with an undrawn interval of 0.3 s or more and no triangle, is also blank
    (inspiration may have started during that interval without a triangle being drawn; 0.2.3; Settings.CLEARED_GAP_SEC).
    A breath containing frames in which a settings dialog covered the waveform panels (Hidden in Alert) and having no triangle is also blank (the triangle may have been hidden; 0.2.4)
  - initial_ocr records the triangle apexes in each frame (`Trigger_Apexes` in Frames.csv), and post_ocr selects those that newly appear just behind the pen (0 to 40 px)
    (`Trigger_New`). Triangles from the previous sweep also remain on screen to the right of the pen, so they are distinguished by position.
    A triangle redrawn at almost the same position as one from the previous sweep is distinguished by the fact that it remains after the pen has passed
  - The time axis is at the 0 position of airway pressure and moves up and down with the axis scale, so triangles are searched for by color and size within the airway pressure panel (y=100 to 315).
    A triangle overlapping the time-axis tick numbers (5, 10, 15, 20) bleeds in color and breaks into small fragments, so the fragments are joined before classification

## Vertical Axis Scale (0.5.0)
- Hamilton frequently and automatically changes the vertical axis scale of each waveform (15 different scales in one hour of Ptranspulm 2026-10-09:
  Paw 0 to 20 / 0 to 40, Flow ±45 / ±75 / ±150, Volume 400 / 800 / 1600, Pes 0 to 20 / -10 to 20 / -20 to 20 / -20 to 40 / -40 to 40, etc. The 0 position also moves)
- In each frame, the axis labels (x=414 to 462) are read, and for each waveform a straight line "label y → value" is fitted (axis_scale.py).
  The labels consist only of 0 to 9 and "-" and always have the same shapes, so each character is matched against templates instead of using general-purpose OCR
- A breath's waveform is converted to physical units with the conversion of the last frame it was cut from. In Breaths.csv, Axis_<waveform> = "a;b;lowest axis label;highest axis label" (value = a*y + b);
  P_min / P_max etc. are the values at the bottom and top edges of the plot area (the vertical axis line on screen) (0.8.1; up to 0.8.0 these were the smallest and largest label numbers,
  and waveforms below the lowest label went outside the chart)

## Expected Layout
See `layout.jpg` (red boxes: OCR (Settings.OCR_FIELDS); white boxes: waveforms; blue boxes: axis labels used to check the screen).

## Frame Rate and Frame Images
- The analysis frame rate need not be 10 fps (engine_api 2; verified at 5, 10 and 30 fps). Conversion between seconds and frame counts uses `post_ocr.fps`.
  The `stable_frames` setting is a frame count at 10 fps; at other frame rates it is converted to the same duration
- Only the frame images needed for waveform extraction are read, with `cv2.imread` (engine_api 3; about 40 images for a 2-minute video)

## Alerts (the `Alert` column of Breaths.csv) and blank Trigger (0.2.5)
A breath can have several alerts (comma-separated). "Accuracy evaluation" tells whether the breath is excluded when evaluating reading accuracy
(`exclude_from_accuracy` in `outputs.alerts` of manifest.json; ENGINE_API.md §8.1).

| Alert | Added when (thresholds) | Accuracy evaluation |
|---|---|---|
| Fragment | The first breath of the recording (the recording starts mid-breath) and the last breath (cut at the end of the recording) | Excluded |
| Hidden | The waveforms were hidden during the breath, by either check: (a) a frame where fewer than 3 waveforms (`AXIS_MIN_PANELS`) have readable axis labels (popup or another screen; `Abnormal` column), or (b) a frame where 10% (`POPUP_COVER_MIN`) or more of the pixels sampled every 8 px in the waveform area (x 467-1567, y 95-1000) are bright (gray above 180), such as a settings dialog (`Wave_Covered` in Frames.csv) | Excluded |
| Clipped | The waveform sticks to the top or bottom of the plot area (3 or more points in a row, or next to a blank) and could not be re-read from a frame with a wider axis within 22 s (`SWEEP_REREAD_SEC`); the bottom of Volume is not checked | Excluded |
| Partial | More than 30% of the waveform (Paw) is blank (drawing stopped, screen cleared or hidden, unreadable) | Excluded |
| Cleared | The ventilator cleared the waveform screen during the breath and restarted from the left edge (the pen moved back left by 100 px (`SWEEP_WRAP_JUMP`) or more before reaching the right edge, or by 15 px (`SWEEP_RESTART_MARGIN`) or more to near the left edge) | Not excluded |
| Axis | An axis could not be read in the breath's last frame and the previous labels (or the defaults) were used (not added when the frame is hidden) | Not excluded |
| Long | The waveform is longer than one sweep (1100 px − 15 px margin = 1085 points, about 21.7 s); only the last sweep is kept | Not excluded |

**Blank Trigger (unknown)**: a breath with no new triangle is blank instead of C when any of these holds:
- it contains frames where the airway pressure panel is not visible (axis labels unreadable, e.g. a popup)
- the waveform area was covered by a settings dialog (Hidden, check b)
- the screen was cleared (Cleared) and the waveform has an undrawn stretch of 0.3 s (`CLEARED_GAP_SEC`) or more

## Limitations
- P0.1 was "---" (not measured) in the sample videos, so reading its numeric value has not been verified (negative values are also allowed)
- Waveform axis labels are read by matching against character templates (axis_glyphs.npz). The templates contain no "9" (it did not appear in the videos used to build them), so
  labels containing "9" (such as 90) cannot be read, and that waveform is converted with the last labels that could be read (Axis in Alert)
- Breaths containing frames in which a settings dialog (e.g. ventilation mode settings) covered the waveform panels get Hidden in Alert (0.2.4; Popup up to 0.2.4).
  Within the waveform panels (x 467 to 1567, y 95 to 1000), the fraction of bright pixels (gray above 180), sampled every 8 px, is recorded in `Wave_Covered` of Frames.csv,
  and frames with 0.1 (`POPUP_COVER_MIN`) or more are considered covered (normally 0.02 or less; dialogs 0.3 to 0.8).
  Dialogs that do not hide the axis labels do not cause Abnormal, so they are detected this way. Hidden breaths are excluded when evaluating reading accuracy
- Frames in which fewer than three waveforms have readable axis labels (popups, other screens) are Abnormal (`Abnormal` column of Breaths.csv; Hidden in Alert since 0.2.5).
  Hamilton keeps drawing the waveforms in the background during a popup, so waveforms are also read for breaths containing Abnormal frames (0.8.0).
  If the image from which the columns would be read is in the middle of a popup, the four waveforms are read from the frame just after the popup disappears (Reread in Wave_Popup of Breaths.csv).
  If the popup lasts long (one sweep, 22 s or more) and those columns have been overwritten, only Paw is read from the airway pressure panel, which remains visible during the popup (PawOnly).
  If the screen is cleared in the meantime, or the airway pressure panel is not visible either, the waveform is blank
- Screens with a changed horizontal axis (sweep width 22 s) are not supported
- For breaths longer than one sweep (22 s), only the last sweep is kept (Long in Alert)
- The first and last breaths of a recording are truncated (Fragment in Alert)
- When the triangle cannot be seen (screen cleared, dialog, popup), Trigger is blank rather than C. In Ptranspulm 2026-10-09 (0.2.5), Trigger matched the Study Recorder
  for all 1103 A and 151 C breaths; 5 breaths were blank (B541, B854, B990, B991, B988)

## Author
- VentOCR Developer Team
