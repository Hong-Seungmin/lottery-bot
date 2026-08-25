import copy
import datetime
import re
import requests
import json
import base64
import binascii
import time
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from HttpClient import HttpClientSingleton

import common

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# 게임 서버(ol/el)는 JSESSIONID, 포털(www)은 DHJSESSIONID를 사용한다.
SESSION_COOKIE_NAMES = ("JSESSIONID", "DHJSESSIONID")

# 비밀번호 변경 후 90일이 지나면 사이트가 변경을 요구하며 로그인 세션을 막는다.
# 브라우저의 "다음에 변경하기" 버튼과 동일하게 30일 연장 처리를 한다.
PSWD_CHG_LATE_URL = "https://www.dhlottery.co.kr/sy/updatePswdChgLate.do"
# 비밀번호가 만료되면 로그인 직후 이 화면으로 302 리다이렉트되고 mypage 는 401이 된다.
# 이 화면의 "다음에 변경"(#btnCancel -> ExpryPswdNotiM.fn_nxtChg) 이 유예를 처리하고,
# 유예 성공 후 loginSuccess.do 로 이동해야 로그인 세션이 확정된다.
PSWD_EXPIRY_PAGE = "/mbrsrvc/ExpryPswdNoti"
PSWD_NXT_CHNG_URL = "https://www.dhlottery.co.kr/mbrsrvc/nxtChngProc.do"
LOGIN_SUCCESS_URL = "https://www.dhlottery.co.kr/login/loginSuccess.do"
TOP_RESOURCE_URL = "https://www.dhlottery.co.kr/sy/selectTopResource.do"


class LoginError(Exception):
    """로그인이 정상적으로 완료되지 않았을 때 발생한다."""


class AuthController:
    _REQ_HEADERS = {
        "User-Agent": USER_AGENT,
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "Upgrade-Insecure-Requests": "1",
        "Origin": "https://www.dhlottery.co.kr",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Referer": "https://www.dhlottery.co.kr/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Accept-Language": "ko,en-US;q=0.9,en;q=0.8,ko-KR;q=0.7",
    }

    _AUTH_CRED = ""

    def __init__(self):
        self.http_client = HttpClientSingleton.get_instance()
        self._user_id = ""

    def login(self, user_id: str, password: str):
        assert isinstance(user_id, str)
        assert isinstance(password, str)

        self._user_id = user_id
        max_retries = 5
        for attempt in range(max_retries):
            try:
                # 1. Warm-up
                self.http_client.get(common.MAIN_URL)
                self.http_client.get(common.LOGIN_PAGE_URL, headers=self._REQ_HEADERS)
                
                # 2. RSA Key Fetch
                modulus, exponent = self._get_rsa_key()

                # 3. Encrypt
                enc_user_id = self._rsa_encrypt(user_id, modulus, exponent)
                enc_password = self._rsa_encrypt(password, modulus, exponent)

                # 4. Prepare Login Request
                headers = copy.deepcopy(self._REQ_HEADERS)
                headers.update({
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.dhlottery.co.kr",
                    "Referer": common.LOGIN_PAGE_URL
                })
                
                data = {
                    "userId": enc_user_id,
                    "userPswdEncn": enc_password, 
                    "inpUserId": user_id
                }

                # 5. Execute Login
                self._try_login(headers, data)
                return # Success
                
            except requests.RequestException as e:
                time.sleep(2)
                if attempt == max_retries - 1:
                     print(f"[Error] Login sequence failed after {max_retries} attempts: {e}")
                     raise
                print(f"[Retry] Login failed ({attempt+1}/{max_retries}): {e}. Retrying...")
        
    def add_auth_cred_to_headers(self, headers: dict) -> str:
        assert isinstance(headers, dict)

        copied_headers = copy.deepcopy(headers)
        return copied_headers

    def _get_default_auth_cred(self):
        res = self.http_client.get(common.MAIN_URL)
        return self._get_j_session_id_from_response(res)

    def _get_rsa_key(self):
        headers = copy.deepcopy(self._REQ_HEADERS)
        headers.update({
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": common.LOGIN_PAGE_URL
        })
        headers.pop("Upgrade-Insecure-Requests", None)

        res = self.http_client.get(
            "https://www.dhlottery.co.kr/login/selectRsaModulus.do",
            headers=headers
        )
        
        try:
            data = res.json()
        except ValueError:
             raise ValueError(f"Failed to parse JSON. St: {res.status_code}")
        
        if "data" in data and "rsaModulus" in data["data"]:
            modulus = data["data"]["rsaModulus"]
            exponent = data["data"]["publicExponent"]
            return modulus, exponent
        
        if "rsaModulus" in data:
            return data["rsaModulus"], data["publicExponent"]
            
        raise KeyError("rsaModulus not found")

    def _rsa_encrypt(self, text, modulus, exponent):
        key_spec = RSA.construct((int(modulus, 16), int(exponent, 16)))
        cipher = PKCS1_v1_5.new(key_spec)
        ciphertext = cipher.encrypt(text.encode('utf-8'))
        return binascii.hexlify(ciphertext).decode('utf-8')

    def _get_j_session_id_from_response(self, res: requests.Response):
        assert isinstance(res, requests.Response)

        for name in SESSION_COOKIE_NAMES:
            for cookie in res.cookies:
                if cookie.name == name:
                    return cookie.value

        return self.get_current_session_id()

    def _generate_req_headers(self):
        return copy.deepcopy(self._REQ_HEADERS)

    def _try_login(self, headers: dict, data: dict):
        assert isinstance(headers, dict)
        assert isinstance(data, dict)
        
        res = self.http_client.post(
            "https://www.dhlottery.co.kr/login/securityLoginCheck.do",
            headers=headers,
            data=data,
        )
        
        self._raise_if_login_rejected(res)

        # 로그인 직후 서버가 어느 화면으로 보냈는지 기록한다.
        # 비밀번호 만료 시 /mbrsrvc/ExpryPswdNoti 로 302 리다이렉트된다.
        landed_on = res.url or ""
        if PSWD_EXPIRY_PAGE in landed_on:
            print(f"[Warning] 로그인 후 비밀번호 만료 안내 화면으로 이동했습니다: {landed_on}")

        new_jsessionid = self._get_j_session_id_from_response(res)
        if new_jsessionid:
             self._update_auth_cred(new_jsessionid)

        try:
             self.http_client.get("https://www.dhlottery.co.kr/main", headers=self._REQ_HEADERS)
        except Exception as e:
             print(f"[Warning] Failed to check main page after login: {e}")

        if not self.is_authenticated():
            attempts = []

            # 1) 비밀번호 만료 게이트: "다음에 변경"과 동일한 유예 처리
            if PSWD_EXPIRY_PAGE in landed_on:
                ok, message = self.defer_password_expiry()
                attempts.append(f"만료 유예(nxtChngProc.do): {message}")
                if ok:
                    print(f"[Info] 비밀번호 만료를 '다음에 변경'으로 유예했습니다. {message}")
                else:
                    print(f"[Warning] 비밀번호 만료 유예에 실패했습니다: {message}")

            # 2) 그래도 막혀 있으면 90일 경과 권고 연장을 시도한다.
            if not self.is_authenticated():
                ok, message = self.defer_password_change()
                attempts.append(f"90일 권고 연장(updatePswdChgLate.do): {message}")
                if ok:
                    print("[Info] 비밀번호 변경 90일 경과 안내를 30일 연장했습니다.")

                if not self.is_authenticated():
                    raise LoginError(
                        "로그인 후에도 인증 세션이 확인되지 않습니다 "
                        f"(로그인 후 도착 URL: {landed_on}). "
                        + " / ".join(attempts)
                        + ". 비밀번호 만료·변경 요구, 계정 휴면/잠금, 접속 IP 차단 여부를 확인하세요."
                    )

        return res

    def _raise_if_login_rejected(self, res: requests.Response) -> None:
        """로그인 실패 시 사이트는 200으로 로그인 화면을 다시 그리고
        errorMessage 변수에 사유를 담아 내려준다."""
        if res.encoding in (None, "ISO-8859-1"):
            res.encoding = "euc-kr"

        matched = re.search(r"errorMessage\s*=\s*'([^']*)'", res.text)
        if matched and matched.group(1).strip():
            raise LoginError(f"로그인이 거부되었습니다: {matched.group(1).strip()}")

    def is_authenticated(self) -> bool:
        """인증 전용 엔드포인트로 로그인 세션이 살아있는지 확인한다.

        로그인에 실패해도 이후 요청이 401로 조용히 실패할 뿐이라
        구매 단계까지 진행되는 문제가 있어 여기서 명시적으로 확인한다."""
        timestamp = int(datetime.datetime.now().timestamp() * 1000)
        url = f"https://www.dhlottery.co.kr/mypage/selectUserMndp.do?_={timestamp}"

        headers = copy.deepcopy(self._REQ_HEADERS)
        headers.update({
            "Referer": "https://www.dhlottery.co.kr/mypage/home",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        headers.pop("Content-Type", None)

        try:
            self.http_client.get(url, headers=headers)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403):
                return False
            raise
        return True

    def _ajax_headers(self, request_menu_uri: str) -> dict:
        headers = copy.deepcopy(self._REQ_HEADERS)
        headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "AJAX": "true",
            "requestMenuUri": request_menu_uri,
            "Referer": f"https://www.dhlottery.co.kr{request_menu_uri}",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Dest": "empty",
        })
        headers.pop("Upgrade-Insecure-Requests", None)
        headers.pop("Sec-Fetch-User", None)
        return headers

    def defer_password_change(self) -> tuple:
        """비밀번호 변경을 30일 연장한다. (사이트의 "다음에 변경하기"와 동일)

        Returns: (성공여부, 서버 메시지)
        """
        try:
            res = self.http_client.post(
                PSWD_CHG_LATE_URL,
                headers=self._ajax_headers("/main"),
                json={},
            )
        except requests.RequestException as e:
            return False, f"요청 실패: {e}"

        try:
            body = res.json() or {}
        except ValueError:
            return False, f"JSON 아님: {res.text[:120]}"

        if body.get("resultCode"):
            return False, body.get("resultMessage") or body.get("resultCode")

        data = body.get("data") or {}
        if not data.get("userId"):
            return False, f"userId 없음: {res.text[:120]}"

        return True, f"userId={data['userId']} 연장 완료"

    def defer_password_expiry(self) -> tuple:
        """비밀번호 만료 안내 화면의 "다음에 변경" 버튼과 동일한 유예 처리.

        Returns: (성공여부, 서버 메시지)
        """
        try:
            res = self.http_client.post(
                PSWD_NXT_CHNG_URL,
                headers=self._ajax_headers(PSWD_EXPIRY_PAGE),
                json={},
            )
        except requests.RequestException as e:
            return False, f"요청 실패: {e}"

        try:
            body = res.json() or {}
        except ValueError:
            return False, f"JSON 아님: {res.text[:120]}"

        if body.get("resultCode"):
            return False, body.get("resultMessage") or body.get("resultCode")

        data = body.get("data") or {}
        try:
            result_cnt = int(data.get("resultCnt") or 0)
        except (TypeError, ValueError):
            result_cnt = 0

        if result_cnt <= 0:
            return False, data.get("resultMsg") or f"resultCnt=0 ({res.text[:120]})"

        # 브라우저와 동일하게 로그인 완료 화면으로 이동해 세션을 확정한다.
        try:
            self.http_client.get(
                LOGIN_SUCCESS_URL,
                headers=self._REQ_HEADERS,
                params={"returnUrl": "/main"},
            )
        except requests.RequestException as e:
            return True, f"유예 성공(resultCnt={result_cnt}) / loginSuccess 이동 실패: {e}"

        return True, f"유예 성공 (resultCnt={result_cnt})"

    def get_password_change_status(self) -> dict:
        """비밀번호 변경 연장 여부(pwdChgLateYn)를 조회한다."""
        headers = self._ajax_headers("/main")
        headers.pop("Content-Type", None)
        res = self.http_client.get(
            TOP_RESOURCE_URL,
            headers=headers,
            params={"pwdChgLate": self._user_id or ""},
        )
        return (res.json() or {}).get("data") or {}

    def _update_auth_cred(self, j_session_id: str) -> None:
        assert isinstance(j_session_id, str)
        # 서버가 내려준 세션 쿠키는 requests 쿠키자에 이미 반영되어 있으므로
        # 여기서 쿠키를 새로 만들지 않는다. (직접 만든 JSESSIONID가 게임 서버
        # ol/el이 발급하는 실제 JSESSIONID를 가려 암호화 키가 깨졌었다.)
        self._AUTH_CRED = j_session_id

    def get_current_session_id(self, host: str = None) -> str:
        """세션 ID를 반환한다.

        ol/el 게임 서버는 각자 자기 JSESSIONID를 AES 키로 쓰기 때문에(encrypt.js),
        host를 지정하면 그 호스트에 발급된 쿠키만 골라야 한다. 지정하지 않으면
        쿠키자 순서에 따라 다른 서버의 세션 ID를 집어올 수 있다.
        WMONID는 세션 식별자가 아니므로 후보에서 제외한다.
        """
        jar = self.http_client.session.cookies

        def _domain_of(cookie):
            return (cookie.domain or "").lstrip(".")

        if host:
            # 1) 해당 호스트에 정확히 발급된 쿠키
            for name in SESSION_COOKIE_NAMES:
                for cookie in jar:
                    if cookie.name == name and _domain_of(cookie) == host:
                        return cookie.value
            # 2) 그 호스트를 포함하는 상위 도메인 쿠키 (.dhlottery.co.kr)
            for name in SESSION_COOKIE_NAMES:
                for cookie in jar:
                    domain = _domain_of(cookie)
                    if cookie.name == name and domain and host.endswith(domain):
                        return cookie.value
        else:
            for name in SESSION_COOKIE_NAMES:
                for cookie in jar:
                    if cookie.name == name:
                        return cookie.value

        return self._AUTH_CRED or ""
            
    def get_user_balance(self) -> str:
        last_error = None
        
        for attempt in range(3):
            try:
                 try:
                     self.http_client.get("https://www.dhlottery.co.kr/mypage/home")
                 except requests.RequestException:
                     pass

                 timestamp = int(datetime.datetime.now().timestamp() * 1000)
                 url = f"https://www.dhlottery.co.kr/mypage/selectUserMndp.do?_={timestamp}"
                 
                 headers = copy.deepcopy(self._REQ_HEADERS)
                 headers.update({
                    "Referer": "https://www.dhlottery.co.kr/mypage/home",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "requestMenuUri": "/mypage/home",
                    "AJAX": "true",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Dest": "empty"
                 })
                 
                 res = self.http_client.get(url, headers=headers)
                 
                 txt = res.text.strip()
                 if txt.startswith("<"):
                      return "확인 불가 (로그인/설정)"

                 data = json.loads(txt)
                 
                 if 'data' in data and isinstance(data['data'], dict):
                     data = data['data']

                 if 'userMndp' in data:
                     data = data['userMndp']
                     
                 if 'totalAmt' in data:
                     val = str(data['totalAmt']).replace(',', '')
                     return f"{int(val):,}원"
                 
                 return "0원"

            except Exception as e:
                 last_error = e
                 time.sleep(1)
        
        # If all retries failed
        print(f"[Error] 잔액 조회에 실패했습니다: {last_error}")
        return f"정보 로드 실패 (로그 확인)"
