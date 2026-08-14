"""
レース組み立て（通過イベント列 → マシンごとのラップ・セクター・合計）

14章 DA3/DA4/DA7 の実装。DB非依存の純粋関数。
rotation.py の同定エンジンを使い、GWから届いた通過イベントを
「スタートレーンごとのマシン」に束ね、ラップ・セクター・合計を組み上げる。

前提（理想状態版）:
  全車完走・欠測なし・予定外なし。イレギュラーは14章 R1-R10 で後日。

入力イベント（GWが記録した材料・14章 DA3）:
  PassEvent(node_id, lane, t_us, t_us_b, quality, seq)
  - node_id : 通過したゲートの実機ID（レイアウトでゲート位置に対応づく）
  - lane    : 物理レーン 1..3
  - t_us    : ビームAの打刻（GW時刻・µs）
  - t_us_b  : ビームBの打刻（速度算出用・任意）
  - quality : 0=正常 / 1=片ビーム欠 / 3=未同期（S4）
  レース単位のメタ:
  - green_t_us : 緑を出した時刻（None なら走行式・DA4）
  - heat_id    : ヒートID（対応づけ用タグ・DA5）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.domain.rotation import (
    CourseModel,
    Gate,
    build_course,
    identify_start_lane,
    LayoutElement,
    LANES,
)


# ---------------------------------------------------------------------------
# 入力データ
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PassEvent:
    """1つのゲート通過イベント（GWが記録した材料）。"""

    node_id: int          # 通過したゲートの実機ID
    lane: int             # 物理レーン 1..3
    t_us: int             # ビームAの打刻（GW時刻・µs）
    t_us_b: int | None = None   # ビームBの打刻（速度用・任意）
    quality: int = 0      # 0正常 / 1片ビーム欠 / 3未同期
    seq: int = 0          # 発生ノードごとの通番（欠番検出用）
    id: int | None = None  # timing_events.id（点除外訂正で参照・J群 §24.82）


StartMode = Literal["f1", "run"]  # F1式 / 走行式


def mode_mismatch(planned_mode: str | None, green_t_us: int | None) -> bool:
    """D7/E6(24.34)：予定モードと実測（green_t_us の有無）の食い違い判定。

    リアルタイム判別はせず、結果をヒートへ反映(apply)する時点の事後チェックで
    「予定と実際のズレ」に気づくための純粋関数（24.34 確定方針）。

      planned_mode : 'f1'（F1式・緑あり予定）/ 'run'（走行式・緑なし予定）
                     / None（予定未設定＝チェックしない）
      green_t_us   : 実測レコードの緑時刻。None なら走行式で走ったと判別（DA4）。

    予定f1なのに緑なし、予定runなのに緑あり、なら食い違い(True)。
    予定未設定(None)や未知の値のときはチェックしない(False)。
    """
    if planned_mode not in ("f1", "run"):
        return False
    actual: StartMode = "f1" if green_t_us is not None else "run"
    return planned_mode != actual


# ---------------------------------------------------------------------------
# 出力データ
# ---------------------------------------------------------------------------

@dataclass
class SectorTime:
    """区間タイム（あるゲート→次のゲート）。"""

    from_gate_index: int
    to_gate_index: int
    dt_us: int
    to_event_id: int | None = None  # 到達点（to_gate）の元イベントid（J群 §24.82）


@dataclass
class LapResult:
    """1周分の結果。"""

    lap: int                       # 1始まり
    lap_time_us: int               # そのラップのタイム
    sectors: list[SectorTime] = field(default_factory=list)


@dataclass
class MachineResult:
    """1台（1スタートレーン）分の結果。"""

    start_lane: int
    laps: list[LapResult] = field(default_factory=list)
    total_time_us: int | None = None   # 完了時のみ
    completed_laps: int = 0
    dnf: bool = False                   # E5(24.39)：CO＝規定周回未達。DNF表示・ranking末尾
    jump_start: bool = False            # G1(24.53)：フライング。F1式でS/G通過が緑より前
    missing: bool = False               # E1(24.37)：通過回数の不整合＝欠測あり（⚠要確認）

    @property
    def best_lap_us(self) -> int | None:
        if not self.laps:
            return None
        return min(l.lap_time_us for l in self.laps)


@dataclass
class RaceResult:
    """レース1本（1ヒート）分の結果。"""

    heat_id: int | None
    mode: StartMode
    target_laps: int
    machines: dict[int, MachineResult] = field(default_factory=dict)  # start_lane -> result

    def ranking(self) -> list[MachineResult]:
        """合計タイムの昇順（完了者優先）。未完了は末尾。"""
        done = [m for m in self.machines.values() if m.total_time_us is not None]
        notdone = [m for m in self.machines.values() if m.total_time_us is None]
        done.sort(key=lambda m: m.total_time_us)  # type: ignore[arg-type]
        # E5(24.39)：DNF・計測中は完了者の後ろ。未完了どうしは周回数の多い順
        #   （途中経過を活かし、より進んだ車を上に）。
        notdone.sort(key=lambda m: m.completed_laps, reverse=True)
        return done + notdone


# ---------------------------------------------------------------------------
# 組み立て本体
# ---------------------------------------------------------------------------

def _node_to_gate(course: CourseModel) -> dict[int, Gate]:
    """node_id → Gate の対応表。"""
    return {g.node_id: g for g in course.gates if g.node_id is not None}


def _events_complete(
    grouped: dict[tuple[int, int], list],
    course: CourseModel,
    sg_gate: Gate,
    target_laps: int,
    lanes: int,
) -> bool:
    """データが「全車完走・欠測なし」で完全かを判定する。

    完全なら各ゲートの通過総数は
      S/G  = lanes * (target_laps + 1)
      各SQ = lanes * target_laps
    と一致する。CO/DNS/スキップ/剥奪(E3/E4)が1つでもあれば総数が減るので False。
    完全なときだけ現行の出現順同定（厳密・無回帰）を使う。
    """
    def cnt(gate_index: int) -> int:
        return sum(len(v) for (ln, gi), v in grouped.items() if gi == gate_index)

    if cnt(sg_gate.index) != lanes * (target_laps + 1):
        return False
    for g in course.gates:
        if g.kind == "SQ" and cnt(g.index) != lanes * target_laps:
            return False
    return True


def _reconstruct_incomplete(
    grouped: dict[tuple[int, int], list],
    course: CourseModel,
    sg_gate: Gate,
    target_laps: int,
    lanes: int,
    green_t_us: int | None,
    mode: StartMode,
) -> dict[int, dict[int, dict[int, tuple[int, PassEvent]]]]:
    """欠測あり（CO/DNS/スキップ）向けの再構成。

    出現順の前提が崩れるため、マシンごとに「自分の開始時刻と1周ペースで次周を予測し、
    その時間窓内で予測に最も近いS/G通過だけを自分のものとして拾う」。脱落したマシンが
    他車の後続打刻を横取りしないので誤同定を避けられる（22章・R系検証で確定）。

    出力は現行と同じ machine_passings 構造:
      machine_passings[start_lane][gate_index][lap_key] = (t_us, event)
      S/G は lap_key=passing(0=スタート,1..=周完了)、SQ は lap_key=lap(1..)。
    これをそのまま既存の下流（ラップ/セクター/total/dnf/missing 組立）に渡す。
    """
    from app.domain.rotation import expected_lane, expected_sg_lane

    rot = course.rot_total
    sg_index = sg_gate.index
    n_gates = len(course.gates)

    # 消費フラグ付きの可変在庫（grouped は時刻昇順済み）: (phys, gate_index) -> [[t, ev, used], ...]
    avail: dict[tuple[int, int], list] = {}
    for (ln, gi), lst in grouped.items():
        avail[(ln, gi)] = [[t, ev, False] for (t, ev, _g) in lst]

    sg_all = [it[0] for (ph, gi), l in avail.items() if gi == sg_index for it in l]
    if not sg_all:
        return {}
    # t0: F1式は緑、走行式はS/G最初の打刻。avg: 1周のおおよその長さ（窓推定用）。
    t0 = green_t_us if (mode == "f1" and green_t_us is not None) else min(sg_all)
    span = max(sg_all) - t0
    avg = max(span / target_laps, 1) if target_laps > 0 else max(span, 1)

    def claim(phys: int, gi: int, lo: float, hi: float, pred: float):
        """(phys, gi) の未消費イベントのうち (lo, hi] 内で pred に最も近い1件を消費して返す。"""
        lst = avail.get((phys, gi))
        if not lst:
            return None
        best = None
        for it in lst:
            if it[2]:
                continue
            if lo < it[0] <= hi:
                d = abs(it[0] - pred)
                if best is None or d < best[1]:
                    best = (it, d)
        if best is None:
            return None
        best[0][2] = True
        return (best[0][0], best[0][1])  # (t_us, ev)

    machine_passings: dict[int, dict[int, dict[int, tuple[int, PassEvent]]]] = {}

    for s in range(1, lanes + 1):
        # スタート打刻（緑直後・物理レーン=s）
        L0 = expected_sg_lane(s, 0, rot, lanes)
        st = claim(L0, sg_index, t0 - 1, t0 + 0.6 * avg, t0)
        if st is None:
            continue  # スタート打刻なし＝このレーンはデータなし（DNS等）。枠は作らない。
        sgmap: dict[int, tuple[int, PassEvent]] = {0: st}

        # 1周目（自ペース未知→globalの avg で窓）
        L1 = expected_sg_lane(s, 1, rot, lanes)
        p1 = claim(L1, sg_index, st[0] + 0.35 * avg, st[0] + 1.7 * avg, st[0] + avg)
        comp = 0
        if p1 is not None:
            sgmap[1] = p1
            lap_s = max(p1[0] - st[0], 1)
            last = p1[0]
            comp = 1
            # 2周目以降は自分の1周ペースで窓を絞る（横取り=約1周ズレは窓外で棄却）
            for p in range(2, target_laps + 1):
                Lp = expected_sg_lane(s, p, rot, lanes)
                cp = claim(Lp, sg_index, last + 0.45 * lap_s, last + 1.55 * lap_s, last + lap_s)
                if cp is None:
                    break
                sgmap[p] = cp
                last = cp[0]
                comp = p

        gates_map: dict[int, dict[int, tuple[int, PassEvent]]] = {sg_index: sgmap}

        # 各完了周のSQを、期待物理レーン＋周内時間窓（予測最近）で付与
        for k in range(1, comp + 1):
            lo = sgmap[k - 1][0]
            hi = sgmap[k][0]
            lap_len = max(hi - lo, 1)
            for g in course.gates:
                if g.kind != "SQ":
                    continue
                phys = expected_lane(s, k, g.rot_to_gate, rot, lanes)
                pred = lo + (g.index / n_gates) * lap_len
                got = claim(phys, g.index, lo, hi, pred)
                if got is not None:
                    gates_map.setdefault(g.index, {})[k] = got

        machine_passings[s] = gates_map

    return machine_passings


def build_race(
    layout: list[LayoutElement],
    events: list[PassEvent],
    target_laps: int,
    green_t_us: int | None = None,
    heat_id: int | None = None,
    lanes: int = LANES,
) -> RaceResult:
    """通過イベント列からレース結果を組み立てる（理想状態版）。

    手順:
      1. レイアウトから CourseModel（ゲート順・累積ずれ）を作る。
      2. green_t_us の有無で F1式/走行式を判別（DA4）。
      3. イベントを (物理レーン, ゲート) ごとに時刻順で並べる。
         同じ (レーン, ゲート) の n 回目が n 周目の通過。
      4. 各通過について、rotation で「どのスタートレーンのマシンか」を同定。
      5. スタートレーンごとに、S/G通過列からラップ・合計を、
         ゲート列からセクターを組み上げる。

    ⚠ 理想状態版の前提: 各 (レーン, ゲート) の通過回数が規定通りで欠測なし。
    """
    course = build_course(layout)
    n2g = _node_to_gate(course)
    mode: StartMode = "f1" if green_t_us is not None else "run"

    # S/Gゲートを特定（レイアウト先頭のSGゲート・index=0想定だが厳密に探す）
    sg_gates = [g for g in course.gates if g.kind == "SG"]
    if len(sg_gates) != 1:
        raise ValueError("layout must contain exactly one S/G gate")
    sg_gate = sg_gates[0]

    result = RaceResult(
        heat_id=heat_id, mode=mode, target_laps=target_laps
    )

    # --- (物理レーン, ゲートindex) ごとに時刻順で並べ、通過順(=何回目)を付与 ---
    # key: (lane, gate_index) -> list[(t_us, event, gate)]
    grouped: dict[tuple[int, int], list[tuple[int, PassEvent, Gate]]] = {}
    for ev in events:
        # E3(24.36/B1)：q=3(未同期)イベントはタイム・速度ごと剥奪＝欠測扱い。
        # E4(24.36/A8)：逆走(B→Aの順＝t_us_b<t_us でマイナス速度)イベントも剥奪。
        #   剥奪した通過は grouped に入れない＝そのゲート通過が欠測になる。位相ズレは
        #   E1(欠測検知・2-a)が「⚠要確認」で拾う（24.1：補完せず事実を示す）。
        #   逆走判定は t_us_b があるときのみ（None/0は片ビーム欠=A2で逆走ではない）。
        if ev.quality == 3:
            continue
        if ev.t_us_b is not None and ev.t_us_b != 0 and ev.t_us_b < ev.t_us:
            continue
        gate = n2g.get(ev.node_id)
        if gate is None:
            # レイアウトに無いノード＝予定外（理想状態版では発生しない想定）
            # 捨てずに無視だけする（R5で扱う）
            continue
        key = (ev.lane, gate.index)
        grouped.setdefault(key, []).append((ev.t_us, ev, gate))

    for lst in grouped.values():
        lst.sort(key=lambda x: x[0])  # 時刻昇順

    # --- 各通過を同定し、スタートレーンごとに仕分ける ---
    # machine_passings[start_lane][gate_index] = { lap: (t_us, event) }
    machine_passings: dict[int, dict[int, dict[int, tuple[int, PassEvent]]]] = {}

    if _events_complete(grouped, course, sg_gate, target_laps, lanes):
        # 完全データ（全車完走・欠測なし）＝通常のレース。
        #   各 (レーン, ゲート) の n 回目が n 周目、という出現順の前提が厳密に成り立つので
        #   現行ロジックのまま同定する（本番の通常レースは一切挙動を変えない・無回帰）。
        for (phys_lane, gate_index), lst in grouped.items():
            gate = n2g_index(course, gate_index)
            for occurrence, (t_us, ev, _g) in enumerate(lst):
                # S/Gは passing=0 がスタート、passing=k が k周目完了。
                # セクションゲートは occurrence 0 が 1周目。
                if gate.kind == "SG":
                    passing = occurrence           # 0=スタート, 1..=周回完了
                    # start_lane 同定（S/Gは rot_to_gate=0・passing周ぶんずれる）
                    start_lane = _identify_from_sg(phys_lane, passing, course.rot_total, lanes)
                    lap_key = passing              # 0=スタート打刻
                else:
                    lap = occurrence + 1           # 1周目=1
                    start_lane = identify_start_lane(
                        phys_lane, lap, gate.rot_to_gate, course.rot_total, lanes
                    )
                    lap_key = lap

                machine_passings.setdefault(start_lane, {}) \
                                .setdefault(gate_index, {})[lap_key] = (t_us, ev)
    else:
        # 欠測あり（CO/DNS/スキップ/剥奪）＝出現順の前提が崩れる。
        #   マシンごとに自ペースで次周を同定する再構成に切り替える（横取りを防ぐ）。
        #   完全データ時の挙動には一切影響しない。
        machine_passings = _reconstruct_incomplete(
            grouped, course, sg_gate, target_laps, lanes, green_t_us, mode
        )

    # --- スタートレーンごとに結果を組む ---
    for start_lane, gates_map in machine_passings.items():
        mres = MachineResult(start_lane=start_lane)

        sg_map = gates_map.get(sg_gate.index, {})
        # スタート時刻 t0: F1式なら緑、走行式ならS/Gのpassing=0
        if mode == "f1":
            t0 = green_t_us
        else:
            t0 = sg_map.get(0, (None, None))[0]

        # ラップ: S/Gの passing=1..target_laps を使う
        prev_t = t0
        for lap in range(1, target_laps + 1):
            cur = sg_map.get(lap)
            if cur is None or prev_t is None:
                break  # 欠測（理想状態版では起きない）
            cur_t = cur[0]
            lap_time = cur_t - prev_t
            lapres = LapResult(lap=lap, lap_time_us=lap_time)

            # セクター: この周の各ゲート通過時刻の差
            lapres.sectors = _build_sectors(course, gates_map, lap, sg_gate, prev_t, cur_t)

            mres.laps.append(lapres)
            prev_t = cur_t
            mres.completed_laps = lap

        # 合計: t0 → 最終ラップ完了
        if mode == "f1":
            final = sg_map.get(target_laps)
            if final is not None and t0 is not None and mres.completed_laps == target_laps:
                mres.total_time_us = final[0] - t0
            # G1(24.53)：フライング検知。F1式で1周目S/G通過が緑(t0)より前なら
            #   その車はマイナスF1タイム＝フライング。total符号ではなく1周目通過で判定
            #   （完走してtotalが正に戻るケースもあるため）。判断はここ(サーバー)に集約・24.1。
            first = sg_map.get(1)
            if first is not None and t0 is not None and first[0] < t0:
                mres.jump_start = True
        else:
            final = sg_map.get(target_laps)
            if final is not None and t0 is not None and mres.completed_laps == target_laps:
                mres.total_time_us = final[0] - t0

        # E5(24.39)：CO→DNF。規定周回に達しなかった車はDNFとして表示する。
        #   COした車はコース上で止まり、以降そのレーンから通過イベントが来なくなる。
        #   沈黙検知は作らず（D3/24.26）、運営が灰ボタンで手動送信する運用のため、
        #   結果は completed_laps < target_laps の形で届く。これをDNFと判定する。
        #   途中経過(laps・sectors・completed_laps)はそのまま残し、表示できる範囲で
        #   見せる（24.39・TFT側24.4と一貫）。total_time_us は未完了で None のまま。
        if mres.completed_laps < target_laps:
            mres.dnf = True

        # E1(24.37)：通過回数の整合性チェック（欠測検知）。補完はしない・24.1。
        #   completed_laps 周まで到達した車について、各SQゲートがその全周ぶんの通過を
        #   持っているかを確認する。1つでも飛んでいれば欠測ありとして missing=True。
        #   末尾の周から欠けるCO(E5)は completed_laps 自体が縮むので誤検知しない。
        #   剥奪(E3/E4)で中間ゲートが欠けたケースもここで拾える。
        if mres.completed_laps >= 1:
            for g in course.gates:
                if g.kind != "SQ":
                    continue
                gmap = gates_map.get(g.index, {})
                # 1..completed_laps の各周でこのSQの通過があるか
                for lap in range(1, mres.completed_laps + 1):
                    if lap not in gmap:
                        mres.missing = True
                        break
                if mres.missing:
                    break

        result.machines[start_lane] = mres

    return result


def _identify_from_sg(phys_lane: int, passing: int, rot_total: int, lanes: int) -> int:
    """S/Gでの逆算。lane = (start-1 + passing*rot_total) mod lanes + 1 の逆。"""
    return (phys_lane - 1 - passing * rot_total) % lanes + 1


def n2g_index(course: CourseModel, gate_index: int) -> Gate:
    """gate_index から Gate を引く。"""
    for g in course.gates:
        if g.index == gate_index:
            return g
    raise KeyError(gate_index)


def _build_sectors(
    course: CourseModel,
    gates_map: dict[int, dict[int, tuple[int, PassEvent]]],
    lap: int,
    sg_gate: Gate,
    lap_start_t: int,
    lap_end_t: int,
) -> list[SectorTime]:
    """1周分のセクター（S/G → G1 → ... → G_last → S/G）を組む。

    その周に、各ゲートを通過した時刻を並べ、隣接差をセクターとする。
    理想状態版では全ゲートを順に通っている前提。
    """
    # この周に通ったゲートを、通過順（index順）に、時刻付きで集める
    # S/G(周回開始) は lap_start_t、S/G(周回完了) は lap_end_t を端点にする
    points: list[tuple[int, int, int | None]] = []  # (gate_index, t_us, event_id)

    # 周回開始の起点＝前のS/G通過（or 緑）: gate_index を S/G として端点に置く
    # 起点の event_id は前周の到達点なのでここでは None（線の始点は除外対象にしない）
    points.append((sg_gate.index, lap_start_t, None))

    # 中間のセクションゲート（index順）: この周(lap)の通過を拾う
    for g in course.gates:
        if g.kind == "SG":
            continue
        gm = gates_map.get(g.index, {})
        cur = gm.get(lap)
        if cur is not None:
            _ev = cur[1]
            points.append((g.index, cur[0], getattr(_ev, "id", None)))

    # 周回完了のS/G（この周の完了イベント）
    sg_map = gates_map.get(sg_gate.index, {})
    sg_cur = sg_map.get(lap)
    sg_ev_id = getattr(sg_cur[1], "id", None) if sg_cur is not None else None
    points.append((sg_gate.index, lap_end_t, sg_ev_id))

    # 時刻順に並べ（通常は既に順序通り）、隣接差をセクターに
    points.sort(key=lambda x: x[1])
    sectors: list[SectorTime] = []
    for a, b in zip(points, points[1:]):
        sectors.append(SectorTime(
            from_gate_index=a[0], to_gate_index=b[0], dt_us=b[1] - a[1],
            to_event_id=b[2],
        ))
    return sectors
