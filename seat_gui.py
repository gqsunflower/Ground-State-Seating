# -*- coding: utf-8 -*-
"""
seat_gui.py
===========
seat_optimizer.py の計算エンジンをそのまま使うGUI。

- 「① メンバーの登録とその相性を設定」ボタン → 別ウィンドウでメンバーの追加(＋)・削除(－)、
  好き嫌いスコアの表(行=自分、列=相手)を編集できる
- 「② 座席を設定」ボタン → 別ウィンドウで、メンバー数と同じ数の□(座席)が
  用意され、ドラッグして自由に配置できる(□同士の距離がそのまま座席間距離になる)
- モード(net / split / lexicographic)を選んで「最適化を実行」を押すと、
  座席配置図(結果)とスコアの概要が表示される
- 「保存」「読込」ボタンで、メンバー・好き嫌いスコア・座席配置・モードを
  JSONファイルに保存/復元できる(次回起動時に続きから再開できる)。
  保存先はデフォルトで saved_data/ フォルダ。実名・個人の好き嫌いを含む
  データなので、このフォルダは .gitignore で除外しGitHubには上げない
- 「結果をPDFに出力」ボタンで、直近の実行結果(最適配置・座席配置図)をPDFとして
  保存できる(要 reportlab。日本語のメンバー名を正しく表示するために使用)

反発の重み・許容幅・対面回避設定などの細かいパラメータは、seat_optimizer.py
冒頭の CONFIG セクションの値がそのまま使われる(変えたい場合はそちらを編集する)。

■ 起動方法
    pip install reportlab   (PDF出力機能に必要。未インストールでも他の機能は使える)
    python seat_gui.py
"""

import json
import math
import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import seat_optimizer as so

# 座席エディタの初期グリッド間隔(px)。元のモデルは「隣接座席の距離=1」を前提に
# REP_WEIGHT等が調整されているので、キャンバス上のpx座標をこの値で割ってから
# 最適化エンジンに渡すことで、素のグリッド配置なら距離1に相当するよう揃える。
SEAT_UNIT_PX = 90

# 保存/読込のデフォルト保存先。メンバー名や好き嫌いスコアなど個人が特定できる
# 情報を含みうるため、このフォルダは .gitignore で除外している(GitHubに上げない)。
SAVE_DIR = "saved_data"


class AffinityEditor(tk.Toplevel):
    """メンバーの追加・削除と、好き嫌いスコア表の編集を行うウィンドウ。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("好き嫌いの設定")
        self.geometry("720x520")
        self.app = app

        # 元データを壊さないよう作業用コピーで編集し、OK時にのみ反映する
        self.people = list(app.people)
        self.affinity = {p: dict(app.affinity.get(p, {})) for p in self.people}
        self.entries = {}

        add_frame = ttk.Frame(self)
        add_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(add_frame, text="新しいメンバー名:").pack(side="left")
        self.new_name_var = tk.StringVar()
        name_entry = ttk.Entry(add_frame, textvariable=self.new_name_var, width=15)
        name_entry.pack(side="left", padx=5)
        name_entry.bind("<Return>", lambda e: self.add_member())
        ttk.Button(add_frame, text="＋ 追加", command=self.add_member).pack(side="left")

        ttk.Label(self, text="行=自分の気持ち、列=相手。例：Aの行のB列＝AがBをどう思うか\n"
                             "+5に近いほど「好き」、-5に近いほど「嫌い」、0＝どちらでもない",
                  foreground="gray", justify="left").pack(padx=10, anchor="w")

        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=5)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        vscroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        hscroll = ttk.Scrollbar(container, orient="horizontal", command=canvas.xview)
        canvas.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")

        self.matrix_frame = ttk.Frame(canvas)
        self.matrix_window = canvas.create_window((0, 0), window=self.matrix_frame, anchor="nw")
        self.matrix_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self.canvas = canvas

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="OK", command=self.on_ok).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="キャンセル", command=self.destroy).pack(side="right")

        self.rebuild_matrix()

    def sync_entries_to_affinity(self):
        """再構築(追加・削除)でウィジェットが作り直される前に、入力済みの値を保存する。"""
        for (pi, pj), var in self.entries.items():
            try:
                self.affinity[pi][pj] = float(var.get())
            except ValueError:
                pass  # 入力途中(空・不正値)はそのまま既存値を保持

    def add_member(self):
        name = self.new_name_var.get().strip()
        if not name:
            messagebox.showerror("エラー", "メンバー名を入力してください。", parent=self)
            return
        if name in self.people:
            messagebox.showerror("エラー", "同じ名前のメンバーが既にいます。", parent=self)
            return
        self.sync_entries_to_affinity()
        self.people.append(name)
        self.affinity[name] = {}
        for p in self.people:
            self.affinity[name].setdefault(p, 0.0)
            self.affinity[p].setdefault(name, 0.0)
        self.new_name_var.set("")
        self.rebuild_matrix()

    def remove_member(self, name):
        self.sync_entries_to_affinity()
        self.people.remove(name)
        del self.affinity[name]
        for p in self.affinity:
            self.affinity[p].pop(name, None)
        self.rebuild_matrix()

    def rebuild_matrix(self):
        for w in self.matrix_frame.winfo_children():
            w.destroy()
        self.entries = {}

        ttk.Label(self.matrix_frame, text="自＼相", borderwidth=1, relief="solid", width=12,
                  anchor="center").grid(row=0, column=0, sticky="nsew")
        for j, pj in enumerate(self.people):
            ttk.Label(self.matrix_frame, text=pj, borderwidth=1, relief="solid", width=8,
                      anchor="center").grid(row=0, column=j + 1, sticky="nsew")

        for i, pi in enumerate(self.people):
            row_header = ttk.Frame(self.matrix_frame, borderwidth=1, relief="solid")
            row_header.grid(row=i + 1, column=0, sticky="nsew")
            ttk.Label(row_header, text=pi, width=7, anchor="center").pack(side="left")
            ttk.Button(row_header, text="－", width=2,
                       command=lambda name=pi: self.remove_member(name)).pack(side="left")

            for j, pj in enumerate(self.people):
                if pi == pj:
                    ttk.Label(self.matrix_frame, text="―", width=8, anchor="center",
                              borderwidth=1, relief="solid").grid(row=i + 1, column=j + 1, sticky="nsew")
                else:
                    var = tk.StringVar(value=str(self.affinity[pi].get(pj, 0.0)))
                    entry = ttk.Entry(self.matrix_frame, textvariable=var, width=8, justify="center")
                    entry.grid(row=i + 1, column=j + 1, sticky="nsew")
                    self.entries[(pi, pj)] = var

    def on_ok(self):
        if len(self.people) < 2:
            messagebox.showerror("エラー", "メンバーは2人以上必要です。", parent=self)
            return
        try:
            for (pi, pj), var in self.entries.items():
                self.affinity[pi][pj] = float(var.get())
        except ValueError:
            messagebox.showerror("エラー", "スコアは数値で入力してください。", parent=self)
            return

        self.app.people = list(self.people)
        self.app.affinity = self.affinity
        self.app.refresh_status()
        self.destroy()


class SeatEditor(tk.Toplevel):
    """メンバー数と同じ数の□をドラッグで配置するウィンドウ。"""

    CANVAS_W = 620
    CANVAS_H = 420
    SQUARE = 56

    def __init__(self, parent, app):
        super().__init__(parent)
        self.title("座席の設定")
        self.app = app

        n = len(app.people)
        if n < 1:
            messagebox.showerror("エラー", "先に「① メンバーの登録とその相性を設定」でメンバーを登録してください。", parent=self)
            self.destroy()
            return

        self.geometry(f"{self.CANVAS_W + 40}x{self.CANVAS_H + 130}")
        self.labels = [f"席{i + 1}" for i in range(n)]

        # 既に同じ人数分の座席配置があれば引き継ぐ。人数が変わっていれば初期グリッドから作り直す
        if len(app.seat_coords) == n:
            self.coords = dict(app.seat_coords)
        else:
            self.coords = self.default_grid_coords(n)

        ttk.Label(self, text=f"□をドラッグして座席の配置(距離関係)を調整してください。(メンバー数: {n})").pack(
            padx=10, pady=(10, 5), anchor="w")

        self.canvas = tk.Canvas(self, width=self.CANVAS_W, height=self.CANVAS_H,
                                 background="white", relief="solid", borderwidth=1)
        self.canvas.pack(padx=10, pady=5)

        self.items = {}
        self.drag_data = {"label": None, "x": 0, "y": 0}
        for label in self.labels:
            self.draw_square(label)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="グリッドに整列", command=self.reset_grid).pack(side="left")
        ttk.Button(btn_frame, text="OK", command=self.on_ok).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="キャンセル", command=self.destroy).pack(side="right")

    def default_grid_coords(self, n):
        cols = max(1, math.ceil(math.sqrt(n)))
        coords = {}
        for i in range(n):
            row, col = divmod(i, cols)
            coords[f"席{i + 1}"] = (80 + col * SEAT_UNIT_PX, 80 + row * SEAT_UNIT_PX)
        return coords

    def reset_grid(self):
        self.coords = self.default_grid_coords(len(self.labels))
        for label in self.labels:
            x, y = self.coords[label]
            rect_id, text_id = self.items[label]
            half = self.SQUARE / 2
            self.canvas.coords(rect_id, x - half, y - half, x + half, y + half)
            self.canvas.coords(text_id, x, y)

    def draw_square(self, label):
        x, y = self.coords[label]
        half = self.SQUARE / 2
        rect_id = self.canvas.create_rectangle(
            x - half, y - half, x + half, y + half,
            fill="#cfe8ff", outline="#4a90d9", width=2)
        text_id = self.canvas.create_text(x, y, text=label)
        self.items[label] = (rect_id, text_id)
        for item_id in (rect_id, text_id):
            self.canvas.tag_bind(item_id, "<ButtonPress-1>", lambda e, l=label: self.on_press(e, l))
            self.canvas.tag_bind(item_id, "<B1-Motion>", lambda e, l=label: self.on_motion(e, l))

    def on_press(self, event, label):
        self.drag_data = {"label": label, "x": event.x, "y": event.y}

    def on_motion(self, event, label):
        dx = event.x - self.drag_data["x"]
        dy = event.y - self.drag_data["y"]
        rect_id, text_id = self.items[label]
        self.canvas.move(rect_id, dx, dy)
        self.canvas.move(text_id, dx, dy)
        x0, y0, x1, y1 = self.canvas.coords(rect_id)
        self.coords[label] = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.drag_data["x"] = event.x
        self.drag_data["y"] = event.y

    def on_ok(self):
        self.app.seat_coords = dict(self.coords)
        self.app.refresh_status()
        self.destroy()


class SeatOptimizerGUI:
    def __init__(self, root):
        self.root = root
        root.title("座席配置最適化ツール")
        root.geometry("760x800")

        self.people = []
        self.affinity = {}
        self.seat_coords = {}
        self.mode = tk.StringVar(value=so.MODE)

        # 直近の「最適化を実行」結果(PDF出力用に保持しておく)
        self.last_people = None
        self.last_assign = None
        self.last_seat_coords = None
        self.last_summary = ""

        top_row = ttk.Frame(root)
        top_row.pack(fill="x", padx=10, pady=10)
        top_row.columnconfigure(0, weight=1)
        top_row.columnconfigure(1, weight=1)

        setup_frame = ttk.LabelFrame(top_row, text="設定")
        setup_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        ttk.Button(setup_frame, text="① メンバーの登録とその相性を設定...", command=self.open_affinity_editor).grid(
            row=0, column=0, padx=10, pady=10, sticky="w")
        self.affinity_status = ttk.Label(setup_frame, text="未設定(0人)")
        self.affinity_status.grid(row=0, column=1, sticky="w")

        ttk.Button(setup_frame, text="② 座席を設定...", command=self.open_seat_editor).grid(
            row=1, column=0, padx=10, pady=10, sticky="w")
        self.seat_status = ttk.Label(setup_frame, text="未設定")
        self.seat_status.grid(row=1, column=1, sticky="w")

        io_frame = ttk.LabelFrame(top_row, text="データの保存・読込(次回に続きから再開できます)")
        io_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ttk.Button(io_frame, text="保存...", command=self.save_data).pack(side="left", padx=10, pady=10)
        ttk.Button(io_frame, text="読込...", command=self.load_data).pack(side="left")

        mode_frame = ttk.LabelFrame(
            root, text="計算モード （3つとも独立した発想のモデルですが、多くの場合ほぼ同じ結論に収束します）")
        mode_frame.pack(fill="x", padx=10, pady=(0, 10))

        MODE_INFO = [
            ('net', 'net（単純合算）',
             '好き嫌いを単純に合算して計算する、最初に作られたシンプルなモデル'),
            ('split', 'split（引力・反発を分離）',
             '「好き」と「嫌い」を別の力として扱う。片思いの「嫌い」が相殺されて消えないよう改良されたモデル'),
            ('lexicographic', 'lexicographic（反発回避を優先）',
             '「嫌い同士を離す」ことを最優先にしたうえで、その次に「好き同士を近づける」ことを狙うモデル'),
        ]
        for i, (val, name, desc) in enumerate(MODE_INFO):
            radio = ttk.Radiobutton(mode_frame, variable=self.mode, value=val)
            radio.grid(row=i, column=0, padx=(10, 5), pady=8, sticky="nw")

            text_frame = ttk.Frame(mode_frame)
            text_frame.grid(row=i, column=1, padx=(0, 10), pady=8, sticky="w")
            name_label = ttk.Label(text_frame, text=name, font=("", 10, "bold"))
            name_label.pack(anchor="w")
            desc_label = ttk.Label(text_frame, text=desc, foreground="gray",
                                    wraplength=600, justify="left")
            desc_label.pack(anchor="w")

            # 名前・説明のクリックでもトグルを選択できるようにする
            for widget in (name_label, desc_label):
                widget.bind("<Button-1>", lambda e, v=val: self.mode.set(v))

        run_frame = ttk.Frame(root)
        run_frame.pack(pady=5)
        ttk.Button(run_frame, text="最適化を実行", command=self.run).pack(side="left", padx=5)
        ttk.Button(run_frame, text="結果をPDFに出力...", command=self.export_pdf).pack(side="left", padx=5)

        self.status_label = ttk.Label(root, text="", foreground="gray")
        self.status_label.pack(padx=10, pady=(0, 5), anchor="w")

        chart_frame = ttk.LabelFrame(root, text="結果(座席配置図)")
        chart_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        chart_frame.rowconfigure(0, weight=1)
        chart_frame.columnconfigure(0, weight=1)

        self.result_canvas = tk.Canvas(chart_frame, background="white")
        result_vscroll = ttk.Scrollbar(chart_frame, orient="vertical", command=self.result_canvas.yview)
        result_hscroll = ttk.Scrollbar(chart_frame, orient="horizontal", command=self.result_canvas.xview)
        self.result_canvas.configure(yscrollcommand=result_vscroll.set, xscrollcommand=result_hscroll.set)
        self.result_canvas.grid(row=0, column=0, sticky="nsew")
        result_vscroll.grid(row=0, column=1, sticky="ns")
        result_hscroll.grid(row=1, column=0, sticky="ew")

    def open_affinity_editor(self):
        AffinityEditor(self.root, self)

    def open_seat_editor(self):
        SeatEditor(self.root, self)

    def refresh_status(self):
        if self.people:
            self.affinity_status.config(text=f"{len(self.people)}人: {'、'.join(self.people)}")
        else:
            self.affinity_status.config(text="未設定(0人)")
        if self.seat_coords:
            self.seat_status.config(text=f"{len(self.seat_coords)}席 設定済み")
        else:
            self.seat_status.config(text="未設定")

    def save_data(self):
        if not self.people:
            messagebox.showerror("エラー", "保存するデータがありません。先に「① メンバーの登録とその相性を設定」を行ってください。")
            return
        os.makedirs(SAVE_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="保存先を選択", initialdir=SAVE_DIR, defaultextension=".json",
            filetypes=[("JSONファイル", "*.json")])
        if not path:
            return
        data = {
            "people": self.people,
            "affinity": self.affinity,
            "seat_coords": {label: list(xy) for label, xy in self.seat_coords.items()},
            "mode": self.mode.get(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            messagebox.showerror("エラー", f"保存に失敗しました: {e}")
            return
        messagebox.showinfo("保存完了", f"保存しました:\n{path}")

    def load_data(self):
        os.makedirs(SAVE_DIR, exist_ok=True)
        path = filedialog.askopenfilename(
            title="読み込むファイルを選択", initialdir=SAVE_DIR,
            filetypes=[("JSONファイル", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            people = list(data["people"])
            affinity = dict(data["affinity"])
            seat_coords = {label: tuple(xy) for label, xy in data.get("seat_coords", {}).items()}
            mode = data.get("mode", so.MODE)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
            messagebox.showerror("エラー", f"読み込みに失敗しました: {e}")
            return

        self.people = people
        self.affinity = affinity
        self.seat_coords = seat_coords
        self.mode.set(mode)
        self.refresh_status()
        messagebox.showinfo("読込完了", f"読み込みました:\n{path}")

    def run(self):
        if len(self.people) < 2:
            messagebox.showerror("エラー", "先に「① メンバーの登録とその相性を設定」でメンバーを2人以上登録してください。")
            return
        if len(self.seat_coords) != len(self.people):
            messagebox.showerror(
                "エラー",
                f"座席数({len(self.seat_coords)})とメンバー数({len(self.people)})が一致していません。\n"
                "「② 座席を設定」で、メンバー数と同じ数の座席を配置してください。")
            return

        try:
            params = dict(
                power=so.NET_POWER, att_power=so.ATT_POWER, rep_power=so.REP_POWER,
                rep_weight=so.REP_WEIGHT, buffer_pct=so.BUFFER_PCT,
                face_threshold=so.FACE_TO_FACE_REP_THRESHOLD, face_mode=so.FACE_TO_FACE_MODE,
                face_soft_penalty=so.FACE_TO_FACE_SOFT_PENALTY,
            )
            # px座標のままだと1マス=90px前後になり、モデルが前提とする「隣接=距離1」から
            # ずれて引力/反発のバランスが崩れるため、SEAT_UNIT_PXで正規化してから渡す
            scaled_coords = {label: (x / SEAT_UNIT_PX, y / SEAT_UNIT_PX)
                              for label, (x, y) in self.seat_coords.items()}
            report, best_assign = so.solve_and_format_coords(
                self.people, self.affinity, scaled_coords, self.mode.get(), params,
                exact_threshold=so.EXACT_THRESHOLD, sa_restarts=so.SA_RESTARTS,
                sa_iters=so.SA_ITERS_PER_RESTART,
            )
        except Exception as e:
            messagebox.showerror("エラー", f"{type(e).__name__}: {e}")
            return

        # report のうち先頭部分(モード・スコア・計算時間)だけをステータス行として表示する
        summary = " ｜ ".join(line for line in report.splitlines()[1:4])
        self.status_label.config(text=summary)

        # PDF出力用に、この時点のデータをスナップショットとして保持しておく
        # (後でメンバー編集などをしても、PDFには実行時点の結果が正しく出るように)
        self.last_people = list(self.people)
        self.last_assign = best_assign
        self.last_seat_coords = dict(self.seat_coords)
        self.last_summary = summary

        self.draw_result_chart(best_assign)

    def draw_result_chart(self, best_assign):
        canvas = self.result_canvas
        canvas.delete("all")
        if not self.seat_coords:
            canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        seat_to_person = {v: k for k, v in best_assign.items()}
        pad = 50
        for label, (x, y) in self.seat_coords.items():
            cx, cy = x + pad, y + pad
            canvas.create_rectangle(cx - 30, cy - 20, cx + 30, cy + 20,
                                     fill="#d6f5d6", outline="#2e8b57", width=2)
            person = seat_to_person.get(label, "?")
            canvas.create_text(cx, cy - 8, text=person, font=("", 10, "bold"))
            canvas.create_text(cx, cy + 10, text=label, font=("", 7), fill="gray")

        canvas.configure(scrollregion=canvas.bbox("all"))

    def export_pdf(self):
        if not self.last_assign:
            messagebox.showerror("エラー", "先に「最適化を実行」で結果を出してください。")
            return

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.pdfgen import canvas as pdfcanvas
        except ImportError:
            messagebox.showerror(
                "エラー",
                "PDF出力には reportlab が必要です。\n"
                "コマンドプロンプトで次を実行してからもう一度お試しください:\n\n"
                "pip install reportlab")
            return

        path = filedialog.asksaveasfilename(
            title="PDFの保存先を選択", defaultextension=".pdf",
            filetypes=[("PDFファイル", "*.pdf")])
        if not path:
            return

        try:
            self._write_result_pdf(path, A4, mm, pdfmetrics, UnicodeCIDFont, pdfcanvas)
        except Exception as e:
            messagebox.showerror("エラー", f"PDF出力に失敗しました: {e}")
            return
        messagebox.showinfo("出力完了", f"PDFを出力しました:\n{path}")

    def _write_result_pdf(self, path, A4, mm, pdfmetrics, UnicodeCIDFont, pdfcanvas):
        font_name = 'HeiseiKakuGo-W5'  # reportlab同梱の日本語CIDフォント(追加のフォントファイル不要)
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))

        page_w, page_h = A4
        margin = 18 * mm
        c = pdfcanvas.Canvas(path, pagesize=A4)

        y = page_h - margin
        c.setFont(font_name, 16)
        c.drawString(margin, y, "座席配置最適化 結果")
        y -= 9 * mm

        c.setFont(font_name, 9)
        c.drawString(margin, y, self.last_summary)
        y -= 8 * mm

        c.setFont(font_name, 11)
        c.drawString(margin, y, "最適配置")
        y -= 6 * mm

        c.setFont(font_name, 9)
        for p in self.last_people:
            c.drawString(margin + 4 * mm, y, f"{p} → {self.last_assign[p]}")
            y -= 5 * mm
            if y < margin + 70 * mm:
                c.showPage()
                c.setFont(font_name, 9)
                y = page_h - margin

        y -= 6 * mm
        c.setFont(font_name, 11)
        c.drawString(margin, y, "座席配置図")
        y -= 4 * mm

        self._draw_seat_chart_on_pdf(c, font_name, margin, page_w, page_h, y)
        c.save()

    def _draw_seat_chart_on_pdf(self, c, font_name, margin, page_w, page_h, chart_top):
        from reportlab.lib.units import mm

        seat_coords = self.last_seat_coords
        seat_to_person = {v: k for k, v in self.last_assign.items()}
        box_w, box_h = 26 * mm, 16 * mm
        chart_bottom = margin
        chart_area_w = page_w - 2 * margin
        chart_area_h = max(chart_top - chart_bottom, box_h)

        xs = [p[0] for p in seat_coords.values()]
        ys = [p[1] for p in seat_coords.values()]
        span_x = max(max(xs) - min(xs), 1.0)
        span_y = max(max(ys) - min(ys), 1.0)
        scale = min((chart_area_w - box_w) / span_x, (chart_area_h - box_h) / span_y)
        scale = min(scale, 1.0)  # px座標をそのまま拡大しすぎないようにする

        for label, (x, yy) in seat_coords.items():
            cx = margin + box_w / 2 + (x - min(xs)) * scale
            cy = chart_top - box_h / 2 - (yy - min(ys)) * scale
            c.setFillColorRGB(0.84, 0.96, 0.84)
            c.roundRect(cx - box_w / 2, cy - box_h / 2, box_w, box_h, 3, fill=1, stroke=1)
            c.setFillColorRGB(0, 0, 0)
            c.setFont(font_name, 10)
            c.drawCentredString(cx, cy + 1.5 * mm, seat_to_person.get(label, "?"))
            c.setFont(font_name, 7)
            c.drawCentredString(cx, cy - 4.5 * mm, label)


if __name__ == '__main__':
    root = tk.Tk()
    app = SeatOptimizerGUI(root)
    root.mainloop()
