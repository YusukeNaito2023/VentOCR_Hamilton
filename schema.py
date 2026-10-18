ENGINE_SETTINGS_SCHEMA = {
    "initial_ocr": {
        "ocr_keys": {
            "type": "multiselect",
            "primary": True,          # 解析ダイアログの「解析する値」に出す
            "choices": ["VTE", "Compliance", "Ppeak", "MinVol", "P01",
                        "Mode", "Set_Rate", "Set_Pinsp", "Set_PEEP", "Set_Oxygen"],
            "choice_labels": {
                "VTE": "呼気VT（換気量）", "Compliance": "コンプライアンス", "Ppeak": "ピーク圧",
                "MinVol": "分時換気量", "P01": "P0.1", "Mode": "換気モード（設定）",
                "Set_Rate": "呼吸回数（設定）", "Set_Pinsp": "Δ吸気圧（設定）",
                "Set_PEEP": "PEEP/CPAP（設定）", "Set_Oxygen": "酸素濃度（設定）"
            },
            "default": ["VTE", "Compliance", "Ppeak"],
            "label": "OCR する値（呼吸の区切りに使う値は常に読み取ります）"
        },
        "use_gpu": {
            "type": "bool",
            "default": True,
            "label": "GPU を使う"
        },
        "diff_thresh": {
            "type": "float",
            "default": 3.0,
            "label": "読み直す変化の大きさ（平均の明るさの差）"
        }
    },
    "post_ocr": {
        "breath_trigger": {
            "type": "multiselect",
            "choices": ["Compliance", "VTE"],
            "choice_labels": {"Compliance": "コンプライアンス", "VTE": "呼気VT"},
            "default": ["Compliance", "VTE"],
            "label": "呼吸の区切り = 表示が更新される値"
        },
        "boundary_shift_sec": {
            "type": "float",
            "default": -0.5,
            "label": "区切りを表示の更新からずらす（秒）"
        },
        "stable_frames": {
            "type": "int",
            "default": 2,
            "label": "値の変化とみなすのに続くフレーム数（VTE・コンプライアンス。10 fps のとき。ほかの fps では同じ時間に換算）"
        },
        "trim_to_breath": {
            "type": "bool",
            "default": True,
            "label": "波形の区切りを吸気の始まりに合わせる"
        }
    }
}
