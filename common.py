import datetime
from datetime import timedelta

from HttpClient import HttpClientSingleton

BASE_URL = "https://www.dhlottery.co.kr"
MAIN_URL = f"{BASE_URL}/"
LOGIN_PAGE_URL = f"{BASE_URL}/login"
MAIN_INFO_URL = f"{BASE_URL}/selectMainInfo.do"


def get_search_date_range() -> dict:
    today = datetime.datetime.today()
    today_str = today.strftime("%Y%m%d")
    weekago = today - timedelta(days=7)
    weekago_str = weekago.strftime("%Y%m%d")
    return {
        "searchStartDate": weekago_str,
        "searchEndDate": today_str
    }


def get_last_drawn_rounds(headers: dict = None) -> dict:
    """메인 페이지 회차 정보 API에서 마지막 추첨 회차를 가져온다.

    구 사이트의 `common.do?method=main` HTML 스크래핑(strong#lottoDrwNo,
    strong#drwNo720)은 리뉴얼로 제거되어, 메인 화면이 실제로 호출하는
    `/selectMainInfo.do` JSON을 사용한다.
    """
    req_headers = dict(headers or {})
    req_headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": MAIN_URL,
    })
    req_headers.pop("Content-Type", None)
    req_headers.pop("Origin", None)

    res = HttpClientSingleton.get_instance().get(MAIN_INFO_URL, headers=req_headers)
    info = ((res.json() or {}).get("data") or {}).get("result") or {}
    epsd_info = info.get("pstLtEpstInfo") or {}

    def _max_round(rows, key):
        values = [row.get(key) for row in (rows or []) if row.get(key) is not None]
        return max(int(v) for v in values) if values else None

    return {
        "lotto645": _max_round(epsd_info.get("lt645"), "ltEpsd"),
        "win720": _max_round(epsd_info.get("pt720"), "psltEpsd"),
    }


SLOTS = ["A", "B", "C", "D", "E"]
