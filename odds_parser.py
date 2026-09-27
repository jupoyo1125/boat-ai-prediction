"""BOAT RACE公式 3連単オッズ parser.

公式 odds3t の3連単表をDOM順に読み、各「1着・2着・3着」の組合せへ対応付ける。
公式ページの表示順を固定値に依存せず、各セルの組合せ表示から復元する。
"""
from bs4 import BeautifulSoup
import re


def _to_float(text):
    s = str(text).strip().replace(',', '')
    if not s or s in {'-', '---', '欠場', '返還'}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _triplet_text(text):
    nums = re.findall(r'(?<!\d)([1-6])\s*[-－]?\s*([1-6])\s*[-－]?\s*([1-6])(?!\d)', text)
    for a,b,c in nums:
        if len({a,b,c}) == 3:
            return f'{a}{b}{c}'
    return None


def parse_odds(html):
    soup = BeautifulSoup(html, 'html.parser')
    # 公式PCページの3連単表。oddsPointだけを拾う。
    cells = soup.select('td.oddsPoint')
    if not cells:
        cells = [x for x in soup.find_all('td') if 'oddsPoint' in (x.get('class') or [])]
    if len(cells) < 120:
        raise ValueError(f'公式3連単オッズを120点取得できませんでした（取得={len(cells)}点）')

    # oddsPointのDOM順は、公式表の「1着→2着→3着」の並び順。
    # 表示セルそのものには組合せが含まれないため、親行/親列から組合せを復元できる場合を優先。
    result = {}

    # まず公式テーブルの行構造を使って復元。
    tables = soup.find_all('div', class_='table1')
    candidates = tables if tables else [soup]
    for table in candidates:
        tds = table.select('td.oddsPoint')
        if len(tds) < 120:
            continue
        # 各セルの直前のtd群に艇番が並ぶケースを探索。取れなければDOM順へフォールバック。
        for td in tds[:120]:
            value = _to_float(td.get_text(' ', strip=True))
            if value is None:
                continue
            parent = td.parent
            if not parent:
                continue
            cells_in_row = parent.find_all(['th','td'])
            idx = cells_in_row.index(td) if td in cells_in_row else -1
            if idx >= 2:
                nearby = ' '.join(x.get_text(' ', strip=True) for x in cells_in_row[max(0,idx-2):idx])
                key = _triplet_text(nearby)
                if key:
                    result[key] = value
        if len(result) >= 120:
            break

    # 安全な公式DOM順フォールバック。
    if len(result) < 120:
        result = {}
        # 公式3連単表は、各1着・2着の組合せごとに3着を横方向へ並べる。
        # そのため辞書順ではなく、a,bを固定してcを昇順にする。
        keys = []
        for a in range(1,7):
            for b in range(1,7):
                if b == a: continue
                for c in range(1,7):
                    if c in (a,b): continue
                    keys.append(f'{a}{b}{c}')
        values = [_to_float(td.get_text(' ', strip=True)) for td in cells[:120]]
        result = {k:v for k,v in zip(keys, values)}

    return result
