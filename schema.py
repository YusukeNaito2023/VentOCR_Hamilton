ENGINE_SETTINGS_SCHEMA = {
    "initial_ocr": {
        "ocr_keys": {
            "type": "multiselect",
            "primary": True,          # shown under "values to analyze" in the analysis dialog
            "choices": ["VTE", "Compliance", "Ppeak", "MinVol", "P01",
                        "Mode", "Set_Rate", "Set_Pinsp", "Set_PEEP", "Set_Oxygen"],
            "choice_labels": {
                "VTE": "Expiratory VT", "Compliance": "Compliance", "Ppeak": "Peak pressure",
                "MinVol": "Minute volume", "P01": "P0.1", "Mode": "Ventilation mode (setting)",
                "Set_Rate": "Rate (setting)", "Set_Pinsp": "ΔPinsp (setting)",
                "Set_PEEP": "PEEP/CPAP (setting)", "Set_Oxygen": "Oxygen (setting)"
            },
            "default": ["VTE", "Compliance", "Ppeak"],
            "label": "Values to OCR (the values used to split breaths are always read)"
        },
        "use_gpu": {
            "type": "bool",
            "default": True,
            "label": "Use GPU"
        },
        "diff_thresh": {
            "type": "float",
            "default": 3.0,
            "label": "Change that triggers a re-read (difference in mean brightness)"
        }
    },
    "post_ocr": {
        "breath_trigger": {
            "type": "multiselect",
            "choices": ["Compliance", "VTE"],
            "choice_labels": {"Compliance": "Compliance", "VTE": "Expiratory VT"},
            "default": ["Compliance", "VTE"],
            "label": "Breath boundary = update of these displayed values"
        },
        "boundary_shift_sec": {
            "type": "float",
            "default": -0.5,
            "label": "Shift of the boundary from the display update (s)"
        },
        "stable_frames": {
            "type": "int",
            "default": 2,
            "label": "Frames a new value must persist to count as a change (VTE, Compliance; at 10 fps, converted to the same time at other fps)"
        },
        "trim_to_breath": {
            "type": "bool",
            "default": True,
            "label": "Align waveform boundaries to the start of inspiration"
        },
        "ptranspulm": {
            "type": "bool",
            "primary": True,          # shown at the top of the analysis dialog (check for each video)
            "default": False,
            "label": "4th waveform is Ptranspulm (transpulmonary pressure); off = Pes (esophageal pressure). Do not switch during a recording"
        }
    }
}
