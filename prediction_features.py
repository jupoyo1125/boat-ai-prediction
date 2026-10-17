"""The four prediction inputs, with explicit point-in-time provenance."""
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from racer_course_history import racer_rates

FEATURE_SCHEMA = 'racer-course-year-v2'
FEATURE_KEYS = ('motor', 'exhibition', 'meeting', 'course')


def meeting_results(html, date, race, jcd):
    """Read only this meeting's finishes known before the target race."""
    soup = BeautifulSoup(unicodedata.normalize('NFKC', html), 'html.parser')
    result = {}
    for body in soup.select('tbody.is-fs12'):
        rows = body.find_all('tr', recursive=False)
        if len(rows) != 4:
            continue
        cells = rows[0].find_all(['td', 'th'], recursive=False)
        if not cells or cells[0].get_text(strip=True) not in '123456':
            continue
        boat = int(cells[0].get_text(strip=True))
        link = body.select_one('a[href*="toban="]')
        racer = parse_qs(urlsplit(link.get('href', '')).query).get('toban', [None])[0] if link else None
        finishes = []
        seen = set()
        for anchor in rows[3].select('a[href*="raceresult?"]'):
            query = parse_qs(urlsplit(anchor.get('href', '')).query)
            day = query.get('hd', [''])[0]
            number = query.get('rno', [''])[0]
            if (not re.fullmatch(r'\d{8}', day) or not number.isdigit()
                    or query.get('jcd', [''])[0] != jcd
                    or (day, int(number)) >= (date, int(race))):
                continue
            key = (day, int(number))
            finish = anchor.get_text(strip=True)
            if key in seen or not finish or finish.startswith(('欠', '不')):
                continue
            seen.add(key)
            if re.fullmatch(r'[1-6]', finish):
                finishes.append({'date': day, 'race': int(number), 'place': int(finish)})
            elif finish.startswith(('F', 'L', '失', '転', '落', '沈', '妨', 'エ')):
                finishes.append({'date': day, 'race': int(number), 'place': None})
        result[boat] = {'racer_id': racer, 'finishes': finishes}
    return result


def relative_score(values, value, reverse=False):
    known = [v for v in values if isinstance(v, (int, float))]
    if value is None or len(known) < 2 or max(known) == min(known):
        return 50.0
    score = (value - min(known)) / (max(known) - min(known)) * 100
    return round(100 - score if reverse else score, 2)


def analyze_features(raw, fixed, before, snapshot, model, meeting):
    rows = []
    for boat in range(1, 7):
        cells = raw.get(boat, [])
        identity = cells[2] if len(cells) > 2 else ''
        match = re.match(r'\s*(\d{4,5})\s*/', identity)
        data = meeting.get(boat, {})
        racer = data.get('racer_id') or (match.group(1) if match else None)
        motor = re.findall(r'\d+(?:\.\d+)?', cells[6] if len(cells) > 6 else '')
        finishes = data.get('finishes', [])
        points = [6 - f['place'] if f['place'] else 0 for f in finishes]
        meeting_score = round(sum(points) / len(points) / 5 * 100, 2) if points else None
        course = before.get('entry_course', {}).get(boat)
        rates = (racer_rates(snapshot, racer, course) if racer and course else
                 {'starts': 0, 'first': None, 'second': None, 'third': None})
        rows.append({'boat': boat, 'name': re.sub(r'^\d+\s*/\s*[A-Z]\d+\s*', '', identity).strip(),
                     'racer_id': racer, 'motor': float(motor[1]) if len(motor) > 1 else None,
                     'exhibition': before['exhibition'].get(boat),
                     'meeting': meeting_score, 'meeting_count': len(finishes), 'meeting_finishes': finishes,
                     'entry_course': course, 'course_rates': rates,
                     'course_start_date': snapshot['start_date'], 'course_end_date': snapshot['end_date'],
                     'feature_schema': FEATURE_SCHEMA})
    for key, reverse in [('motor', False), ('exhibition', True), ('meeting', False)]:
        values = [r[key] for r in rows]
        for row in rows:
            row[key + 's'] = relative_score(values, row[key], reverse)
    for place, score_key in [('first', 'score'), ('second', 'second_score'), ('third', 'third_score')]:
        values = [row['course_rates'][place] for row in rows]
        for row in rows:
            course_score = relative_score(values, row['course_rates'][place])
            row['course_' + place + 's'] = course_score
            row[score_key] = round(sum(row[key + 's'] * model['weights'][key]
                                       for key in ('motor', 'exhibition', 'meeting'))
                                   + course_score * model['weights']['course'], 2)
    for row in rows:
        row['courses'] = row['course_firsts']
        if fixed != 'none' and row['boat'] == int(fixed):
            row['score'] += 8
    return rows
