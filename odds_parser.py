"""BOAT RACE公式 3連単オッズ parser.

公式3連単オッズ表を20行×6列の行列として取得し、
120通りの「1着-2着-3着」に正確に対応付ける。

重要:
- 全DOMの単純な120個順では割り当てない
- 3連単オッズ表そのものを特定する
- 20行×6列を確認してから変換する
- 120通り揃わない場合はエラーにしてAIへ誤データを渡さない
- 欠場・返還など数値がないセルはNoneとして保持する
"""

from bs4 import BeautifulSoup


EXPECTED_KEYS = {
    f"{a}{b}{c}"
    for a in range(1, 7)
    for b in range(1, 7)
    for c in range(1, 7)
    if len({a, b, c}) == 3
}


def _to_float(text):
    """オッズ文字列をfloatへ変換する。"""
    s = str(text).strip().replace(",", "")

    if not s or s in {"-", "---", "欠場", "返還"}:
        return None

    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _extract_odds_matrix(soup):
    """
    公式3連単オッズ表から
    20行×6列のoddsPointを取得する。
    """

    candidates = []

    # まずtableを探す
    for table in soup.find_all("table"):
        rows = []

        for tr in table.select("tr"):
            cells = tr.select("td.oddsPoint")

            if len(cells) == 6:
                rows.append(cells)

        if len(rows) >= 20:
            candidates.append(rows[:20])

    # 念のため.table1構造にも対応
    if not candidates:
        for container in soup.select(".table1"):
            rows = []

            for tr in container.select("tr"):
                cells = tr.select("td.oddsPoint")

                if len(cells) == 6:
                    rows.append(cells)

            if len(rows) >= 20:
                candidates.append(rows[:20])

    if not candidates:
        raise ValueError(
            "公式3連単オッズ表（20行×6列）を特定できませんでした。"
            "誤ったオッズを返さないため処理を停止します。"
        )

    # 「3連単」を含むテーブルを優先
    best_rows = candidates[0]

    for table in soup.find_all("table"):
        rows = []

        for tr in table.select("tr"):
            cells = tr.select("td.oddsPoint")

            if len(cells) == 6:
                rows.append(cells)

        if (
            len(rows) >= 20
            and "3連単" in table.get_text(" ", strip=True)
        ):
            best_rows = rows[:20]
            break

    matrix = [
        [
            _to_float(cell.get_text(" ", strip=True))
            for cell in row
        ]
        for row in best_rows
    ]

    # 20×6でなければ停止
    if len(matrix) != 20:
        raise ValueError(
            f"3連単オッズの行数が20ではありません: {len(matrix)}"
        )

    if any(len(row) != 6 for row in matrix):
        raise ValueError(
            "3連単オッズの列数が6ではない行があります。"
        )

    return matrix


def _matrix_to_result(matrix):
    """
    20×6の公式オッズ表を
    123 -> オッズ
    の120通りの辞書へ変換する。

    公式表は、
    matrix.T.reshape(-1)
    相当の並びで3連単120通りに対応する。
    """

    if len(matrix) != 20:
        raise ValueError("オッズ行列は20行必要です。")

    if any(len(row) != 6 for row in matrix):
        raise ValueError("オッズ行列は6列必要です。")

    # 公式3連単表の列→行の順番で120値を取得
    values = [
        matrix[row][col]
        for col in range(6)
        for row in range(20)
    ]

    # 3連単120通り
    keys = [
        f"{a}{b}{c}"
        for a in range(1, 7)
        for b in range(1, 7)
        for c in range(1, 7)
        if len({a, b, c}) == 3
    ]

    if len(values) != 120:
        raise ValueError(
            f"オッズ取得数が120ではありません: {len(values)}"
        )

    if len(keys) != 120:
        raise ValueError(
            f"3連単キー生成数が120ではありません: {len(keys)}"
        )

    result = dict(zip(keys, values))

    # 最終チェック
    if len(result) != 120:
        raise ValueError(
            f"3連単辞書が120通りではありません: {len(result)}"
        )

    if set(result.keys()) != EXPECTED_KEYS:
        raise ValueError(
            "3連単120通りのキーが一致しません。"
        )

    return result


def parse_odds(html):
    """
    BOAT RACE公式3連単オッズHTMLを解析する。

    戻り値:

        {
            "123": 7.4,
            "124": 7.9,
            "125": 26.4,
            ...
            "654": 2454.0
        }

    欠場・返還など公式ページで数値がない場合はNone。

    組合せとオッズの対応を保証できない場合は
    ValueErrorを発生させる。
    """

    soup = BeautifulSoup(html, "html.parser")

    # ① 公式3連単表を20×6で取得
    matrix = _extract_odds_matrix(soup)

    # ② 120通りへ変換
    result = _matrix_to_result(matrix)

    # ③ 最終安全チェック
    if len(result) != 120:
        raise ValueError(
            "3連単オッズ120点の完全性チェックに失敗しました。"
        )

    if set(result.keys()) != EXPECTED_KEYS:
        raise ValueError(
            "3連単の組み合わせが不完全です。"
        )

    return result
