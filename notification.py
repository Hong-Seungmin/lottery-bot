import html
import re

import requests


class Notification:
    def send_lotto_buying_message(self, body: dict, destination: dict) -> None:
        if not body:
            return

        result = body.get("result", {})
        result_msg = result.get("resultMsg", body.get("resultMsg", "Unknown Error"))
        if result.get("resultMsg", "FAILURE").upper() != "SUCCESS":
            if self._is_balance_shortage(body):
                self.send_purchase_balance_shortage_message(
                    "로또 6/45",
                    body.get("balance", "확인불가"),
                    result_msg,
                    destination,
                )
            return

        lotto_number_str = self.make_lotto_number_message(result["arrGameChoiceNum"])
        message = (
            "🎟️ <b>로또 6/45 구매 완료</b>\n"
            f"• 회차: <b>{self._escape(result['buyRound'])}회</b>\n"
            f"• 남은 잔액: <b>{self._escape(body.get('balance', '확인불가'))}</b>\n"
            f"{self._code_block(lotto_number_str)}"
        )
        self._send_message(destination, message)

    def make_lotto_number_message(self, lotto_number: list) -> str:
        assert type(lotto_number) == list

        # parse list without last number 3
        lotto_number = [x[:-1] for x in lotto_number]

        # remove alphabet and | replace white space  from lotto_number
        lotto_number = [x.replace("|", " ") for x in lotto_number]

        # lotto_number to string
        lotto_number = "\n".join(x for x in lotto_number)

        return lotto_number

    def send_win720_buying_message(self, body: dict, destination: dict) -> None:
        if not body:
            return

        if body.get("resultCode") != "100":
            if self._is_balance_shortage(body):
                self.send_purchase_balance_shortage_message(
                    "연금복권 720+",
                    body.get("balance", "확인불가"),
                    body.get("resultMsg", "Unknown Error"),
                    destination,
                )
            return

        win720_round = body.get("round", "?")
        if win720_round == "?":
            try:
                win720_round = body.get("saleTicket", "").split("|")[-2]
            except (IndexError, AttributeError, TypeError):
                win720_round = "?"

        if not body.get("saleTicket"):
            win720_number_str = "번호 정보 없음"
        else:
            win720_number_str = self.make_win720_number_message(body.get("saleTicket"))

        message = (
            "💰 <b>연금복권 720+ 구매 완료</b>\n"
            f"• 회차: <b>{self._escape(win720_round)}회</b>\n"
            f"• 남은 잔액: <b>{self._escape(body.get('balance', '확인불가'))}</b>\n"
            f"{self._code_block(win720_number_str)}"
        )
        self._send_message(destination, message)

    def make_win720_number_message(self, win720_number: str) -> str:
        formatted_numbers = []
        for number in win720_number.split(","):
            formatted_number = f"{number[0]}조 " + " ".join(number[1:])
            formatted_numbers.append(formatted_number)
        return "\n".join(formatted_numbers)

    def send_lotto_winning_message(self, winning: dict, destination: dict) -> None:
        assert type(winning) == dict

        balance_str = winning.get("balance", "확인불가")
        try:
            if winning["lotto_details"]:
                max_label_status_length = max(
                    len(f"{line['label']} {line['status']}")
                    for line in winning["lotto_details"]
                )

                formatted_lines = []
                for line in winning["lotto_details"]:
                    line_label_status = f"{line['label']} {line['status']}".ljust(max_label_status_length)
                    line_result = line["result"]

                    formatted_nums = []
                    for num in line_result:
                        matched = re.search(r"\d+", num)
                        raw_num = matched.group() if matched else "0"
                        formatted_num = f"{int(raw_num):02d}"
                        if "✨" in num:
                            formatted_nums.append(f"[{formatted_num}]")
                        else:
                            formatted_nums.append(f" {formatted_num} ")

                    formatted_nums = [f"{num:>6}" for num in formatted_nums]

                    formatted_line = f"{line_label_status} " + " ".join(formatted_nums)
                    formatted_lines.append(formatted_line)

                formatted_results = "\n".join(formatted_lines)
            else:
                formatted_results = "상세 정보를 불러오지 못했습니다."

            is_winning = winning["money"] != "-" and winning["money"] != "0 원" and winning["money"] != "0"
            result_message = "🎉 당첨" if is_winning else "🫠 다음 기회에"

            message = (
                "🎯 <b>로또 6/45 결과</b>\n"
                f"• 회차: <b>{self._escape(winning['round'])}회</b>\n"
                f"• 결과: <b>{result_message}</b>\n"
                f"• 당첨금: <b>{self._escape(winning['money'])}</b>\n"
                f"• 남은 잔액: <b>{self._escape(balance_str)}</b>\n"
                f"{self._code_block(formatted_results)}"
            )
            self._send_message(destination, message)
        except KeyError:
            message = (
                "🎯 <b>로또 6/45 결과</b>\n"
                "• 결과: <b>🫠 다음 기회에</b>\n"
                f"• 남은 잔액: <b>{self._escape(balance_str)}</b>"
            )
            self._send_message(destination, message)
            return

    def send_win720_winning_message(self, winning: dict, destination: dict) -> None:
        assert type(winning) == dict

        balance_str = winning.get("balance", "확인불가")
        try:
            if "win720_details" in winning and winning["win720_details"]:
                max_label_status_length = max(
                    len(f"{line['label']} {line['status']}")
                    for line in winning["win720_details"]
                )
                formatted_lines = []
                for line in winning["win720_details"]:
                    line_label_status = f"{line['label']} {line['status']}".ljust(max_label_status_length)
                    formatted_lines.append(f"{line_label_status} {line['result']}")

                formatted_results = "\n".join(formatted_lines)
                result_block = self._code_block(formatted_results)
            else:
                result_block = ""

            is_winning = winning["money"] != "-" and winning["money"] != "0 원" and winning["money"] != "0"
            result_message = "🎉 당첨" if is_winning else "🫠 다음 기회에"

            message = (
                "🎯 <b>연금복권 720+ 결과</b>\n"
                f"• 회차: <b>{self._escape(winning['round'])}회</b>\n"
                f"• 결과: <b>{result_message}</b>\n"
                f"• 당첨금: <b>{self._escape(winning['money'])}</b>\n"
                f"• 남은 잔액: <b>{self._escape(balance_str)}</b>\n"
                f"{result_block}"
            )
            self._send_message(destination, message)
        except KeyError:
            message = (
                "🎯 <b>연금복권 720+ 결과</b>\n"
                "• 결과: <b>🫠 다음 기회에</b>\n"
                f"• 남은 잔액: <b>{self._escape(balance_str)}</b>"
            )
            self._send_message(destination, message)

    def send_purchase_balance_shortage_message(
        self, lottery_name: str, balance: str, reason: str, destination: dict
    ) -> None:
        message = (
            "⚠️ <b>복권 구매 실패 - 잔액 부족</b>\n"
            f"• 대상: <b>{self._escape(lottery_name)}</b>\n"
            f"• 현재 잔액: <b>{self._escape(balance)}</b>\n"
            f"• 실패 사유: <b>{self._escape(reason)}</b>\n"
            "\n잔액 부족으로 인한 구매 실패는 알림 대상입니다. "
            "다음 자동 구매를 위해 예치금 충전을 확인해 주세요."
        )
        self._send_message(destination, message)

    def send_balance_low_message(self, balance: str, destination: dict) -> None:
        message = (
            "⚠️ <b>복권 예치금 충전 안내</b>\n"
            f"• 현재 잔액: <b>{self._escape(balance)}</b>\n"
            "• 매주 구매 기준 필요 금액: <b>10,000원</b>\n"
            "  - 로또 6/45: 주 5게임 5,000원\n"
            "  - 연금복권 720+: 주 5게임 5,000원\n"
            "\n잔액이 10,000원 이하입니다. 다음 주 자동 구매를 위해 충전을 권장합니다."
        )
        self._send_message(destination, message)

    def _is_balance_shortage(self, body: dict) -> bool:
        text = self._flatten_message(body).lower()
        balance_shortage_patterns = [
            ("잔액", "부족"),
            ("예치금", "부족"),
            ("deposit", "insufficient"),
            ("balance", "insufficient"),
            ("insufficient", "fund"),
            ("not enough", "balance"),
        ]
        return any(all(keyword in text for keyword in pattern) for pattern in balance_shortage_patterns)

    def _flatten_message(self, value) -> str:
        if isinstance(value, dict):
            return " ".join(self._flatten_message(v) for v in value.values())
        if isinstance(value, (list, tuple, set)):
            return " ".join(self._flatten_message(v) for v in value)
        return str(value)

    def _send_message(self, destination: dict, message: str) -> None:
        if not destination:
            print(f"[Info] Notification target not found. Message: {self._strip_html(message)}")
            return

        if destination.get("telegram_bot_token") and destination.get("telegram_chat_id"):
            self._send_telegram_message(
                destination["telegram_bot_token"],
                destination["telegram_chat_id"],
                message,
            )
            return

        webhook_url = destination.get("webhook_url")
        if not webhook_url:
            print(f"[Info] Webhook URL not found. Message: {self._strip_html(message)}")
            return

        payload = {"content": self._strip_html(message)}
        requests.post(webhook_url, json=payload, timeout=30)

    def _send_telegram_message(self, bot_token: str, chat_id: str, message: str) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        requests.post(url, data=payload, timeout=30)

    def _code_block(self, value: str) -> str:
        return f"\n<pre>{self._escape(value)}</pre>" if value else ""

    def _escape(self, value) -> str:
        return html.escape(str(value), quote=False)

    def _strip_html(self, value: str) -> str:
        return re.sub(r"</?(b|pre)>", "", value)
