"""
ローテーション同定（レーンチェンジャーによるレーン移動の計算）

14章 DA7 / DA8 の実装。DB・ネットワークに依存しない純粋関数のみ。
v6.0c のクリーンアーキテクチャの domain 層に置く（テスト対象）。

用語:
  lane            物理レーン番号。1..LANES（既定3）。
  start_lane      そのマシンがスタートしたレーン（1..LANES）。
  LC              レーンチェンジ。1つ通るごとにレーンが +1 ずれる（循環）。
  layout          コースを通過順に並べたもの。S/G・SQ(セクションゲート)・LC の列。

設計の芯（14章 DA7）:
  あるゲートを、あるスタートレーンのマシンが k 周目に通過するときのレーンは
    lane = ((start_lane-1) + (k-1)*rot_total + rot_to_gate) mod LANES + 1
  ここで
    rot_total    = 1周で通るLCの数（1周あたりのずれ）
    rot_to_gate  = S/G からそのゲートまでに通るLCの数（累積ずれ）
  逆に解けば「記録レーンから、どのスタートレーンのマシンか」を同定できる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

LANES = 3  # ミニ四駆3レーン固定。将来変えるならここだけ。


# ---------------------------------------------------------------------------
# レイアウト（コースの地図）
# ---------------------------------------------------------------------------

# レイアウト要素の種別
ElementKind = Literal["SG", "SQ", "LC"]


@dataclass(frozen=True)
class LayoutElement:
    """コースレイアウトの1要素（通過順に並ぶ）。

    kind: "SG"(スタート/ゴール) / "SQ"(セクションゲート) / "LC"(レーンチェンジ)
    node_id: SG/SQ のとき、割り当てられた実機のノードID（LCは None）
    """

    kind: ElementKind
    node_id: int | None = None


@dataclass(frozen=True)
class Gate:
    """レイアウトから導出した「ゲート」1つ分の同定用パラメータ。

    index      レイアウト上のゲートの並び順（0=S/G, 1=最初のSQ, ...）
    kind       "SG" or "SQ"
    node_id    実機ノードID
    rot_to_gate  S/G からこのゲートまでの累積LC数（= このゲートでの追加ずれ）
    """

    index: int
    kind: ElementKind
    node_id: int | None
    rot_to_gate: int


@dataclass(frozen=True)
class CourseModel:
    """レイアウトを同定に使える形に前処理したもの。

    gates      通過順のゲート列（SGとSQのみ・LCは畳み込み済み）
    rot_total  1周あたりのLC数（= 1周のずれ）
    lc_count   1周のLC総数（rot_total と同じ。可読性のため別名で保持）
    """

    gates: tuple[Gate, ...]
    rot_total: int
    lc_count: int


def build_course(layout: list[LayoutElement]) -> CourseModel:
    """通過順レイアウトから CourseModel を構築する。

    先頭から走査し、LC を通るたびに累積ずれ(+1)を増やす。
    SG/SQ に到達した時点の累積ずれを、そのゲートの rot_to_gate として記録する。

    注意: これはバリデーション後に呼ぶことを想定するが、単体でも壊れない。
    """
    gates: list[Gate] = []
    rot = 0
    gate_index = 0
    for el in layout:
        if el.kind == "LC":
            rot += 1
        else:  # SG or SQ
            gates.append(
                Gate(
                    index=gate_index,
                    kind=el.kind,
                    node_id=el.node_id,
                    rot_to_gate=rot,
                )
            )
            gate_index += 1
    lc_total = rot  # 走査し終えた時点の累積 = 1周のLC総数
    return CourseModel(gates=tuple(gates), rot_total=lc_total, lc_count=lc_total)


# ---------------------------------------------------------------------------
# 期待レーンの計算（順方向）
# ---------------------------------------------------------------------------

def expected_lane(
    start_lane: int,
    lap: int,
    rot_to_gate: int,
    rot_total: int,
    lanes: int = LANES,
) -> int:
    """スタートレーンのマシンが、lap 周目に、あるゲートを通過するときのレーン。

    start_lane   1..lanes
    lap          1 以上（1周目=1）
    rot_to_gate  S/Gからそのゲートまでの累積LC数
    rot_total    1周あたりのLC数
    戻り値       1..lanes
    """
    if not (1 <= start_lane <= lanes):
        raise ValueError(f"start_lane must be 1..{lanes}, got {start_lane}")
    if lap < 1:
        raise ValueError(f"lap must be >= 1, got {lap}")
    shift = (lap - 1) * rot_total + rot_to_gate
    return (start_lane - 1 + shift) % lanes + 1


def expected_sg_lane(
    start_lane: int,
    passing: int,
    rot_total: int,
    lanes: int = LANES,
) -> int:
    """S/Gゲートでの期待レーン。S/Gは通過インデックス passing で数える。

    S/G上流スタート（14章）なので通過回数は N+1 回:
      passing=0 がスタート打刻（そのレーン=start_lane）
      passing=k が k周目の完了
    lane = ((start_lane-1) + passing*rot_total) mod lanes + 1
    """
    if not (1 <= start_lane <= lanes):
        raise ValueError(f"start_lane must be 1..{lanes}, got {start_lane}")
    if passing < 0:
        raise ValueError(f"passing must be >= 0, got {passing}")
    return (start_lane - 1 + passing * rot_total) % lanes + 1


# ---------------------------------------------------------------------------
# スタートレーンの同定（逆方向）
# ---------------------------------------------------------------------------

def identify_start_lane(
    observed_lane: int,
    lap: int,
    rot_to_gate: int,
    rot_total: int,
    lanes: int = LANES,
) -> int:
    """あるゲートで observed_lane を lap 周目に記録したマシンの、スタートレーンを逆算。

    expected_lane の逆関数。
      start_lane = ((observed_lane-1) - (lap-1)*rot_total - rot_to_gate) mod lanes + 1
    """
    if not (1 <= observed_lane <= lanes):
        raise ValueError(f"observed_lane must be 1..{lanes}, got {observed_lane}")
    if lap < 1:
        raise ValueError(f"lap must be >= 1, got {lap}")
    shift = (lap - 1) * rot_total + rot_to_gate
    return (observed_lane - 1 - shift) % lanes + 1


# ---------------------------------------------------------------------------
# コースレイアウト確定時バリデーション（14章 DA8）
# ---------------------------------------------------------------------------

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ValidationIssue:
    """バリデーション結果の1項目。

    severity  "error"（確定不可）/ "warning"（OKで確定可）
    code      機械可読なコード（UI側の分岐用）
    message   人間向けの説明（なぜ珍しいか＋用途を含める）
    """

    severity: Severity
    code: str
    message: str


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def can_commit(self) -> bool:
        """エラーが1つもなければ確定可能（警告はOKで通せる）。"""
        return len(self.errors) == 0


def validate_layout(
    layout: list[LayoutElement],
    max_sq: int = 6,
    lanes: int = LANES,
) -> ValidationResult:
    """コースレイアウトを確定時にチェックする（14章 DA8）。

    エラー（確定不可）:
      - S/Gが1個でない
      - LCが3の倍数（1周で元レーンに戻る）
      - 同じ機体番号が複数位置に割当てられている
      - SG/SQ枠に機器が未割当（node_id が None）
      - SQが max_sq を超える
    警告（OKで確定可）:
      - LCが0個（全周同一レーン。検証用途に使える）
      - SQが5個以上（TFTにセクタータイムを表示できない）
    """
    result = ValidationResult()

    sg_count = sum(1 for e in layout if e.kind == "SG")
    sq_count = sum(1 for e in layout if e.kind == "SQ")
    lc_count = sum(1 for e in layout if e.kind == "LC")

    # --- S/Gは必ず1個 ---
    if sg_count == 0:
        result.issues.append(ValidationIssue(
            "error", "sg_missing",
            "スタートゲートがありません。必ず1個配置してください。",
        ))
    elif sg_count > 1:
        result.issues.append(ValidationIssue(
            "error", "sg_multiple",
            f"スタートゲートが{sg_count}個あります。スタートゲートは1個だけです。",
        ))

    # --- SQの上限 ---
    if sq_count > max_sq:
        result.issues.append(ValidationIssue(
            "error", "sq_over_max",
            f"セクションゲートが{sq_count}個あります。最大{max_sq}個までです。",
        ))

    # --- LCが3の倍数（0を除く）はエラー ---
    if lc_count > 0 and lc_count % lanes == 0:
        result.issues.append(ValidationIssue(
            "error", "lc_multiple_of_lanes",
            f"レーンチェンジが{lc_count}個（{lanes}の倍数）です。"
            f"1周でマシンが元のレーンに戻り、公平な順位がつきません。数を変えてください。",
        ))

    # --- LCが0個は警告（検証用途に使える） ---
    if lc_count == 0:
        result.issues.append(ValidationIssue(
            "warning", "lc_zero",
            "レーンチェンジがありません。マシンは全周、同じレーンを走ります。"
            "通常のレースでは公平な順位がつきませんが、"
            "同一レーンでのセンサー検証などにはこの設定を使えます。",
        ))

    # --- SQが5個以上は警告（TFTにセクター表示不可） ---
    if sq_count >= 5:
        result.issues.append(ValidationIssue(
            "warning", "sq_no_tft_sector",
            f"セクションゲートが{sq_count}個あります。"
            "GW本体の画面にはセクタータイムを表示できません（ラップと合計のみ）。"
            "詳細はアプリで確認できます。",
        ))

    # --- ゲート枠の未割当・機体番号の重複 ---
    seen_nodes: dict[int, int] = {}  # node_id -> 何番目のゲートか
    gate_pos = 0
    for e in layout:
        if e.kind in ("SG", "SQ"):
            if e.node_id is None:
                result.issues.append(ValidationIssue(
                    "error", "gate_unassigned",
                    f"{gate_pos + 1}番目のゲートに機器が割り当てられていません。"
                    "機器台帳から選んでください。",
                ))
            else:
                if e.node_id in seen_nodes:
                    result.issues.append(ValidationIssue(
                        "error", "node_duplicated",
                        f"機体番号 {e.node_id} が複数の位置に割り当てられています。"
                        "1つの基板は1箇所にしか置けません。",
                    ))
                else:
                    seen_nodes[e.node_id] = gate_pos
            gate_pos += 1

    return result
