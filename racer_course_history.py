"""Point-in-time, one-year racer/course results from official daily archives."""
from collections import defaultdict
from datetime import datetime, timedelta
import hashlib
import re
import unicodedata

from historical_archive import decode_archive


FORMAT_VERSION = 1


def year_window(target_date):
    """The complete calendar year ending before the predicted race's date."""
    target = datetime.strptime(str(target_date), '%Y%m%d').date()
    end = target - timedelta(days=1)
    try:
        anniversary = end.replace(year=end.year - 1)
    except ValueError:
        anniversary = end.replace(year=end.year - 1, day=28)
    start = anniversary + timedelta(days=1)
    return [(start + timedelta(days=i)).strftime('%Y%m%d')
            for i in range((end - start).days + 1)]


def parse_course_results(text, date):
    """Keep racer ID, actual entry course and finish; never infer course from boat."""
    text = unicodedata.normalize('NFKC', text)
    if not text.startswith('STARTK') or not text.rstrip().endswith('FINALK'):
        raise ValueError('コース成績の公式結果ファイルが不完全です。')
    dates = {f'{int(y):04d}{int(m):02d}{int(d):02d}'
             for y, m, d in re.findall(r'(\d{4})/\s*(\d{1,2})/\s*(\d{1,2})', text)}
    if dates != {date}:
        raise ValueError('コース成績の公式結果の日付が一致しません。')
    counts = defaultdict(lambda: [0, 0, 0, 0])
    races, no_course = 0, 0
    race_keys = set()
    venues = list(re.finditer(r'(?m)^(\d{2})KBGN\s*\n([\s\S]*?)^\1KEND', text))
    if not venues:
        raise ValueError('コース成績の開催場データを確認できません。')
    for venue in venues:
        jcd, body = venue.groups()
        headers = list(re.finditer(r'(?m)^\s*(\d{1,2})R\s+[^\n]*H\d{4}m[^\n]*$', body))
        for index, header in enumerate(headers):
            race = int(header.group(1))
            key = (jcd, race)
            if key in race_keys or race not in range(1, 13):
                raise ValueError('コース成績の公式結果に重複・不正レースがあります。')
            race_keys.add(key)
            block = body[header.end():headers[index + 1].start() if index + 1 < len(headers) else len(body)]
            rows = re.findall(r'(?m)^\s{2}(\d{2}|[A-Z]\d?)\s+([1-6])\s+(\d{4,5})\s+([^\n]*)$', block)
            if not rows:
                if any(label in block for label in ('中止', '不成立')):
                    continue
                raise ValueError('コース成績の選手別結果がありません。')
            if len(rows) != 6 or {boat for _, boat, _, _ in rows} != set('123456'):
                raise ValueError('コース成績の選手別結果が重複しています。')
            seen_courses = set()
            for finish, boat, racer, rest in rows:
                # The course column follows exhibition time. Race ST and race
                # time are deliberately discarded after locating that column.
                match = re.search(r'(?:^|\s)\d\.[0-9]{2}\s+([1-6])(?:\s|$)', rest)
                if not match:
                    if finish.isdigit() and 1 <= int(finish) <= 6:
                        raise ValueError('着順のある選手の進入コースを確認できません。')
                    no_course += 1
                    continue
                course = int(match.group(1))
                if course in seen_courses:
                    raise ValueError('同一レースの進入コースが重複しています。')
                seen_courses.add(course)
                row = counts[(int(racer), course)]
                row[0] += 1
                rank = int(finish) if finish.isdigit() else 0
                if 1 <= rank <= 3:
                    row[rank] += 1
            races += 1
    if not race_keys:
        raise ValueError('コース成績のレースを確認できません。')
    return {'format_version': FORMAT_VERSION, 'date': date, 'races': races,
            'starts': sum(row[0] for row in counts.values()), 'no_course': no_course,
            'counts': [[racer, course, *values]
                       for (racer, course), values in sorted(counts.items())]}


def parse_course_archive(content, date):
    result = parse_course_results(decode_archive(content, date), date)
    result['source_sha256'] = hashlib.sha256(content).hexdigest()
    return result


def aggregate_days(days, target_date):
    wanted = year_window(target_date)
    seen = set()
    totals = defaultdict(lambda: [0, 0, 0, 0])
    for day in days:
        date = day['date']
        if date not in wanted or date in seen:
            raise ValueError('選手別コース成績は過去1年分の公式結果が必要です。')
        seen.add(date)
        if day.get('format_version') != FORMAT_VERSION:
            raise ValueError('コース成績の保存形式を確認してください。')
        for racer, course, starts, first, second, third in day['counts']:
            values = totals[(str(racer), int(course))]
            for i, value in enumerate((starts, first, second, third)):
                values[i] += value
    if seen != set(wanted):
        raise ValueError('選手別コース成績は過去1年分の公式結果が必要です。')
    return {'target_date': target_date, 'start_date': wanted[0], 'end_date': wanted[-1],
            'days': len(wanted), 'totals': dict(totals)}


def racer_rates(snapshot, racer, course):
    counts = snapshot['totals'].get((str(racer), int(course)), [0, 0, 0, 0])
    starts = counts[0]
    return {'starts': starts,
            'first': round(counts[1] / starts * 100, 4) if starts else None,
            'second': round(counts[2] / starts * 100, 4) if starts else None,
            'third': round(counts[3] / starts * 100, 4) if starts else None}
