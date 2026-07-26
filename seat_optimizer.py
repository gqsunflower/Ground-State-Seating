# -*- coding: utf-8 -*-
"""
seat_optimizer.py
==================
人数・座席配置パターンを自由に変えて使える座席最適化ツール（〜50人程度まで想定）。

好き嫌い(非対称でOK)のスコアから、"静電ポテンシャル的"なエネルギー
  E = Σ_pairs (この人たちを近づける/遠ざける力) / 距離^power
を計算し、それを最小化する座席割当を求める。

■ 使い方（2通り）
  A) コードに直接書く場合:
       下の CONFIG セクションの PEOPLE / AFFINITY / SEAT_LAYOUT を書き換える
  B) CSVから読み込む場合（人数が多いときはこちらを推奨）:
       USE_CSV = True にして、AFFINITY_CSV と SEAT_LAYOUT_CSV のパスを指定する
       - 好き嫌いCSV: 1行目に人名ヘッダー、1列目に人名、中身がスコア（正方行列）
       - 座席CSV: グリッドの各セルに座席名。空セルは通路（使用不可）扱い

■ 3つのモード
  'net'           : 相手↔自分の感情を単純に合算 (W=a_ij+a_ji) して E=-W/d^power を最小化
  'split'         : 引力(好き)と反発(嫌い)を別の力として扱い、別々のべき乗・重みを与える
  'lexicographic' : 反発（嫌い同士を離す）を最優先。その理論(近似)最小値のある許容幅(%)内で、
                    引力(好き同士を近づける)を最大化する二段階方式

■ 人数が多いとき
  N! が現実的な範囲(EXACT_THRESHOLD以下、目安9〜10人)なら総当たりの厳密解。
  それを超えると自動的に焼きなまし法（差分計算による高速版、複数回リスタート）に切替わる。
  50人程度までは数十秒〜数分程度で近似解が得られる想定（PC性能による）。
"""

import csv
import itertools
import math
import random
import time
import traceback

# =====================================================================
# CONFIG ここを書き換えてください
# =====================================================================

USE_CSV = False
AFFINITY_CSV = 'affinity.csv'      # USE_CSV=True のとき使用
SEAT_LAYOUT_CSV = 'seats.csv'      # USE_CSV=True のとき使用

PEOPLE = ['A', 'B', 'C', 'D', 'E', 'F', 'G']

AFFINITY = {
    'A': {'A': 0, 'B': 5, 'C': 5, 'D': -5, 'E': 0, 'F': 0, 'G': -5},
    'B': {'A': 3, 'B': 0, 'C': 5, 'D': -5, 'E': 0, 'F': 0, 'G': -5},
    'C': {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 0, 'G': 0},
    'D': {'A': -5, 'B': 2, 'C': -5, 'D': 0, 'E': 2, 'F': 3, 'G': -5},
    'E': {'A': 5, 'B': -5, 'C': 0, 'D': 0, 'E': 0, 'F': -5, 'G': -5},
    'F': {'A': -5, 'B': -5, 'C': -5, 'D': -5, 'E': -5, 'F': -5, 'G': -5},
    'G': {'A': 5, 'B': 5, 'C': 5, 'D': 5, 'E': 5, 'F': 5, 'G': 5},
}

SEAT_LAYOUT = [
    ['あ', 'い', 'う'],
    ['え', 'お', None],
    [None, None, None],
    ['か', 'き', None],
]

MODE = 'lexicographic'   # 'net' / 'split' / 'lexicographic'

NET_POWER = 1.0

ATT_POWER = 2.0
REP_POWER = 1.0
REP_WEIGHT = 1.5

BUFFER_PCT = 0.10

# --- 対面(視界に入る)回避の設定 ---
# どの座席同士が「対面」関係にあるかは部屋の机の向き次第で決まるので、自動推測はせず
# 必ず明示的にリストで指定する。(座席名, 座席名) のタプルのリスト。
FACING_PAIRS = [
    ('え', 'か'),
    ('お', 'き'),
]
FACE_TO_FACE_REP_THRESHOLD = -5   # 斥力R(=お互いの嫌い度合計)がこの値以下なら「大きい斥力」とみなす
FACE_TO_FACE_MODE = 'hard'        # 'hard'=絶対NG(実質的に除外) / 'soft'=大きいがペナルティで済ます
FACE_TO_FACE_SOFT_PENALTY = 50.0  # FACE_TO_FACE_MODE='soft' のときのペナルティ量

EXACT_THRESHOLD = 9      # これ以下の人数なら総当たり厳密解

# 焼きなまし法のパラメータ。Noneなら人数に応じて自動設定される。
SA_RESTARTS = None
SA_ITERS_PER_RESTART = None

# =====================================================================
# CSV読み込み
# =====================================================================

def load_affinity_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    header = [h.strip() for h in rows[0][1:]]
    affinity = {}
    for row in rows[1:]:
        name = row[0].strip()
        affinity[name] = {}
        for h, v in zip(header, row[1:]):
            affinity[name][h] = float(v) if v.strip() != '' else 0.0
    return header, affinity


def load_seat_layout_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))
    layout = []
    for row in rows:
        layout.append([cell.strip() if cell.strip() != '' else None for cell in row])
    return layout


# =====================================================================
# コアロジック
# =====================================================================

def distances_from_coords(coords):
    """座席名 -> (x, y) の辞書から、座席間のユークリッド距離の辞書を作る。"""
    dist = {s: {} for s in coords}
    for s1, (x1, y1) in coords.items():
        for s2, (x2, y2) in coords.items():
            dist[s1][s2] = math.hypot(x1 - x2, y1 - y2)
    return dist


def build_seat_distances(layout):
    coords = {}
    for y, row in enumerate(layout):
        for x, label in enumerate(row):
            if label is not None:
                if label in coords:
                    raise ValueError(f"座席名が重複しています: {label}")
                coords[label] = (x, y)
    return coords, distances_from_coords(coords)


def build_symmetric_weights(people, affinity):
    """W, A, R を people×people の対称な辞書(両方向アクセス可)として作る。"""
    W = {p: {} for p in people}
    A = {p: {} for p in people}
    R = {p: {} for p in people}
    for i, j in itertools.combinations(people, 2):
        aij, aji = affinity[i][j], affinity[j][i]
        w = aij + aji
        a = max(aij, 0) + max(aji, 0)
        r = min(aij, 0) + min(aji, 0)
        W[i][j] = W[j][i] = w
        A[i][j] = A[j][i] = a
        R[i][j] = R[j][i] = r
    return W, A, R


def build_facing_set(facing_pairs):
    """FACING_PAIRS を frozenset({座席,座席}) の集合にして O(1) 判定できるようにする。"""
    return {frozenset(pair) for pair in facing_pairs}


def random_assignment(people, seats, rnd):
    seats = list(seats)
    rnd.shuffle(seats)
    return dict(zip(people, seats))


class EnergyModel:
    """項(term)を1ペア単位で計算できるようにして、差分更新を可能にするクラス。"""

    def __init__(self, mode, people, W, A, R, dist, params, facing_set=None):
        self.mode = mode
        self.people = people
        self.W, self.A, self.R = W, A, R
        self.dist = dist
        self.p = params
        self.facing_set = facing_set or set()
        self.face_threshold = params.get('face_threshold', -5)
        self.face_mode = params.get('face_mode', 'hard')
        self.face_soft_penalty = params.get('face_soft_penalty', 50.0)

    def facing_penalty(self, i, j, seat_i, seat_j):
        """seat_i,seat_j が対面関係で、かつ i,j の斥力が大きいときのペナルティ。"""
        if not self.facing_set:
            return 0.0
        if frozenset((seat_i, seat_j)) not in self.facing_set:
            return 0.0
        if self.R[i][j] > self.face_threshold:
            return 0.0  # 斥力がそこまで大きくないので対面でも許容
        if self.face_mode == 'hard':
            return 1e9  # 実質的に選ばれないほど大きい値
        return self.face_soft_penalty

    def term(self, i, j, seat_i, seat_j):
        d = self.dist[seat_i][seat_j]
        fp = self.facing_penalty(i, j, seat_i, seat_j)
        if self.mode == 'net':
            return -self.W[i][j] / (d ** self.p['power']) + fp
        elif self.mode == 'split':
            return (-self.A[i][j] / (d ** self.p['att_power'])
                    + self.p['rep_weight'] * (-self.R[i][j]) / (d ** self.p['rep_power'])
                    + fp)
        elif self.mode == 'rep_only':
            return -self.R[i][j] / (d ** self.p['rep_power']) + fp
        elif self.mode == 'vector':  # (R項, A項) のタプルを返す。lexicographic段階2用
            r_term = -self.R[i][j] / (d ** self.p['rep_power']) + fp
            a_term = -self.A[i][j] / (d ** self.p['att_power'])
            return (r_term, a_term)
        else:
            raise ValueError(self.mode)

    def total(self, assign):
        others = self.people
        if self.mode == 'vector':
            r_tot = a_tot = 0.0
            for i, j in itertools.combinations(others, 2):
                rt, at = self.term(i, j, assign[i], assign[j])
                r_tot += rt
                a_tot += at
            return (r_tot, a_tot)
        total = 0.0
        for i, j in itertools.combinations(others, 2):
            total += self.term(i, j, assign[i], assign[j])
        return total

    def swap_delta(self, assign, p1, p2):
        """p1, p2 の座席を入れ替えたときのエネルギー差分(新-旧)を O(N) で計算。"""
        s1, s2 = assign[p1], assign[p2]
        others = [q for q in self.people if q != p1 and q != p2]
        if self.mode == 'vector':
            dr = da = 0.0
        else:
            delta = 0.0
        for q in others:
            sq = assign[q]
            if self.mode == 'vector':
                old1 = self.term(p1, q, s1, sq); old2 = self.term(p2, q, s2, sq)
                new1 = self.term(p1, q, s2, sq); new2 = self.term(p2, q, s1, sq)
                dr += (new1[0] + new2[0]) - (old1[0] + old2[0])
                da += (new1[1] + new2[1]) - (old1[1] + old2[1])
            else:
                old = self.term(p1, q, s1, sq) + self.term(p2, q, s2, sq)
                new = self.term(p1, q, s2, sq) + self.term(p2, q, s1, sq)
                delta += new - old
        # p1-p2 同士の距離はswapしても不変なので寄与は変化しない(計算不要)
        if self.mode == 'vector':
            return (dr, da)
        return delta


def simulated_annealing(people, seats, model, score_fn, restarts, iters, seed=None):
    """
    model: EnergyModel（差分計算に使用）
    score_fn: total値(スカラー or タプル) -> 比較用スカラー、を返す関数
    """
    rnd = random.Random(seed)
    best_assign, best_total, best_score = None, None, math.inf

    for _ in range(restarts):
        cur = random_assignment(people, seats, rnd)
        cur_total = model.total(cur)
        cur_score = score_fn(cur_total)
        T0, T1 = 1.0, 0.001
        for it in range(iters):
            T = T0 * ((T1 / T0) ** (it / iters))
            p1, p2 = rnd.sample(people, 2)
            delta = model.swap_delta(cur, p1, p2)
            if isinstance(cur_total, tuple):
                new_total = (cur_total[0] + delta[0], cur_total[1] + delta[1])
            else:
                new_total = cur_total + delta
            new_score = score_fn(new_total)
            d_score = new_score - cur_score
            if d_score <= 0 or rnd.random() < math.exp(-d_score / max(T, 1e-9)):
                cur[p1], cur[p2] = cur[p2], cur[p1]
                cur_total, cur_score = new_total, new_score
            # 却下時は何もしない(swapしていないので元に戻す必要もない)
        if cur_score < best_score:
            best_score, best_total, best_assign = cur_score, cur_total, dict(cur)

    return best_assign, best_total, best_score


def default_sa_params(n):
    """人数に応じたSAのデフォルトパラメータ。"""
    restarts = max(5, min(30, 300 // max(n, 1)))
    iters = max(3000, n * n * 15)
    return restarts, iters


def optimize(people, seats, W, A, R, dist, mode, params, exact_threshold=9,
             sa_restarts=None, sa_iters=None, facing_set=None):
    n = len(people)
    exact = n <= exact_threshold
    restarts, iters = sa_restarts, sa_iters
    if restarts is None or iters is None:
        d_restarts, d_iters = default_sa_params(n)
        restarts = restarts or d_restarts
        iters = iters or d_iters

    if mode in ('net', 'split'):
        model = EnergyModel(mode, people, W, A, R, dist, params, facing_set)
        if exact:
            best_assign, best_score = None, math.inf
            for perm in itertools.permutations(seats):
                assign = dict(zip(people, perm))
                s = model.total(assign)
                if s < best_score:
                    best_score, best_assign = s, assign
            return best_assign, best_score, True
        else:
            best_assign, best_total, best_score = simulated_annealing(
                people, seats, model, lambda t: t, restarts, iters)
            return best_assign, best_score, False

    elif mode == 'lexicographic':
        buffer_pct = params.get('buffer_pct', 0.10)
        if exact:
            rep_model = EnergyModel('rep_only', people, W, A, R, dist, params, facing_set)
            results = []
            for perm in itertools.permutations(seats):
                assign = dict(zip(people, perm))
                r = rep_model.total(assign)
                results.append((r, assign))
            min_rep = min(r for r, a in results)
            threshold = min_rep * (1 + buffer_pct) if min_rep != 0 else min_rep + buffer_pct
            candidates = [(r, a) for r, a in results if r <= threshold]

            def att_only(assign):
                return sum(-A[i][j] / (dist[assign[i]][assign[j]] ** params.get('att_power', 1.0))
                           for i, j in itertools.combinations(people, 2))
            candidates.sort(key=lambda t: att_only(t[1]))
            best_assign = candidates[0][1]
            best_rep = candidates[0][0]
            best_att = att_only(best_assign)
            return best_assign, (best_rep, best_att), True
        else:
            rep_model = EnergyModel('rep_only', people, W, A, R, dist, params, facing_set)
            _, _, approx_min_rep = simulated_annealing(
                people, seats, rep_model, lambda t: t, restarts, iters)
            threshold = (approx_min_rep * (1 + buffer_pct) if approx_min_rep != 0
                         else approx_min_rep + buffer_pct)

            vec_model = EnergyModel('vector', people, W, A, R, dist, params, facing_set)
            PENALTY = 1e6

            def score_fn(total):
                r_tot, a_tot = total
                return a_tot + PENALTY * max(0.0, r_tot - threshold)

            best_assign, best_total, _ = simulated_annealing(
                people, seats, vec_model, score_fn, restarts, iters)
            return best_assign, best_total, False
    else:
        raise ValueError(f"未知のモード: {mode}")


def check_facing_violations(assign, facing_set, R, threshold):
    """最終的な配置で、対面かつ斥力が大きいペアが残っていないか確認する。"""
    seat_to_person = {v: k for k, v in assign.items()}
    violations = []
    for pair in facing_set:
        s1, s2 = tuple(pair)
        if s1 in seat_to_person and s2 in seat_to_person:
            p1, p2 = seat_to_person[s1], seat_to_person[s2]
            if R[p1][p2] <= threshold:
                violations.append((p1, p2, s1, s2, R[p1][p2]))
    return violations


def format_score(score):
    """スコアを表示用に整形する(小数点1桁)。lexicographicモードはタプル(反発コスト, 引力スコア)なのでラベルを付ける。"""
    if isinstance(score, tuple):
        rep, att = score
        return f"反発コスト {rep:.1f} / 引力スコア {att:.1f}"
    return f"{score:.1f}"


def format_seating_chart(layout, assign):
    seat_to_person = {v: k for k, v in assign.items()}
    lines = []
    for row in layout:
        cells = []
        for label in row:
            if label is None:
                cells.append('  ×  ')
            else:
                p = seat_to_person.get(label, '?')
                cells.append(f'{label}:{p}')
        lines.append('  '.join(cells))
    return '\n'.join(lines)


def solve_and_format(people, affinity, layout, mode, params, exact_threshold=9,
                      sa_restarts=None, sa_iters=None, facing_pairs=None):
    """
    データ一式(人・好き嫌い・座席レイアウト)を受け取り、最適化を実行して
    結果レポート文字列を返す。CLI(main)とGUI(seat_gui.py)の両方から共通で使う。
    """
    coords, dist = build_seat_distances(layout)
    seats = list(coords.keys())
    if len(seats) != len(people):
        raise ValueError(f"人数({len(people)})と使用可能な座席数({len(seats)})が一致していません。")

    W, A, R = build_symmetric_weights(people, affinity)
    facing_set = build_facing_set(facing_pairs or [])

    t0 = time.time()
    best_assign, score, is_exact = optimize(
        people, seats, W, A, R, dist, mode, params,
        exact_threshold=exact_threshold, sa_restarts=sa_restarts, sa_iters=sa_iters,
        facing_set=facing_set)
    elapsed = time.time() - t0

    lines = [
        f"人数: {len(people)}人 / 座席数: {len(seats)}",
        f"モード: {mode}  {'(厳密解・総当たり)' if is_exact else '(近似解・焼きなまし法)'}",
        f"スコア: {format_score(score)}",
        f"計算時間: {elapsed:.2f}秒",
        "",
        "--- 最適配置 ---",
    ]
    for p in people:
        lines.append(f"  {p} → {best_assign[p]}")
    lines.append("")
    lines.append("--- 座席配置図 ---")
    lines.append(format_seating_chart(layout, best_assign))

    if facing_set:
        violations = check_facing_violations(best_assign, facing_set, R, params.get('face_threshold', -5))
        lines.append("")
        if violations:
            lines.append(f"⚠ 対面回避の制約({params.get('face_mode', 'hard')})が破られています（他に解がなかった可能性）:")
            for p1, p2, s1, s2, r in violations:
                lines.append(f"   {p1}({s1}) ⇔ {p2}({s2})  斥力R={r}")
        else:
            lines.append("対面回避の制約: 違反なし")

    return '\n'.join(lines)


def solve_and_format_coords(people, affinity, seat_coords, mode, params, exact_threshold=9,
                             sa_restarts=None, sa_iters=None, facing_pairs=None):
    """
    座席の位置を「座席名 -> (x, y)」の辞書で直接指定するバージョン。
    グリッド(layout)を前提にしない点だけが solve_and_format と異なる
    (GUIのドラッグ配置エディタ seat_gui.py から使う)。
    戻り値: (レポート文字列, 最適配置dict{person: seat})
    """
    seats = list(seat_coords.keys())
    if len(seats) != len(people):
        raise ValueError(f"人数({len(people)})と座席数({len(seats)})が一致していません。")

    dist = distances_from_coords(seat_coords)
    W, A, R = build_symmetric_weights(people, affinity)
    facing_set = build_facing_set(facing_pairs or [])

    t0 = time.time()
    best_assign, score, is_exact = optimize(
        people, seats, W, A, R, dist, mode, params,
        exact_threshold=exact_threshold, sa_restarts=sa_restarts, sa_iters=sa_iters,
        facing_set=facing_set)
    elapsed = time.time() - t0

    lines = [
        f"人数: {len(people)}人 / 座席数: {len(seats)}",
        f"モード: {mode}  {'(厳密解・総当たり)' if is_exact else '(近似解・焼きなまし法)'}",
        f"スコア: {format_score(score)}",
        f"計算時間: {elapsed:.2f}秒",
        "",
        "--- 最適配置 ---",
    ]
    for p in people:
        lines.append(f"  {p} → {best_assign[p]}")

    if facing_set:
        violations = check_facing_violations(best_assign, facing_set, R, params.get('face_threshold', -5))
        lines.append("")
        if violations:
            lines.append(f"⚠ 対面回避の制約({params.get('face_mode', 'hard')})が破られています（他に解がなかった可能性）:")
            for p1, p2, s1, s2, r in violations:
                lines.append(f"   {p1}({s1}) ⇔ {p2}({s2})  斥力R={r}")
        else:
            lines.append("対面回避の制約: 違反なし")

    return '\n'.join(lines), best_assign


def main():
    if USE_CSV:
        people, affinity = load_affinity_csv(AFFINITY_CSV)
        layout = load_seat_layout_csv(SEAT_LAYOUT_CSV)
    else:
        people, affinity, layout = PEOPLE, AFFINITY, SEAT_LAYOUT

    params = dict(power=NET_POWER, att_power=ATT_POWER, rep_power=REP_POWER,
                  rep_weight=REP_WEIGHT, buffer_pct=BUFFER_PCT,
                  face_threshold=FACE_TO_FACE_REP_THRESHOLD, face_mode=FACE_TO_FACE_MODE,
                  face_soft_penalty=FACE_TO_FACE_SOFT_PENALTY)

    report = solve_and_format(people, affinity, layout, MODE, params,
                               exact_threshold=EXACT_THRESHOLD, sa_restarts=SA_RESTARTS,
                               sa_iters=SA_ITERS_PER_RESTART, facing_pairs=FACING_PAIRS)
    print(report)


if __name__ == '__main__':
    # ダブルクリックで実行したとき、エラーでも正常終了でもコンソールが
    # 一瞬で閉じてしまわないように、最後にキー入力待ちを入れる。
    try:
        main()
    except Exception:
        traceback.print_exc()
    input("\n[Enterキーを押すと終了します]")
