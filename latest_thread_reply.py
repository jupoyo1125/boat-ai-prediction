"""Select the newest confirmed public Threads post for an adult's LINE request."""
from datetime import datetime
import re


def choose_reply(rows, now, min_lead):
    date = now.strftime('%Y%m%d')
    key_pattern = rf'auto:{date}:(?:0[1-9]|1[0-9]|2[0-4]):(?:0[1-9]|1[0-2])'
    candidates = []
    for key, sent, deadline, body, link in rows:
        if not re.fullmatch(key_pattern, key):
            continue
        try:
            stamp = datetime.fromisoformat(sent)
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo and stamp <= now and stamp.astimezone(now.tzinfo).date() == now.date():
            candidates.append((stamp, key, deadline, body, link))
    if not candidates:
        return '本日はまだ公開を確認できた投稿がありません。投稿後に「買い目」と送ってください。', None
    _, _, deadline, body, link = max(candidates, key=lambda row: row[:2])
    try:
        cutoff = datetime.fromisoformat(deadline)
        if not cutoff.tzinfo or cutoff.astimezone(now.tzinfo).date() != now.date():
            raise ValueError()
    except (TypeError, ValueError):
        return '直近の投稿の締切を確認できないため、返信を見送ります。', None
    if now >= cutoff - min_lead:
        return '直近の投稿は締切直前・締切後のため配信終了です。次の投稿をお待ちください。', None
    if not isinstance(body, str) or not body.strip():
        return '直近の投稿本文を確認できません。', None
    text = body.strip()
    if isinstance(link, str) and re.fullmatch(
            r'https://(?:www\.)?threads\.(?:net|com)/@ton123kyotei456/post/[A-Za-z0-9_-]+/?', link):
        text += '\n\n' + link
    if len(text.encode('utf-16-le')) // 2 > 5000:
        return '直近の投稿は本文が長いためLINEで表示できません。', None
    return text, cutoff
