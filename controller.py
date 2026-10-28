import os
import re
import sys
import time

from dotenv import load_dotenv

import auth
import lotto645
import notification
import win720

BALANCE_LOW_THRESHOLD = 10000


def _normalize_secret(value: str):
    if value and value.startswith("YOUR_"):
        return None
    return value


def _setup_and_login():
    load_dotenv(override=True)
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")

    slack_webhook_url = _normalize_secret(os.environ.get("SLACK_WEBHOOK_URL"))
    discord_webhook_url = _normalize_secret(os.environ.get("DISCORD_WEBHOOK_URL"))
    telegram_bot_token = _normalize_secret(os.environ.get("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id = _normalize_secret(os.environ.get("TELEGRAM_CHAT_ID"))

    webhook_url = slack_webhook_url or discord_webhook_url
    notification_destination = {
        "telegram_bot_token": telegram_bot_token,
        "telegram_chat_id": telegram_chat_id,
        "webhook_url": webhook_url,
    }

    auth_ctrl = auth.AuthController()
    auth_ctrl.login(username, password)

    return auth_ctrl, username, notification_destination


def buy_lotto645(authCtrl: auth.AuthController, cnt: int, mode: str):
    lotto = lotto645.Lotto645()
    _mode = lotto645.Lotto645Mode[mode.upper()]
    response = lotto.buy_lotto645(authCtrl, cnt, _mode)
    response["balance"] = authCtrl.get_user_balance()
    return response


def check_winning_lotto645(authCtrl: auth.AuthController) -> dict:
    lotto = lotto645.Lotto645()
    item = lotto.check_winning(authCtrl)
    item["balance"] = authCtrl.get_user_balance()
    return item


def buy_win720(authCtrl: auth.AuthController, username: str):
    pension = win720.Win720()
    response = pension.buy_Win720(authCtrl, username)
    if response is not None:
        response["balance"] = authCtrl.get_user_balance()
    return response


def check_winning_win720(authCtrl: auth.AuthController) -> dict:
    pension = win720.Win720()
    item = pension.check_winning(authCtrl)
    item["balance"] = authCtrl.get_user_balance()
    return item


# 알림 발송에 실패한 내역. 한 건이라도 있으면 프로세스를 실패로 종료해
# 워크플로가 빨간불이 되도록 한다. 알림이 조용히 끊기는 것을 막기 위함이다.
_NOTIFICATION_FAILURES = []


def send_message(mode: int, lottery_type: int, response: dict, destination: dict):
    if response is None:
        return

    notify = notification.Notification()

    try:
        if mode == 0:
            if lottery_type == 0:
                notify.send_lotto_winning_message(response, destination)
            else:
                notify.send_win720_winning_message(response, destination)
        elif mode == 1:
            if lottery_type == 0:
                notify.send_lotto_buying_message(response, destination)
            else:
                notify.send_win720_buying_message(response, destination)
    except Exception as e:
        # 알림 실패가 남은 구매 단계를 막아서는 안 되므로 여기서 멈추지 않는다.
        # 대신 기록해 두었다가 종료 코드로 드러낸다.
        print(f"[Error] 알림 발송 실패: {type(e).__name__}: {e}")
        _NOTIFICATION_FAILURES.append(f"{type(e).__name__}: {e}")


def _parse_balance_amount(balance: str):
    if not balance:
        return None

    matched = re.search(r"[\d,]+", str(balance))
    if not matched:
        return None

    return int(matched.group().replace(",", ""))


def _send_balance_low_message_if_needed(balance: str, destination: dict):
    balance_amount = _parse_balance_amount(balance)
    if balance_amount is None or balance_amount > BALANCE_LOW_THRESHOLD:
        return

    notify = notification.Notification()
    notify.send_balance_low_message(balance, destination)


def _run_buy_step(description: str, callback, destination: dict = None):
    """구매 단계를 실행하고, 실패하면 알림을 보낸 뒤 계속 진행한다.

    한 상품의 실패가 다른 상품 구매를 막지 않도록 예외는 삼키되,
    실패 사실 자체는 반드시 알린다. 예전에는 로그만 남겨서 봇이
    몇 주째 멈춰 있어도 알 수 없었다."""
    try:
        return callback()
    except Exception as e:
        print(f"[Warning] {description} 실패: {e}")
        if destination:
            try:
                notification.Notification().send_failure_message(
                    description, f"{type(e).__name__}: {e}", destination
                )
            except Exception as notify_error:
                print(f"[Error] 실패 알림 발송도 실패했습니다: {notify_error}")
        return None


def check():
    auth_ctrl, _, destination = _setup_and_login()

    lotto_response = check_winning_lotto645(auth_ctrl)
    send_message(0, 0, response=lotto_response, destination=destination)

    time.sleep(10)

    win720_response = check_winning_win720(auth_ctrl)
    send_message(0, 1, response=win720_response, destination=destination)

    balance = win720_response.get("balance") or lotto_response.get("balance")
    _send_balance_low_message_if_needed(balance, destination)


def buy():
    load_dotenv(override=True)
    count = int(os.environ.get("COUNT"))
    mode = "AUTO"

    auth_ctrl, username, destination = _setup_and_login()

    response = _run_buy_step(
        "로또 구매",
        lambda: buy_lotto645(auth_ctrl, count, mode),
        destination,
    )
    send_message(1, 0, response=response, destination=destination)

    time.sleep(10)

    auth_ctrl.http_client.session.cookies.clear()
    auth_ctrl, username, destination = _setup_and_login()

    response = _run_buy_step(
        "연금복권 구매",
        lambda: buy_win720(auth_ctrl, username),
        destination,
    )
    send_message(1, 1, response=response, destination=destination)


def lotto_buy():
    load_dotenv(override=True)
    count = int(os.environ.get("COUNT"))
    auth_ctrl, _, destination = _setup_and_login()
    mode = "AUTO"

    response = _run_buy_step(
        "로또 구매",
        lambda: buy_lotto645(auth_ctrl, count, mode),
        destination,
    )
    send_message(1, 0, response=response, destination=destination)


def win720_buy():
    auth_ctrl, username, destination = _setup_and_login()

    response = _run_buy_step(
        "연금복권 구매",
        lambda: buy_win720(auth_ctrl, username),
        destination,
    )
    send_message(1, 1, response=response, destination=destination)


def lotto_check():
    auth_ctrl, _, destination = _setup_and_login()

    response = check_winning_lotto645(auth_ctrl)
    send_message(0, 0, response=response, destination=destination)
    _send_balance_low_message_if_needed(response.get("balance"), destination)


def win720_check():
    auth_ctrl, _, destination = _setup_and_login()

    response = check_winning_win720(auth_ctrl)
    send_message(0, 1, response=response, destination=destination)
    _send_balance_low_message_if_needed(response.get("balance"), destination)


def heartbeat():
    """봇이 살아 있는지 확인하고 알린다.

    로그인과 알림 경로를 함께 점검하므로, 이 메시지가 끊기면 예약 실행이
    멈췄거나 계정/토큰에 문제가 생겼다는 신호가 된다. 구매는 하지 않는다."""
    auth_ctrl, _, destination = _setup_and_login()
    balance = auth_ctrl.get_user_balance()

    notification.Notification().send_heartbeat_message(
        f"• 로그인 정상\n• 현재 잔액: {balance}", destination
    )
    _send_balance_low_message_if_needed(balance, destination)


def run():
    if len(sys.argv) < 2:
        print("Usage: python controller.py [buy|check|heartbeat]")
        return

    if sys.argv[1] == "buy":
        buy()
    elif sys.argv[1] == "heartbeat":
        heartbeat()
    elif sys.argv[1] == "check":
        check()
    elif sys.argv[1] == "buy_lotto":
        lotto_buy()
    elif sys.argv[1] == "buy_win720":
        win720_buy()
    elif sys.argv[1] == "check_lotto":
        lotto_check()
    elif sys.argv[1] == "check_win720":
        win720_check()

    if _NOTIFICATION_FAILURES:
        print(
            f"[Error] 알림 {len(_NOTIFICATION_FAILURES)}건 발송 실패: "
            + " / ".join(_NOTIFICATION_FAILURES)
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
