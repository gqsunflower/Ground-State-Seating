# -*- coding: utf-8 -*-
"""
seat_gui.py
===========
seat_optimizer.py の計算エンジンをそのまま使う、CSV読み込み対応のGUI。

- 好き嫌いCSVと座席レイアウトCSVをファイル選択ダイアログで読み込む
  (フォーマットは seat_optimizer.py の load_affinity_csv / load_seat_layout_csv 参照。
   sample_data/ フォルダにサンプルCSVあり)
- モード(net / split / lexicographic)を選んで「最適化を実行」を押すと結果が表示される
- 反発の重み・許容幅・対面回避設定などの細かいパラメータは、seat_optimizer.py 冒頭の
  CONFIG セクションの値がそのまま使われる(変えたい場合はそちらを編集する)

■ 起動方法
    python seat_gui.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import seat_optimizer as so


class SeatOptimizerGUI:
    def __init__(self, root):
        self.root = root
        root.title("座席配置最適化ツール")
        root.geometry("720x600")

        self.affinity_path = tk.StringVar()
        self.seat_path = tk.StringVar()
        self.mode = tk.StringVar(value=so.MODE)

        file_frame = ttk.LabelFrame(root, text="データの読み込み")
        file_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(file_frame, text="好き嫌いCSV:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(file_frame, textvariable=self.affinity_path, width=55, state="readonly").grid(row=0, column=1, padx=5)
        ttk.Button(file_frame, text="参照...", command=self.browse_affinity).grid(row=0, column=2, padx=5)

        ttk.Label(file_frame, text="座席レイアウトCSV:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        ttk.Entry(file_frame, textvariable=self.seat_path, width=55, state="readonly").grid(row=1, column=1, padx=5)
        ttk.Button(file_frame, text="参照...", command=self.browse_seat).grid(row=1, column=2, padx=5)

        ttk.Label(file_frame, text="(サンプル: sample_data/affinity_sample.csv, sample_data/seats_sample.csv)",
                  foreground="gray").grid(row=2, column=0, columnspan=3, sticky="w", padx=5)

        mode_frame = ttk.LabelFrame(root, text="計算モード")
        mode_frame.pack(fill="x", padx=10, pady=(0, 10))
        for i, (val, label) in enumerate([
            ('net', 'net（単純合算）'),
            ('split', 'split（引力・反発を分離）'),
            ('lexicographic', 'lexicographic（反発回避を優先）'),
        ]):
            ttk.Radiobutton(mode_frame, text=label, variable=self.mode, value=val).grid(
                row=0, column=i, padx=10, pady=5, sticky="w")

        ttk.Button(root, text="最適化を実行", command=self.run).pack(pady=5)

        self.output = scrolledtext.ScrolledText(root, font=("Consolas", 10), state="disabled")
        self.output.pack(fill="both", expand=True, padx=10, pady=10)

    def browse_affinity(self):
        path = filedialog.askopenfilename(title="好き嫌いCSVを選択", filetypes=[("CSVファイル", "*.csv")])
        if path:
            self.affinity_path.set(path)

    def browse_seat(self):
        path = filedialog.askopenfilename(title="座席レイアウトCSVを選択", filetypes=[("CSVファイル", "*.csv")])
        if path:
            self.seat_path.set(path)

    def run(self):
        aff_path = self.affinity_path.get()
        seat_path = self.seat_path.get()
        if not aff_path or not seat_path:
            messagebox.showerror("エラー", "好き嫌いCSVと座席レイアウトCSVの両方を選択してください。")
            return

        try:
            people, affinity = so.load_affinity_csv(aff_path)
            layout = so.load_seat_layout_csv(seat_path)
            params = dict(
                power=so.NET_POWER, att_power=so.ATT_POWER, rep_power=so.REP_POWER,
                rep_weight=so.REP_WEIGHT, buffer_pct=so.BUFFER_PCT,
                face_threshold=so.FACE_TO_FACE_REP_THRESHOLD, face_mode=so.FACE_TO_FACE_MODE,
                face_soft_penalty=so.FACE_TO_FACE_SOFT_PENALTY,
            )
            report = so.solve_and_format(
                people, affinity, layout, self.mode.get(), params,
                exact_threshold=so.EXACT_THRESHOLD, sa_restarts=so.SA_RESTARTS,
                sa_iters=so.SA_ITERS_PER_RESTART, facing_pairs=so.FACING_PAIRS,
            )
        except Exception as e:
            messagebox.showerror("エラー", f"{type(e).__name__}: {e}")
            return

        self.output.config(state="normal")
        self.output.delete("1.0", tk.END)
        self.output.insert(tk.END, report)
        self.output.config(state="disabled")


if __name__ == '__main__':
    root = tk.Tk()
    app = SeatOptimizerGUI(root)
    root.mainloop()
