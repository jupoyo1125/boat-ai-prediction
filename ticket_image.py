"""Render immutable, unpurchased forecast slips, with no official logo/barcode."""
import base64
from datetime import datetime
from functools import lru_cache
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

from PIL import Image, ImageDraw, ImageFont
from race_venues import STADIUMS

ORIGIN = 'https://boat-ai-prediction.onrender.com'
GROUPS = (('gachi', 'ガチガチ', 10), ('roman', 'ロマン砲', 10), ('oni', '鬼しぼり', 3))
SIZE = (1200, 1400)
MAX_IMAGE_BYTES = 500_000


def snapshot(row):
    """List exact individual combinations; do not expand a formation."""
    date, race = row['date'], row['race']
    datetime.strptime(date, '%Y%m%d')
    stadium = row.get('stadium')
    if stadium not in STADIUMS or type(race) is not int or not 1 <= race <= 12:
        raise ValueError('invalid ticket scope')
    bets = row.get('bets') or []
    if not bets or any(b.get('category') not in {g[0] for g in GROUPS} for b in bets):
        raise ValueError('invalid ticket categories')
    groups = []
    for key, label, limit in GROUPS:
        selected = [b['bet'] for b in bets if b['category'] == key]
        if len(selected) > limit or any(not isinstance(b, str) for b in selected):
            raise ValueError('invalid ticket count')
        if len(set(selected)) != len(selected):
            raise ValueError('duplicate ticket combination')
        for bet in selected:
            if not re.fullmatch(r'[1-6]-[1-6]-[1-6]', bet) or len(set(bet.split('-'))) != 3:
                raise ValueError('invalid ticket combination')
        groups.append({'label': label, 'bets': sorted(selected)})
    times = {}
    for key in ('prediction_saved_at', 'scheduled_deadline'):
        dt = datetime.fromisoformat(row[key])
        if dt.tzinfo is None or dt.strftime('%Y%m%d') != date:
            raise ValueError('invalid ticket time')
        times[key] = dt.strftime('%H:%M')
    return {'version': 2, 'date': date, 'stadium': stadium, 'venue': STADIUMS[stadium],
            'race': race, 'groups': groups, **times}


@lru_cache(maxsize=16)
def font(size):
    raw = base64.b64decode((Path(__file__).parent / 'assets/ticket-font.ttf.b64').read_bytes())
    return ImageFont.truetype(BytesIO(raw), size)


@lru_cache(maxsize=64)
def _render(serialized, preview):
    item = json.loads(serialized)
    image = Image.new('RGB', SIZE, '#DAD8D1')
    draw = ImageDraw.Draw(image)
    def text(x, y, value, size=28, fill='#22221F', anchor='lt'):
        draw.text((x, y), str(value), font=font(size), fill=fill, anchor=anchor)
    d = item['date']; date_label = f'{d[:4]}/{d[4:6]}/{d[6:]}'
    text(52, 32, f'{item["venue"]} {item["race"]}R｜展示後予想', 47)
    text(1146, 47, '独自サイト予想', 27, anchor='rt')
    text(55, 92, '形式見本・過去の予想（締切済み）' if preview else
         f'{date_label}　保存 {item["prediction_saved_at"]} ／ 締切 {item["scheduled_deadline"]}', 23)
    for index, group in enumerate(item['groups']):
        top, height = ((130, 400), (558, 400), (986, 280))[index]
        draw.rectangle((59, top+7, 1151, top+height+7), fill='#B8B6AE')
        draw.rectangle((49, top, 1141, top+height), fill='#FFFEF4', outline='#B8B6AC', width=2)
        draw.line((302, top+19, 302, top+height-21), fill='#22221F', width=2)
        for x in range(67, 1128, 13):
            draw.line((x, top+height-12, x+5, top+height-12), fill='#CECCC0')
        text(78, top+28, item['venue'], 36)
        text(76, top+83, f'{item["race"]}R', 69)
        text(79, top+170, '3連単', 29)
        text(79, top+214, date_label, 19)
        text(79, top+247 if height>300 else top+241, '予想用・未購入', 22)
        text(332, top+23, group['label'], 39)
        selected = group['bets']
        text(1110, top+31, f'{len(selected)}点', 30, anchor='rt')
        draw.line((331, top+78, 1110, top+78), fill='#22221F', width=2)
        if not selected:
            text(367, top+111, '見送り', 42)
        for number, bet in enumerate(selected):
            col, line = divmod(number, 5 if height>300 else 2)
            x, y = 341+col*386, top+96+line*46
            text(x, y+9, f'{number+1:02d}', 20, '#67665F')
            text(x+50, y, bet, 38)
        text(1110, top+height-49, f'参考：各100円 ／ 合計 {len(selected)*100:,}円', 25, anchor='rt')
    text(55, 1303, '買い目は保存済み予想のまま。表示順のみ整理しています。', 25)
    text(55, 1344, '各100円は参考換算です。実際に購入した舟券ではありません。', 23, '#67665F')
    stream = BytesIO(); image.save(stream, format='PNG', optimize=True)
    raw = stream.getvalue()
    if len(raw)>MAX_IMAGE_BYTES:
        raise ValueError('ticket image too large')
    return raw


def render_row(row, preview=False):
    return _render(json.dumps(snapshot(row), ensure_ascii=False, sort_keys=True), preview)


def freeze(row):
    raw = render_row(row)
    digest = hashlib.sha256(raw).hexdigest()
    scope = row['date'] if row['stadium']=='04' else f'{row["date"]}/{row["stadium"]}'
    return {'format': 'ticket-v1', 'image_sha256': digest,
            'image_data': base64.b64encode(raw).decode('ascii'),
            'image_url': f'{ORIGIN}/api/threads_ticket/{scope}/{row["race"]}/{digest}.png'}


def register_route(app, store_factory):
    """Read only the immutable PNG already queued by the publisher."""
    from flask import abort, Response

    @app.get('/api/threads_ticket/<date>/<int:race>/<digest>.png')
    def legacy_threads_ticket_image(date, race, digest):
        return threads_ticket_image(date, '04', race, digest)

    @app.get('/api/threads_ticket/<date>/<stadium>/<int:race>/<digest>.png')
    def threads_ticket_image(date, stadium, race, digest):
        if (stadium not in STADIUMS or not re.fullmatch(r'\d{8}', date) or not 1<=race<=12
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            abort(404)
        try:
            entry = store_factory().read()['posts'].get(f'auto:{date}:{stadium}:{race:02d}') or {}
        except Exception:
            abort(503)
        if entry.get('format') != 'ticket-v1' or entry.get('image_sha256') != digest:
            abort(404)
        try:
            encoded = entry['image_data']
            if not isinstance(encoded, str) or len(encoded)>MAX_IMAGE_BYTES*2:
                abort(404)
            raw = base64.b64decode(encoded, validate=True)
            if (len(raw)>MAX_IMAGE_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n')
                    or hashlib.sha256(raw).hexdigest()!=digest):
                abort(404)
        except (KeyError, ValueError, TypeError):
            abort(404)
        response = Response(raw, mimetype='image/png')
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.set_etag(digest)
        return response
