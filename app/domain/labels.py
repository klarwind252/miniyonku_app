"""レース属性の表示ラベル定義（旧 routers/tournaments.py から移設・無変更）。"""

STATUS_LABELS = {
    "prepare": "準備中",
    "qualifying": "予選中",
    "final": "決勝中",
    "finished": "終了",
}

TIME_SLOT_LABELS = {
    "day": "デイレース",
    "night": "ナイトレース",
    "extended": "延長",
    "free": "フリーワード",
}

REGULATION_LABELS = {
    "open": "オープンクラス",
    "junior": "ジュニアクラス",
    "stock": "ストッククラス",
    "stock_junior": "ストッククラス（Jr.の部）",
    "bmax": "B-MAX",
    "gt_advance": "GT-Advance",
    "scratch": "巣組",
    "normal_motor": "ノーマルモーター限定",
    "tune_motor": "チューンモーター限定",
    "hyper_dash": "ハイパーダッシュ限定",
    "single_axis": "片軸限定",
    "single_axis_junior": "片軸限定（Jr.の部）",
}

QUALIFYING_LABELS = {
    "none": "なし（即決勝トーナメント）",
    "none_roundrobin": "なし（即決勝総当たり）",
    "heat_tournament": "ヒート（トーナメント）",
    "heat_tournament_garappa": "ヒート（トーナメント）[がらっぱ堂]",
    "heat_roundrobin": "ヒート（総当たり）",
    "point": "ポイント",
    "roundrobin": "総当たり",
    "order": "並び順（ポイント制）",
    "order_winner": "並び順（勝ち抜け）",
}