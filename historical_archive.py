"""Read the official daily result download without using race outcomes as inputs."""
import io
import re
import unicodedata

import lhafile

ARCHIVE_BASE = 'https://www1.mbrace.or.jp/od2/K/'
MAX_COMPRESSED = 2 * 1024 * 1024
MAX_UNCOMPRESSED = 4 * 1024 * 1024


def archive_url(date):
    return f'{ARCHIVE_BASE}{date[:6]}/k{date[2:]}.lzh'


def decode_archive(content, date):
    if len(content) > MAX_COMPRESSED:
        raise ValueError('公式結果ファイルのサイズが上限を超えました。')
    archive = lhafile.Lhafile(io.BytesIO(content))
    name = f'K{date[2:]}.TXT'
    files = [entry for entry in archive.infolist() if entry.filename.upper() == name]
    if len(files) != 1 or files[0].file_size > MAX_UNCOMPRESSED:
        raise ValueError('公式結果ファイルの名前・サイズを確認できませんでした。')
    return archive.read(files[0].filename).decode('cp932')


def parse_daily_results(text, date):
    """Only labels/payouts and refund flags are retained; race ST is never an input."""
    text = unicodedata.normalize('NFKC', text)
    if not text.startswith('STARTK') or not text.rstrip().endswith('FINALK'):
        raise ValueError('公式結果ファイルの形式が不正です。')
    dates = {f'{int(y):04d}{int(m):02d}{int(d):02d}'
             for y, m, d in re.findall(r'(\d{4})/\s*(\d{1,2})/\s*(\d{1,2})', text)}
    if dates != {date}:
        raise ValueError('公式結果ファイルの日付が一致しません。')
    results = []
    for venue in re.finditer(r'(?m)^(\d{2})KBGN\s*\n([\s\S]*?)^\1KEND', text):
        jcd, body = venue.groups()
        headers = list(re.finditer(r'(?m)^\s*(\d{1,2})R\s+[^\n]*H\d{4}m[^\n]*$', body))
        for index, header in enumerate(headers):
            block = body[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(body)]
            payouts = set(re.findall(r'3連単\s+([1-6])-([1-6])-([1-6])\s+(\d+)', block))
            finishers = re.findall(r'(?m)^\s{2}(\d{2}|F|L|K|S\d)\s+([1-6])\s+\d{4}\s', block)
            record = {'stadium': jcd, 'race': int(header.group(1)), 'combo': '', 'payout': 0,
                      'excluded': 'invalid_result'}
            if len(payouts) == 1:
                a, b, c, payout = next(iter(payouts))
                combo = a + b + c
                if len(set(combo)) == 3 and int(payout) > 0:
                    record.update(combo=combo, payout=int(payout))
                    if len(finishers) == 6 and {boat for _, boat in finishers} == set('123456'):
                        # Refund accounting and dead heats need a separate settlement policy.
                        if any(rank in ('F', 'L', 'K') for rank, _ in finishers):
                            record['excluded'] = 'refund'
                        elif any(sum(rank == place for rank, _ in finishers) != 1
                                 for place in ('01', '02', '03')):
                            record['excluded'] = 'dead_heat'
                        else:
                            actual = ''.join(next(boat for rank, boat in finishers if rank == place)
                                             for place in ('01', '02', '03'))
                            if actual == combo:
                                record['excluded'] = None
            results.append(record)
    if not results:
        raise ValueError('公式結果ファイルからレースを読み取れませんでした。')
    keys = [(row['stadium'], row['race']) for row in results]
    if len(keys) != len(set(keys)):
        raise ValueError('公式結果ファイルに重複レースがあります。')
    return sorted(results, key=lambda row: (row['stadium'], row['race']))


def previous_history(days, date, jcd):
    from datetime import datetime, timedelta
    start = (datetime.strptime(date, '%Y%m%d') - timedelta(days=3)).strftime('%Y%m%d')
    records = [row for day in days if start <= day['date'] < date
               for row in day['results'] if row['stadium'] == jcd and row.get('combo')]
    races = len(records)
    combos = {}
    for row in records:
        combos[row['combo']] = combos.get(row['combo'], 0) + 1
    payouts = [row['payout'] for row in records if row.get('payout')]
    return {'days': 3, 'dates': sum(any(row['stadium'] == jcd for row in day['results'])
                                  for day in days if start <= day['date'] < date),
            'races': races,
            'first_win_rate': {str(i): round(sum(row['combo'][0] == str(i) for row in records)
                                           / races * 100, 2) if races else 0 for i in range(1, 7)},
            'top_combos': sorted(combos.items(), key=lambda item: item[1], reverse=True)[:10],
            'avg_payout': round(sum(payouts) / len(payouts)) if payouts else None,
            'max_payout': max(payouts) if payouts else None}
