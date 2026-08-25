import copy
import json
import datetime
import base64
import os
import requests

from enum import Enum
from datetime import timedelta
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes

from HttpClient import HttpClientSingleton

import auth
import common
import re

import logging
import time

logger = logging.getLogger(__name__)

WIN720_LIMIT_ENV = "WIN720_LIMIT"
DEFAULT_WIN720_LIMIT = 5
WIN720_BUY_COUNT = 5

# 데스크톱 구매 진입점은 gmUtil.goGameClsf('LP72','PRCHS') ->
# {serviceElwasUrl}/game/TotalGame.jsp?LottoId=LP72 팝업이다.
# 이 진입을 거쳐야 el 서버가 암호화 키로 쓰이는 JSESSIONID를 발급한다.
EL_HOST = "el.dhlottery.co.kr"
EL_BASE_URL = f"https://{EL_HOST}"
WIN720_LOTTO_ID = "LP72"
EL_TOTAL_GAME_URL = f"{EL_BASE_URL}/game/TotalGame.jsp"
EL_GAME_URL = f"{EL_BASE_URL}/game/pension720/game.jsp"


class Win720:

    keySize = 128
    iterationCount = 1000
    BlockSize = 16
    keyCode = ""

    _pad = lambda self, s: s + (self.BlockSize - len(s) % self.BlockSize) * chr(self.BlockSize - len(s) % self.BlockSize)
    _unpad = lambda self, s : s[:-ord(s[len(s)-1:])]

    _REQ_HEADERS = {
        "User-Agent": auth.USER_AGENT,
        "Connection": "keep-alive",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "Origin": "https://el.dhlottery.co.kr",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Referer": "https://el.dhlottery.co.kr/game/pension720/game.jsp",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "sec-ch-ua-platform": "\"Windows\"",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ko,ko-KR;q=0.9,en-US;q=0.8,en;q=0.7",
        "X-Requested-With": "XMLHttpRequest"
    }

    def __init__(self):
        self.http_client = HttpClientSingleton.get_instance()

    def buy_Win720(
        self, 
        auth_ctrl: auth.AuthController,
        username: str
    ) -> dict:
        assert isinstance(auth_ctrl, auth.AuthController)

        win720_round = self._get_round()

        win720_limit = self._get_purchase_limit()
        current_purchase = self._get_current_round_purchase(auth_ctrl, win720_round)
        remaining_count = win720_limit - current_purchase["count"]
        if remaining_count < WIN720_BUY_COUNT:
            print(
                f"[Info] 연금복권 720+ {win720_round}회차는 이미 "
                f"{current_purchase['count']}게임을 구매했습니다. "
                f"회차별 제한은 {win720_limit}게임이고 1회 구매 수량은 {WIN720_BUY_COUNT}게임이므로 "
                "추가 구매를 건너뜁니다."
            )
            return None

        # 게임 창에 진입한 뒤, el 서버가 발급한 세션 ID를 암호화 키로 사용한다.
        # (호스트를 지정하지 않으면 ol 서버의 JSESSIONID를 집어와 복호화가 깨진다.)
        self.enter_game(auth_ctrl)
        self.keyCode = auth_ctrl.get_current_session_id(EL_HOST)
        if not self.keyCode:
            raise RuntimeError(f"{EL_HOST} 세션 ID를 찾을 수 없어 암호화를 진행할 수 없습니다.")

        makeAutoNum_ret = self._makeAutoNumbers(auth_ctrl, win720_round)
        
        try:
            q_val = json.loads(makeAutoNum_ret)['q']
        except json.JSONDecodeError:
            raise ValueError(f"Failed to parse makeAutoNum response: {makeAutoNum_ret[:100]}...")
        decrypted = self._decText(q_val)
        
        if "resultMsg" in decrypted and ":" in decrypted:
             decrypted = re.sub(r'("resultMsg":\s*)([^",}]*)([,}])', r'\1"\2"\3', decrypted)

        parsed_ret = decrypted
        try:
           extracted_num = json.loads(parsed_ret).get("selLotNo", "")
        except ValueError:
             raise ValueError(f"Failed to parse decrypted parsed_ret: {repr(parsed_ret)[:500]}... (Key: {self.keyCode[:5]}...{self.keyCode[-5:] if len(self.keyCode)>5 else ''})")

        if not extracted_num:
             return json.loads(parsed_ret)

        orderNo, orderDate = self._doOrderRequest(auth_ctrl, win720_round, extracted_num)
        
        body = json.loads(self._doConnPro(auth_ctrl, win720_round, extracted_num, username, orderNo, orderDate))

        self._show_result(body)
        body['round'] = win720_round
        return body

    def _generate_req_headers(self, auth_ctrl: auth.AuthController) -> dict:
        assert isinstance(auth_ctrl, auth.AuthController)
        return auth_ctrl.add_auth_cred_to_headers(self._REQ_HEADERS)

    def _page_headers(self, referer: str, same_origin: bool) -> dict:
        headers = copy.deepcopy(self._REQ_HEADERS)
        headers.pop("Content-Type", None)
        headers.pop("X-Requested-With", None)
        headers.pop("Origin", None)
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                      "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Site": "same-origin" if same_origin else "same-site",
        })
        return headers

    def enter_game(self, auth_ctrl: auth.AuthController) -> None:
        """브라우저와 같은 순서로 연금복권 게임 창에 진입한다.

        el 서버는 이 진입 과정에서 JSESSIONID를 발급하고, 게임 서버의
        encrypt.js가 그 값을 AES 키로 사용한다. 이 단계를 건너뛰면
        makeAutoNo.do 응답을 복호화할 수 없다.
        """
        total_game_url = f"{EL_TOTAL_GAME_URL}?LottoId={WIN720_LOTTO_ID}"

        self.http_client.get(
            total_game_url,
            headers=self._page_headers(common.MAIN_URL, same_origin=False),
        )
        self.http_client.get(
            EL_GAME_URL,
            headers=self._page_headers(total_game_url, same_origin=True),
        )
    
    def _get_purchase_limit(self) -> int:
        raw_limit = os.environ.get(WIN720_LIMIT_ENV)
        if not raw_limit:
            return DEFAULT_WIN720_LIMIT

        try:
            limit = int(raw_limit)
        except ValueError as e:
            raise ValueError(f"{WIN720_LIMIT_ENV}는 숫자로 설정해야 합니다: {raw_limit}") from e

        if limit < 1:
            raise ValueError(f"{WIN720_LIMIT_ENV}는 1 이상의 숫자로 설정해야 합니다: {raw_limit}")

        return limit

    def _get_current_round_purchase(self, auth_ctrl: auth.AuthController, win720_round: str) -> dict:
        headers = self._generate_ledger_headers(auth_ctrl)
        parameters = common.get_search_date_range()
        params = {
            "srchStrDt": parameters["searchStartDate"],
            "srchEndDt": parameters["searchEndDate"],
            "ltGdsCd": "LP72",
            "pageNum": 1,
            "recordCountPerPage": 20,
        }

        try:
            res = self.http_client.get(
                "https://www.dhlottery.co.kr/mypage/selectMyLotteryledger.do",
                params=params,
                headers=headers,
            )
            data = res.json().get("data", {}) or {}
        except (requests.RequestException, ValueError, AttributeError) as e:
            logger.warning(
                "[Warning] 연금복권 구매 이력 확인 실패. "
                "중복 구매 방지를 위해 구매를 중단합니다: %s",
                e,
            )
            raise RuntimeError(
                "연금복권 구매 이력을 확인하지 못해 중복 구매 위험이 있으므로 구매를 중단합니다."
            ) from e

        purchase = {
            "count": 0,
            "orders": [],
        }
        # 원장 API는 주문 1건을 구매 게임(조) 수만큼 여러 행으로 돌려준다.
        # 주문번호로 중복을 제거하지 않으면 게임 수가 배수로 부풀려져
        # (5게임 구매가 25게임으로 계산) 이후 구매가 영구히 막힌다.
        seen_order_nos = set()
        for item in data.get("list", []):
            item_round = self._normalize_round(item.get("ltEpsd") or item.get("ltEpsdView"))
            if item_round != str(win720_round):
                continue

            order_no = item.get("ntslOrdrNo") or "-"
            if order_no != "-":
                if order_no in seen_order_nos:
                    continue
                seen_order_nos.add(order_no)

            order = {
                "round": item_round,
                "purchased_date": item.get("eltOrdrDt", "-"),
                "order_no": order_no,
            }
            order["count"] = self._get_purchase_count_from_detail(auth_ctrl, order_no)
            purchase["count"] += order["count"]
            purchase["orders"].append(order)

        return purchase

    def _get_purchase_count_from_detail(self, auth_ctrl: auth.AuthController, order_no: str) -> int:
        if not order_no or order_no == "-":
            return WIN720_BUY_COUNT

        try:
            res = self.http_client.get(
                "https://www.dhlottery.co.kr/mypage/lottery720select.do",
                params={"ntslOrdrNo": order_no},
                headers=self._generate_ledger_headers(auth_ctrl),
            )
            detail_data = res.json().get("data", {}) or {}
            game_list = detail_data.get("list")
            if game_list:
                return len(game_list)
        except (requests.RequestException, ValueError, AttributeError) as e:
            logger.warning(
                "[Warning] 연금복권 상세 구매 이력 확인 실패(order_no=%s). 기본 구매 수량(%s게임)으로 계산합니다: %s",
                order_no,
                WIN720_BUY_COUNT,
                e,
            )

        return WIN720_BUY_COUNT

    def _generate_ledger_headers(self, auth_ctrl: auth.AuthController) -> dict:
        headers = self._generate_req_headers(auth_ctrl)
        headers.update({
            "Referer": "https://www.dhlottery.co.kr/mypage/mylotteryledger",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        })
        headers.pop("Content-Type", None)
        headers.pop("Origin", None)
        return headers

    def _normalize_round(self, value) -> str:
        matched = re.search(r"\d+", str(value or ""))
        if not matched:
            return ""
        return str(int(matched.group()))


    def _get_round(self) -> str:
        try:
            last_drawn_round = common.get_last_drawn_rounds(self._REQ_HEADERS)["win720"]
            if last_drawn_round is None:
                raise ValueError("pt720 psltEpsd not found in selectMainInfo.do")
            return str(last_drawn_round + 1)
        except (requests.RequestException, AttributeError, ValueError, KeyError):
             base_date = datetime.datetime(2024, 12, 26)
             base_round = 244
             
             today = datetime.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
             
             days_ahead = (3 - today.weekday()) % 7
             next_thursday = today + datetime.timedelta(days=days_ahead)
             
             weeks = (next_thursday - base_date).days // 7
             
             return str(base_round + weeks - 1)

    def _makeAutoNumbers(self, auth_ctrl: auth.AuthController, win720_round: str) -> str:
        payload = "ROUND={}&round={}&LT_EPSD={}&SEL_NO=&BUY_CNT=&AUTO_SEL_SET=SA&SEL_CLASS=&BUY_TYPE=A&ACCS_TYPE=01".format(win720_round, win720_round, win720_round)
        headers = self._generate_req_headers(auth_ctrl)
        
        data = {
            "q": requests.utils.quote(self._encText(payload))
        }

        max_retries = 5
        for attempt in range(max_retries):
            try:
                res = self.http_client.post(
                    url="https://el.dhlottery.co.kr/makeAutoNo.do", 
                    headers=headers,
                    data=data
                )
                res.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"[Retry] makeAutoNo connection failed ({attempt+1}/{max_retries}): {e}. Retrying in 2s...")
                    time.sleep(2)
                else:
                    logger.error(f"[Error] makeAutoNo connection failed after {max_retries} attempts: {e}")
                    raise

        return res.text

    def _doOrderRequest(self, auth_ctrl: auth.AuthController, win720_round: str, extracted_num: str) -> str:
        payload = "ROUND={}&round={}&LT_EPSD={}&AUTO_SEL_SET=SA&SEL_CLASS=&SEL_NO={}&BUY_TYPE=M&BUY_CNT=5".format(win720_round, win720_round, win720_round, extracted_num)
        headers = self._generate_req_headers(auth_ctrl)

        data = {
            "q": requests.utils.quote(self._encText(payload))
        }

        max_retries = 5
        for attempt in range(max_retries):
            try:
                res = self.http_client.post(
                    url="https://el.dhlottery.co.kr/makeOrderNo.do", 
                    headers=headers,
                    data=data
                )
                res.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"[Retry] makeOrderNo connection failed ({attempt+1}/{max_retries}): {e}. Retrying in 2s...")
                    time.sleep(2)
                else:
                    logger.error(f"[Error] makeOrderNo connection failed after {max_retries} attempts: {e}")
                    raise

        try:
            ret = json.loads(self._decText(json.loads(res.text)['q']))
            return ret['orderNo'], ret['orderDate']
        except (json.JSONDecodeError, KeyError) as err:
             raise ValueError(f"Failed to parse doOrderRequest/decText: {res.text[:100]}...") from err

    def _doConnPro(self, auth_ctrl: auth.AuthController, win720_round: str, extracted_num: str, username: str, orderNo: str, orderDate: str) -> str:
        payload = "ROUND={}&FLAG=&BUY_KIND=01&BUY_NO={}&BUY_CNT=5&BUY_SET_TYPE=SA%2CSA%2CSA%2CSA%2CSA&BUY_TYPE=A%2CA%2CA%2CA%2CA%2C&CS_TYPE=01&orderNo={}&orderDate={}&TRANSACTION_ID=&WIN_DATE=&USER_ID={}&PAY_TYPE=&resultErrorCode=&resultErrorMsg=&resultOrderNo=&WORKING_FLAG=true&NUM_CHANGE_TYPE=&auto_process=N&set_type=SA&classnum=&selnum=&buytype=M&num1=&num2=&num3=&num4=&num5=&num6=&DSEC=34&CLOSE_DATE=&verifyYN=N&curdeposit=&curpay=5000&DROUND={}&DSEC=0&CLOSE_DATE=&verifyYN=N&lotto720_radio_group=on".format(win720_round,"".join([ "{}{}%2C".format(i,extracted_num) for i in range(1,6)])[:-3],orderNo, orderDate, username, win720_round)
        headers = self._generate_req_headers(auth_ctrl)
        
        data = {
            "q": requests.utils.quote(self._encText(payload))
        }
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                res = self.http_client.post(
                    url="https://el.dhlottery.co.kr/connPro.do", 
                    headers=headers,
                    data=data
                )
                res.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"[Retry] connPro connection failed ({attempt+1}/{max_retries}): {e}. Retrying in 2s...")
                    time.sleep(2)
                else:
                    logger.error(f"[Error] connPro connection failed after {max_retries} attempts: {e}")
                    raise

        try:
            ret = self._decText(json.loads(res.text)['q'])
        except (json.JSONDecodeError, KeyError) as err:
             raise ValueError(f"Failed to parse doConnPro: {res.text[:100]}...") from err
        else:
            return ret

    def _encText(self, plainText: str) -> str:
        encSalt = get_random_bytes(32)
        encIV = get_random_bytes(16)
        passPhrase = self.keyCode[:32]
        encKey = PBKDF2(passPhrase, encSalt, self.BlockSize, count=self.iterationCount, hmac_hash_module=SHA256)
        aes = AES.new(encKey, AES.MODE_CBC, encIV)

        plainText = self._pad(plainText).encode('utf-8')

        return "{}{}{}".format(bytes.hex(encSalt), bytes.hex(encIV), base64.b64encode(aes.encrypt(plainText)).decode('utf-8'))

    def _decText(self, encText: str) -> str:

        decSalt = bytes.fromhex(encText[0:64])
        decIv = bytes.fromhex(encText[64:96])
        cryptText = encText[96:]
        passPhrase = self.keyCode[:32]
        decKey = PBKDF2(passPhrase, decSalt, self.BlockSize, count=self.iterationCount, hmac_hash_module=SHA256)

        aes = AES.new(decKey, AES.MODE_CBC, decIv)

        decrypted_bytes = self._unpad(aes.decrypt(base64.b64decode(cryptText)))
        try:
            return decrypted_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return decrypted_bytes.decode('euc-kr')
            except UnicodeDecodeError:
                return f'{{"resultMsg": "Decryption Failed (Raw: {decrypted_bytes.hex()[:20]}...)"}}'




    def check_winning(self, auth_ctrl: auth.AuthController) -> dict:
        assert isinstance(auth_ctrl, auth.AuthController)

        headers = self._generate_req_headers(auth_ctrl)

        parameters = common.get_search_date_range()
        data = {
            "nowPage": 1, 
            "searchStartDate": parameters["searchStartDate"],
            "searchEndDate": parameters["searchEndDate"],
            "winGrade": 1,
            "lottoId": "LP72", 
            "sortOrder": "DESC"
        }

        result_data = {
            "data": "no winning data"
        }

        try:
            api_url = "https://www.dhlottery.co.kr/mypage/selectMyLotteryledger.do"
            params = {
                "srchStrDt": parameters["searchStartDate"],
                "srchEndDt": parameters["searchEndDate"],
                "ltGdsCd": "LP72",
                "pageNum": 1,
                "recordCountPerPage": 10
            }
            
            res = self.http_client.get(api_url, params=params, headers=headers)
            
            if res.status_code == 200:
                try:
                    data = res.json()
                    data = data.get("data", {})
                    
                    if data.get("list"):
                        item = data["list"][0]
                        
                        purchased_date = item.get("eltOrdrDt", "-")
                        round_no = item.get("ltEpsdView", "")
                        money_raw = item.get("ltWnAmt", "0")
                        if money_raw is None:
                            money_raw = "0"
                        
                        if "회" in round_no:
                            round_no = round_no.replace("회", "")
                        
                        try:
                            val = int(money_raw)
                            money = f"{val:,} 원"
                        except (ValueError, TypeError):
                            money = "0 원"
                            
                        result_data = {
                            "round": round_no,
                            "money": money,
                            "purchased_date": purchased_date,
                            "winning_date": item.get("epsdRflDt", "-"),
                            "win720_details": []
                        }
                        
                        try:
                            detail_url = "https://www.dhlottery.co.kr/mypage/lottery720select.do"
                            detail_params = {
                                "ntslOrdrNo": item.get("ntslOrdrNo")
                            }
                            
                            res_detail = self.http_client.get(detail_url, params=detail_params, headers=headers)
                            detail_data = res_detail.json()
                            
                            detail_data = detail_data.get("data", detail_data)
                            
                            win720_details = []
                            
                            if "list" in detail_data:
                                for i, d_item in enumerate(detail_data["list"]):
                                    label = common.SLOTS[i] if i < len(common.SLOTS) else "?"
                                    
                                    info_cn = d_item.get("ltGmInfoCn", "")
                                    
                                    rank = d_item.get("wnRnk")
                                    if rank is None:
                                        rank = 0
                                    else:
                                        try:
                                            rank = int(rank)
                                        except (ValueError, TypeError):
                                            rank = 0
                                            
                                    status = "0등" if rank == 0 else f"{rank}등"
                                    
                                    if ":" in info_cn:
                                        parts = info_cn.split(":")
                                        group = parts[0]
                                        number_str = parts[1]
                                        
                                        hl_count = 0 
                                        hl_group = False
                                        
                                        if rank == 1:
                                            hl_count = 6
                                            hl_group = True
                                        elif rank == 2:
                                            hl_count = 6
                                        elif rank == 3:
                                            hl_count = 5
                                        elif rank == 4:
                                            hl_count = 4
                                        elif rank == 5:
                                            hl_count = 3
                                        elif rank == 6:
                                            hl_count = 2
                                        elif rank == 7:
                                            hl_count = 1
                                        
                                        formatted_chars = []
                                        digits = list(number_str)
                                        L = len(digits)
                                        
                                        for idx, digit in enumerate(digits):
                                            if idx >= (L - hl_count):
                                                formatted_chars.append(f"[{digit}]")
                                            else:
                                                formatted_chars.append(f" {digit} ")
                                        
                                        formatted_num = " ".join(formatted_chars)
                                        
                                        label = f"{group}조"
                                        
                                        result_str = formatted_num
                                    else:
                                        label = "?"
                                        result_str = info_cn
                                    
                                    
                                    win720_details.append({
                                        "label": label,
                                        "result": result_str,
                                        "status": status
                                    })
                                    
                            result_data["win720_details"] = win720_details

                        except Exception as e:
                            logger.error(f"[Error] Win720 detail error: {e}")
                            
                except Exception as e:
                     logger.error(f"[Error] Win720 list process error: {e}")
            
        except Exception as e:
            logger.error(f"[Error] Win720 check error: {e}")

        return result_data
    

    def _show_result(self, body: dict) -> None:
        assert isinstance(body, dict)

        if body.get("loginYn") != "Y":
            return

        result = body.get("result", {})
        if result.get("resultMsg", "FAILURE").upper() != "SUCCESS":    
            return