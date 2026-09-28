from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

from flask import request, jsonify

import app as base


app = base.app
BASE = base.BASE
STADIUMS = base.STADIUMS
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.6.1 Mobile/15E148 Safari/604.1",
    "Referer": "https://www.boatrace.jp/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


# --------------------------------------------------
# 軽量HTTP取得
# 1回の外部通信を最大8秒に制限
# --------------------------------------------------

def fast_get(url, timeout=8):
    candidates = [url]

    if "www.boatrace.jp" in url:
        candidates.append(
            url.replace(
                "https://www.boatrace.jp",
                "https://boatrace.jp"
            )
        )

    last_error = None

    for target_url in candidates:
        try:
            r = requests.get(
                target_url,
                headers=BROWSER_HEADERS,
                timeout=timeout,
                allow_redirects=True
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text

        except Exception as e:
            last_error = e

    raise last_error


# --------------------------------------------------
# 空の直前情報
# 公式直前情報が取得できない場合でも
# 分析画面を止めないための予備データ
# --------------------------------------------------

def empty_before():
    return {
        "wind": None,
        "wave": None,
        "air": None,
        "water": None,
        "exhibition": {},
        "exhibition_st": {}
    }


# --------------------------------------------------
# /api/analyze
# --------------------------------------------------

def api_analyze_fixed():

    # ------------------------------
    # パラメータ
    # ------------------------------

    date = request.args.get(
        "date",
        datetime.now().strftime("%Y%m%d")
    )

    date = (
        date
        .replace("/", "")
        .replace("-", "")
    )

    jcd = request.args.get(
        "stadium",
        "15"
    )

    try:
        race = int(
            request.args.get(
                "race",
                "9"
            )
        )
    except Exception:
        race = 9

    fixed = request.args.get(
        "fixed",
        "none"
    )

    # ------------------------------
    # URL
    # ------------------------------

    source = (
        f"{BASE}racelist"
        f"?hd={date}"
        f"&jcd={jcd}"
        f"&rno={race:02d}"
    )

    before_source = (
        f"{BASE}beforeinfo"
        f"?hd={date}"
        f"&jcd={jcd}"
        f"&rno={race:02d}"
    )

    odds_source = (
        f"{BASE}odds3t"
        f"?hd={date}"
        f"&jcd={jcd}"
        f"&rno={race:02d}"
    )

    # ------------------------------
    # 3ページを同時取得
    # ------------------------------

    results = {}

    urls = {
        "race": source,
        "before": before_source,
        "odds": odds_source
    }

    try:

        with ThreadPoolExecutor(
            max_workers=3
        ) as executor:

            futures = {
                executor.submit(
                    fast_get,
                    url,
                    8
                ): name

                for name, url in urls.items()
            }

            for future in as_completed(futures):

                name = futures[future]

                try:
                    results[name] = future.result()

                except Exception as e:
                    results[name] = None

    except Exception:
        pass

    # ------------------------------
    # 出走表
    # ------------------------------

    race_html = results.get("race")

    if not race_html:

        return jsonify({
            "ok": False,
            "error": (
                "公式出走表を取得できませんでした。"
            ),
            "source": source,
            "before_source": before_source,
            "odds_source": odds_source
        }), 502

    try:

        boats_raw = base.boats_from(
            race_html
        )

        if not boats_raw:

            raise RuntimeError(
                "出走表を解析できませんでした"
            )

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": (
                "出走表の解析に失敗しました: "
                f"{type(e).__name__}: {e}"
            ),
            "source": source,
            "before_source": before_source,
            "odds_source": odds_source
        }), 502

    # ------------------------------
    # 直前情報
    # ------------------------------

    before = empty_before()

    before_html = results.get(
        "before"
    )

    if before_html:

        try:

            parsed = base.parse_before(
                before_html
            )

            if parsed:
                before = parsed

        except Exception:
            pass

    # ------------------------------
    # 履歴データ
    #
    # 今回はタイムアウト防止のため
    # 外部の過去データ取得をしない
    # ------------------------------

    hist = {
        "days": 0,
        "dates": 0,
        "races": 0,
        "first_win_rate": {
            str(i): 0
            for i in range(1, 7)
        },
        "top_combos": [],
        "avg_payout": None,
        "max_payout": None
    }

    # ------------------------------
    # AIスコア計算
    # ------------------------------

    try:

        boats = base.analyze(
            boats_raw,
            fixed,
            before,
            hist
        )

    except Exception as e:

        return jsonify({
            "ok": False,
            "error": (
                "スコア計算に失敗しました: "
                f"{type(e).__name__}: {e}"
            ),
            "source": source,
            "before_source": before_source,
            "odds_source": odds_source
        }), 502

    # ------------------------------
    # オッズ
    # ------------------------------

    odds = {}

    odds_error = None

    odds_html = results.get(
        "odds"
    )

    if odds_html:

        try:

            odds = base.parse_odds(
                odds_html
            )

        except Exception as e:

            odds_error = (
                f"{type(e).__name__}: {e}"
            )

    # ------------------------------
    # 3連単候補
    # ------------------------------

    try:

        combos = base.build_bets(
            boats,
            odds,
            fixed
        )

    except Exception:

        combos = []

    # ------------------------------
    # スコア順
    # ------------------------------

    boats.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    # ------------------------------
    # 結果
    # ------------------------------

    return jsonify({

        "ok": True,

        "venue": STADIUMS.get(
            jcd,
            jcd
        ),

        "boats": boats,

        "main": boats[0]["boat"],

        "second": (
            boats[1]["boat"]
            if len(boats) > 1
            else boats[0]["boat"]
        ),

        "hole": (
            boats[2]["boat"]
            if len(boats) > 2
            else boats[-1]["boat"]
        ),

        "scenario": base.scenario(
            boats,
            before
        ),

        "bets": combos[:12],

        "history": hist,

        "weather": {
            "wind": before.get("wind"),
            "wave": before.get("wave"),
            "air": before.get("air"),
            "water": before.get("water")
        },

        "odds_count": sum(
            v is not None
            for v in odds.values()
        ),

        "odds_error": odds_error,

        "notice": (
            "公式出走表・直前情報・"
            "公式3連単オッズを優先して分析。"
            "過去データ補正は次の段階で追加します。"
        ),

        "source": source,

        "before_source": before_source,

        "odds_source": odds_source
    })


# --------------------------------------------------
# 元の/api/analyzeを軽量版に置き換える
# --------------------------------------------------

app.view_functions[
    "api_analyze"
] = api_analyze_fixed
