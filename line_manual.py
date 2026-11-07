"""Record a completed Official Account Manager send without sending a message."""
import hmac
import os
from datetime import datetime
from flask import make_response, render_template_string, request
from line_bot import JST, timestamp

PAGE = """<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LINE手動配信の記録</title>
<style>body{max-width:36em;margin:2em auto;padding:0 1em;font:1em/1.6 sans-serif}
label{display:block;margin:1em 0}textarea{width:100%}input,textarea,button{font:inherit}</style>
<h1>LINE手動配信の記録</h1><p>LINE公式管理画面で配信した後に登録してください。ここからLINEには送信しません。</p>
<p role="status">{{ notice }}</p><form method="post">
<label>配信日（日本時間・当日分のみ） <input type="date" name="date" value="{{ day }}" required></label>
<label>実際に配信した時刻 <input type="time" step="1" name="sent" required></label>
<label>レースの締切時刻 <input type="time" name="deadline" required></label>
<label>配信した本文<textarea name="body" maxlength="5000" rows="10" required></textarea></label>
<label>管理用パスワード <input type="password" name="password" required></label>
<button>配信済みとして記録</button></form></html>"""


def register_manual(app, store, clock):
    @app.route('/admin/line/manual', methods=['GET', 'POST'])
    def manual_line():
        now = clock().astimezone(JST)
        def page(msg='', code=200):
            response = make_response(render_template_string(PAGE, notice=msg, day=now.date().isoformat()), code)
            response.headers.update({'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                                     'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"})
            return response
        if request.method == 'GET':
            return page()
        if (request.mimetype != 'application/x-www-form-urlencoded' or
                request.content_length is None or request.content_length > 100000):
            return page('入力形式または文字数を確認してください。', 400)
        key = os.environ.get('BOAT_LINE_MANUAL_PASSWORD', '')
        if len(key) < 24 or not hmac.compare_digest(key.encode(), request.form.get('password', '').encode()):
            return page('管理用パスワードを確認してください。', 403)
        body = request.form.get('body', '').replace('\r\n', '\n')
        try:
            day = request.form['date']
            sent, end = (datetime.fromisoformat(day + 'T' + request.form[k]).replace(tzinfo=JST)
                         for k in ('sent', 'deadline'))
            if (day != now.date().isoformat() or not sent <= now or end <= sent or not body.strip()
                    or '\x00' in body or len(body.encode('utf-16-le')) // 2 > 5000):
                raise ValueError
        except (ValueError, KeyError):
            return page('当日の配信日時、配信より後の締切、本文を確認してください。', 400)
        record = dict(sent=sent.isoformat(), deadline=end.isoformat(), body=body)
        def save(state):
            daily = store.day(state, now)
            old = daily.get('manual_line')
            if old and timestamp(old['sent']) > sent:
                return False
            daily['manual_line'] = record
            return True
        if not store.mutate(save):
            return page('これより新しい手動配信がすでに登録されています。', 409)
        return page('記録しました。「買い目」には自動・手動のうち配信時刻が新しい本文を使います。')
