"""동행복권 사이트 연동 점검 스크립트.

로그인 → 잔액/구매이력 조회 → 회차 조회 → 구매 직전 단계까지 실제로 호출해보고
각 단계의 성공/실패와 원인을 리포트한다.

실제 구매는 하지 않는다. 주문번호 발급(makeOrderNo.do)과 결제(connPro.do,
execBuy.do)는 호출하지 않으며, 구매 요청 본문은 전송 없이 미리보기만 출력한다.

    python3 verify_site.py
"""

import copy
import datetime
import json
import os
import re
import sys
import traceback

import requests
from bs4 import BeautifulSoup as BS
from dotenv import load_dotenv

from HttpClient import HttpClient

import auth
import common
import lotto645
import win720

# 윈도우 콘솔(cp949)에서 서버가 내려준 문자열 때문에 출력이 죽지 않도록 한다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def _mask(value, keep=6):
    text = str(value or "")
    if len(text) <= keep:
        return "*" * len(text)
    return f"{text[:keep]}...({len(text)}자)"


def step(title):
    def decorator(func):
        def wrapper(*args, **kwargs):
            print(f"\n{'=' * 70}\n[STEP] {title}\n{'-' * 70}")
            try:
                value = func(*args, **kwargs)
            except Exception as e:
                RESULTS.append((title, "FAIL", f"{type(e).__name__}: {e}"))
                print(f"  -> FAIL: {type(e).__name__}: {e}")
                traceback.print_exc(limit=3)
                return None
            RESULTS.append((title, "PASS", ""))
            print("  -> PASS")
            return value
        return wrapper
    return decorator


def dump_cookies(auth_ctrl, label):
    print(f"  [{label}] 쿠키 상태:")
    for cookie in auth_ctrl.http_client.session.cookies:
        print(f"    - {cookie.name} @ {cookie.domain} = {_mask(cookie.value)}")
    print(f"    => get_current_session_id() = {_mask(auth_ctrl.get_current_session_id())}")


def _credentials():
    load_dotenv(override=True)
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    if not username or not password:
        raise RuntimeError("USERNAME / PASSWORD 환경변수가 없습니다 (.env 확인)")
    if username.startswith("YOUR") or password.startswith("YOUR"):
        raise RuntimeError(
            f".env가 예시값 그대로입니다 (USERNAME={username!r}). 실제 값으로 채워주세요."
        )
    return username.strip(), password


@step("1. 로그인 및 인증 세션 확인")
def do_login():
    username, password = _credentials()

    print(f"  로그인 시도: {username[:3]}*** (아이디 {len(username)}자, 비밀번호 {len(password)}자)")
    auth_ctrl = auth.AuthController()
    auth_ctrl.login(username, password)
    print("  로그인 및 /mypage/selectUserMndp.do 인증 확인 통과")
    dump_cookies(auth_ctrl, "www 로그인 직후")
    return auth_ctrl, username


def diagnose_login():
    """로그인이 실패했을 때 어느 지점이 문제인지 요청 단위로 추적한다."""
    print(f"\n{'=' * 70}\n[진단] 로그인 시퀀스 상세 추적\n{'-' * 70}")
    try:
        username, password = _credentials()
    except RuntimeError as e:
        print(f"  자격증명을 읽을 수 없습니다: {e}")
        return

    client = HttpClient()  # 앞선 시도의 쿠키와 섞이지 않도록 새 세션 사용
    ctrl = auth.AuthController()
    ctrl.http_client = client

    def show(label, res):
        cookie_names = sorted({c.name for c in res.cookies})
        print(f"  {label}: {res.status_code} {res.url}")
        if res.history:
            print(f"      리다이렉트: {[h.status_code for h in res.history]} -> {res.url}")
        if cookie_names:
            print(f"      Set-Cookie: {cookie_names}")

    show("GET /", client.get(common.MAIN_URL))
    show("GET /login", client.get(common.LOGIN_PAGE_URL, headers=ctrl._REQ_HEADERS))

    modulus, exponent = ctrl._get_rsa_key()
    print(f"  RSA modulus 수신: {len(modulus)}자")

    headers = copy.deepcopy(ctrl._REQ_HEADERS)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.dhlottery.co.kr",
        "Referer": common.LOGIN_PAGE_URL,
    })
    res = client.post(
        "https://www.dhlottery.co.kr/login/securityLoginCheck.do",
        headers=headers,
        data={
            "userId": ctrl._rsa_encrypt(username, modulus, exponent),
            "userPswdEncn": ctrl._rsa_encrypt(password, modulus, exponent),
            "inpUserId": username,
        },
    )
    if res.encoding in (None, "ISO-8859-1"):
        res.encoding = "euc-kr"
    show("POST securityLoginCheck.do", res)

    body = res.text
    print(f"      응답 길이: {len(body)}자")
    markers = {
        "로그인 폼 재표시(loginForm)": "id=\"loginForm\"" in body,
        "errorMessage 변수": "errorMessage" in body,
        "비밀번호 변경 안내": ("비밀번호" in body and "변경" in body),
        "JC20(비밀번호 변경 유예)": "JC20" in body,
        "휴면/잠금 안내": ("휴면" in body or "잠금" in body or "제한" in body),
        "본인확인/추가인증": ("본인확인" in body or "추가인증" in body or "인증번호" in body),
        "securityLogout(로그인 상태 헤더)": "securityLogout" in body,
    }
    for name, hit in markers.items():
        print(f"      {'O' if hit else '.'} {name}")

    matched = re.search(r"errorMessage\s*=\s*'([^']*)'", body)
    print(f"      errorMessage 값: {matched.group(1)!r}" if matched else "      errorMessage 값: (없음)")

    for pattern in (r"\$\.alert\('([^']{2,120})'\)", r"alert\(\"([^\"]{2,120})\"\)"):
        for hit in re.findall(pattern, body):
            print(f"      alert(): {hit!r}")

    print(f"      쿠키: {[(c.name, c.domain) for c in client.session.cookies]}")

    res_main = client.get("https://www.dhlottery.co.kr/main", headers=ctrl._REQ_HEADERS)
    show("GET /main", res_main)
    print(f"      로그인 상태 표시(securityLogout 포함): {'securityLogout' in res_main.text}")

    timestamp = int(datetime.datetime.now().timestamp() * 1000)
    probe_headers = copy.deepcopy(ctrl._REQ_HEADERS)
    probe_headers.update({
        "Referer": "https://www.dhlottery.co.kr/mypage/home",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    probe_headers.pop("Content-Type", None)
    try:
        res_mndp = client.get(
            f"https://www.dhlottery.co.kr/mypage/selectUserMndp.do?_={timestamp}",
            headers=probe_headers,
        )
        print(f"  GET selectUserMndp.do: {res_mndp.status_code} / 본문 {res_mndp.text[:200]}")
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print(f"  GET selectUserMndp.do: {status} (인증 실패)")

    print(f"\n{'-' * 70}\n[진단] 비밀번호 만료 유예 / 90일 연장 처리\n{'-' * 70}")
    ctrl._user_id = username

    if auth.PSWD_EXPIRY_PAGE in (res.url or ""):
        print(f"  로그인 후 만료 안내 화면으로 이동함 -> nxtChngProc.do 유예 시도")
        ok, message = ctrl.defer_password_expiry()
        print(f"  nxtChngProc.do: {'성공' if ok else '실패'} - {message}")
        print(f"  유예 후 인증 상태: {'인증됨' if ctrl.is_authenticated() else '여전히 401'}")
    else:
        print(f"  만료 안내 화면이 아니므로 nxtChngProc.do 유예는 생략 (도착 URL: {res.url})")

    try:
        print(f"  연장 전 pwdChgLateYn: {ctrl.get_password_change_status().get('pwdChgLateYn')}")
    except Exception as e:
        print(f"  연장 전 상태 조회 실패: {e}")

    deferred, message = ctrl.defer_password_change()
    print(f"  updatePswdChgLate.do: {'성공' if deferred else '실패'} - {message}")

    try:
        print(f"  연장 후 pwdChgLateYn: {ctrl.get_password_change_status().get('pwdChgLateYn')}")
    except Exception as e:
        print(f"  연장 후 상태 조회 실패: {e}")

    print(f"  최종 인증 상태: {'인증됨' if ctrl.is_authenticated() else '여전히 401'}")


@step("1-2. 비밀번호 변경 90일 연장 상태 (selectTopResource.do)")
def check_password_status(auth_ctrl):
    status = auth_ctrl.get_password_change_status()
    print(f"  pwdChgLateYn = {status.get('pwdChgLateYn')} (Y면 30일 연장 적용됨)")
    print(f"  세션 잔여시간 = {status.get('time')}초")
    return status


@step("2. 예치금 잔액 조회 (selectUserMndp.do)")
def check_balance(auth_ctrl):
    balance = auth_ctrl.get_user_balance()
    print(f"  잔액: {balance}")
    if "실패" in balance or "확인 불가" in balance:
        raise RuntimeError(f"잔액 조회 실패: {balance}")
    return balance


@step("3. 회차 조회 (selectMainInfo.do)")
def check_rounds():
    rounds = common.get_last_drawn_rounds()
    print(f"  마지막 추첨 회차: {rounds}")
    lotto_round = lotto645.Lotto645()._get_round()
    win_round = win720.Win720()._get_round()
    print(f"  로또6/45 판매중 회차   : {lotto_round}")
    print(f"  연금복권720+ 판매중 회차: {win_round}")
    if rounds["lotto645"] is None or rounds["win720"] is None:
        raise RuntimeError("selectMainInfo.do에서 회차를 찾지 못했습니다")
    return lotto_round, win_round


@step("4. 로또6/45 당첨/구매 내역 조회 (selectMyLotteryledger.do)")
def check_lotto_ledger(auth_ctrl):
    item = lotto645.Lotto645().check_winning(auth_ctrl)
    print(f"  결과: {json.dumps(item, ensure_ascii=False)[:600]}")
    if str(item.get("data", "")).startswith("no winning data"):
        print("  (주의) 조회 결과가 비어 있습니다. 최근 7일 구매 내역이 없으면 정상입니다.")
    return item


@step("5. 연금복권720+ 당첨/구매 내역 조회 (selectMyLotteryledger.do)")
def check_win720_ledger(auth_ctrl):
    item = win720.Win720().check_winning(auth_ctrl)
    print(f"  결과: {json.dumps(item, ensure_ascii=False)[:600]}")
    return item


@step("6. 연금복권720+ 회차별 구매 수량 확인 (중복구매 방지 로직)")
def check_win720_purchase(auth_ctrl, win_round):
    pension = win720.Win720()
    purchase = pension._get_current_round_purchase(auth_ctrl, win_round)
    limit = pension._get_purchase_limit()
    print(f"  {win_round}회차 구매 게임 수: {purchase['count']} / 제한 {limit}")
    print(f"  주문 내역: {json.dumps(purchase['orders'], ensure_ascii=False)}")
    return purchase


@step("7. 로또6/45 구매 준비정보 조회 (egovUserReadySocket.json + game645.do)")
def check_lotto_requirements(auth_ctrl):
    lotto = lotto645.Lotto645()
    headers = lotto._generate_req_headers(auth_ctrl)

    html_headers = copy.deepcopy(lotto._REQ_HEADERS)
    html_headers.pop("Origin", None)
    html_headers.pop("Content-Type", None)
    html_headers["Referer"] = common.MAIN_URL
    res = lotto.http_client.get(
        "https://ol.dhlottery.co.kr/olotto/game/game645.do", headers=html_headers
    )
    soup = BS(res.text, "html5lib")
    found = {
        node_id: (soup.find("input", id=node_id) is not None)
        for node_id in ("ROUND_DRAW_DATE", "WAMT_PAY_TLMT_END_DT", "curRound", "direct")
    }
    print(f"  game645.do HTML input 존재 여부: {found}")
    # "세션이 해제" 문구는 로그인 상태에서도 조건부 분기로 템플릿에 남아 있으므로
    # 판단 기준으로 쓸 수 없다. 필수 input 유무로만 판단한다.
    if not found["ROUND_DRAW_DATE"]:
        raise RuntimeError("game645.do에 구매용 input이 없습니다 (비로그인/세션해제 페이지)")

    requirements = lotto._getRequirements(headers)
    print(f"  direct(ready_ip)     : {requirements[0]}")
    print(f"  ROUND_DRAW_DATE      : {requirements[1]}")
    print(f"  WAMT_PAY_TLMT_END_DT : {requirements[2]}")
    print(f"  round                : {requirements[3]}")
    if not all(found[k] for k in ("ROUND_DRAW_DATE", "WAMT_PAY_TLMT_END_DT")):
        raise RuntimeError("날짜 input을 HTML에서 찾지 못해 fallback 계산값을 사용했습니다")
    return requirements


@step("8. 로또6/45 구매 요청 본문 미리보기 (전송하지 않음)")
def preview_lotto_body(requirements):
    count = int(os.environ.get("COUNT") or 5)
    body = lotto645.Lotto645()._generate_body_for_auto_mode(count, requirements)
    print(f"  execBuy.do 로 보낼 본문: {json.dumps(body, ensure_ascii=False)}")
    print("  (실제 전송하지 않았습니다)")
    return body


@step("9. 연금복권720+ 게임 서버 세션 및 AES 암복호화 검증 (makeAutoNo.do)")
def check_win720_crypto(auth_ctrl, win_round):
    pension = win720.Win720()

    def el_session():
        return auth_ctrl.get_current_session_id(win720.EL_HOST)

    print(f"  진입 전 el 세션 ID: {_mask(el_session()) or '(없음)'}")

    total_game_url = f"{win720.EL_TOTAL_GAME_URL}?LottoId={win720.WIN720_LOTTO_ID}"
    res = pension.http_client.get(
        total_game_url, headers=pension._page_headers(common.MAIN_URL, same_origin=False)
    )
    print(f"  TotalGame.jsp: {len(res.text)}바이트 -> el 세션 {_mask(el_session()) or '(없음)'}")

    res = pension.http_client.get(
        win720.EL_GAME_URL, headers=pension._page_headers(total_game_url, same_origin=True)
    )
    print(f"  game.jsp: {len(res.text)}바이트, '로그인후 이용' 포함: {'로그인후 이용' in res.text}")
    dump_cookies(auth_ctrl, "el 게임창 진입 후")

    pension.keyCode = el_session()
    print(f"  AES 키로 사용할 el 세션 ID: {_mask(pension.keyCode) or '(없음)'}")
    if not pension.keyCode:
        raise RuntimeError(f"{win720.EL_HOST} 세션 ID가 발급되지 않았습니다")

    raw = pension._makeAutoNumbers(auth_ctrl, win_round)
    print(f"  makeAutoNo.do 응답: {raw[:120]}...")
    q_val = json.loads(raw)["q"]
    decrypted = pension._decText(q_val)
    print(f"  복호화 결과: {decrypted[:300]}")
    if "Decryption Failed" in decrypted or not decrypted:
        raise RuntimeError("AES 복호화 실패 - keyCode(세션 ID)가 서버 기대값과 다릅니다")
    sel = json.loads(decrypted).get("selLotNo", "")
    print(f"  자동 선택 번호(selLotNo): {sel or '(없음)'}")
    if not sel:
        raise RuntimeError(f"selLotNo를 받지 못했습니다: {decrypted[:200]}")
    print("  (주문번호 발급 makeOrderNo.do 및 결제 connPro.do 는 호출하지 않았습니다)")
    return sel


def summary():
    print(f"\n{'=' * 70}\n점검 결과 요약\n{'-' * 70}")
    failed = 0
    for title, status, detail in RESULTS:
        print(f"  [{status}] {title}{('  <- ' + detail) if detail else ''}")
        if status != "PASS":
            failed += 1
    print(f"{'-' * 70}\n총 {len(RESULTS)}단계 중 {failed}단계 실패")
    return 1 if failed else 0


def main():
    print("동행복권 연동 점검 시작 (실제 구매는 하지 않습니다)")

    login_result = do_login()
    if login_result is None:
        diagnose_login()
        print("\n로그인 실패로 이후 단계를 진행할 수 없습니다.")
        summary()
        return 1
    auth_ctrl, _username = login_result

    check_password_status(auth_ctrl)
    check_balance(auth_ctrl)
    rounds = check_rounds()
    check_lotto_ledger(auth_ctrl)
    check_win720_ledger(auth_ctrl)

    win_round = rounds[1] if rounds else None
    if win_round:
        check_win720_purchase(auth_ctrl, win_round)

    requirements = check_lotto_requirements(auth_ctrl)
    if requirements:
        preview_lotto_body(requirements)

    if win_round:
        check_win720_crypto(auth_ctrl, win_round)

    return summary()


if __name__ == "__main__":
    sys.exit(main())
