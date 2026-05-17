from playwright.sync_api import sync_playwright
import random
import time
import datetime as dt
import os
import re
from pathlib import Path
from typing import Optional, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()

kakao_id = os.getenv("KAKAO_ID")
kakao_pw = os.getenv("KAKAO_PW")
card_num = os.getenv("CARD_NUM")
card_name = os.getenv("CARD_NAME")
card_birth = os.getenv("CARD_BIRTH")
card_exp = os.getenv("CARD_EXP")
pay_amount = os.getenv("PAY_AMOUNT", "5999")
tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

login_url = "https://www.lguplus.com/login"
pay_url = "https://www.lguplus.com/mypage/payinfo?p=1"

MAX_LOGIN_ATTEMPTS = 2
SMS_VALID_SECONDS = 170          # LG U+ SMS 유효시간(180초)보다 10초 짧게
OTP_WAIT_SECONDS = SMS_VALID_SECONDS  # 폴링 타임아웃 = SMS 유효시간
WARN_BEFORE_TIMEOUT = 30         # 타임아웃 n초 전 경고 발송
MAX_SMS_RESENDS = 3              # 세션 내 "다시" 재전송 최대 횟수
MAX_REAUTH_ATTEMPTS = 2          # 세션 만료/타임아웃 후 재인증 최대 횟수

RETRY_KEYWORDS = {"다시", "재전송", "retry", "resend"}
CANCEL_KEYWORDS = {"/cancel", "취소", "중단"}
STATUS_KEYWORDS = {"/status", "상태"}

SCRIPT_START_TS = int(time.time())
DEBUG_DIR = Path(__file__).parent / "debug_runtime"

# LG U+ API 응답 캐치 — 결제 성공/실패 판정의 최종 근거
# (페이지 텍스트 매칭은 false positive 발생 사례 있었음)
#   PUT  /uhdc/fo/mbrm/auth/mobile/v1/auth-num:verify     → {"isSuccess":"Y","message":...}
#   POST /uhdc/fo/myin/blpy/recp/v1/online-payment-comple-auth
#        → {"rsltYn":"Y","messageCode":"00","message":"요금이 납부되었습니다."}
API_STATE = {"otp_verify": None, "payment": None}


def _make_api_handler(state: dict):
    def handler(resp):
        try:
            url = resp.url
            method = resp.request.method
            if "auth-num:verify" in url and method == "PUT":
                body = resp.json()
                state["otp_verify"] = body
                print(
                    f"[API] OTP verify isSuccess={body.get('isSuccess')} "
                    f"msg={body.get('message')}"
                )
            elif "online-payment-comple-auth" in url and method == "POST":
                body = resp.json()
                state["payment"] = body
                print(
                    f"[API] 결제 rsltYn={body.get('rsltYn')} "
                    f"code={body.get('messageCode')} msg={body.get('message')}"
                )
        except Exception as e:
            print(f"[API] response parse fail: {e}")
    return handler


# ============================================================
# 알림 / 디버그
# ============================================================
def send_telegram(message: str) -> bool:
    if not tg_token or not tg_chat_id:
        print("[!] Telegram 설정 없음")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": int(tg_chat_id), "text": message},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        print(f"[!] Telegram 전송 오류: {e}")
        return False


def dump_debug(page, prefix: str):
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        page.screenshot(path=str(DEBUG_DIR / f"{ts}_{prefix}.png"), full_page=True)
        (DEBUG_DIR / f"{ts}_{prefix}.html").write_text(page.content(), encoding="utf-8")
        print(f"[디버그] 덤프 저장: {ts}_{prefix}")
    except Exception as e:
        print(f"[디버그] 덤프 실패: {e}")


# ============================================================
# Telegram long-polling
# ============================================================
def _tg_get_latest_update_id() -> int:
    try:
        r = requests.get(f"https://api.telegram.org/bot{tg_token}/getUpdates", timeout=10)
        updates = r.json().get("result", [])
        if updates:
            return updates[-1]["update_id"]
    except Exception as e:
        print(f"[!] getUpdates 초기화 오류: {e}")
    return 0


def wait_for_otp_or_retry(
    timeout: int = OTP_WAIT_SECONDS,
    baseline: Optional[int] = None,
    status_text: str = "",
) -> Tuple[str, Optional[str]]:
    """Telegram 폴링. 반환값:
       ('code',    '123456') — 6자리 OTP 수신
       ('retry',   None)     — 사용자가 '다시' 계열 키워드 전송
       ('cancel',  None)     — 사용자가 '/cancel' / '취소' 전송
       ('timeout', None)     — 타임아웃

       우선순위: cancel > code(6자리) > retry > status(메타)
       엄격 일치: 'retry' / 'cancel' / 'status' 는 strip 후 완전일치만 인정.
    """
    if not tg_token or not tg_chat_id:
        print("[!] Telegram 설정 없음")
        return ("timeout", None)
    if baseline is None:
        baseline = _tg_get_latest_update_id()

    chat_id_int = int(tg_chat_id)
    start = time.time()
    last_id = baseline
    warned = False
    print(f"[OTP] 폴링 (baseline={baseline}, timeout={timeout}s)")

    while True:
        elapsed = time.time() - start
        remain = timeout - elapsed
        if remain <= 0:
            print("[OTP] 타임아웃")
            return ("timeout", None)

        if not warned and remain <= WARN_BEFORE_TIMEOUT:
            send_telegram(
                f"⚠️ {int(remain)}초 후 타임아웃됩니다.\n"
                "응답 없으면 자동으로 재인증을 진행합니다."
            )
            warned = True

        try:
            r = requests.get(
                f"https://api.telegram.org/bot{tg_token}/getUpdates",
                params={"offset": last_id + 1, "timeout": min(25, max(1, int(remain)))},
                timeout=30,
            )
            data = r.json()
            for upd in data.get("result", []):
                last_id = max(last_id, upd["update_id"])
                msg = upd.get("message") or upd.get("edited_message") or {}
                if msg.get("chat", {}).get("id") != chat_id_int:
                    continue
                if msg.get("date", 0) < SCRIPT_START_TS:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                lowered = text.lower()

                if lowered in CANCEL_KEYWORDS:
                    print(f"[OTP] 취소 요청: {text[:60]}")
                    return ("cancel", None)

                m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                if m:
                    code = m.group(1)
                    print(f"[OTP] 수신: {code} (원문: {text[:60]})")
                    return ("code", code)

                if lowered in RETRY_KEYWORDS:
                    print(f"[OTP] 재전송 요청: {text[:60]}")
                    return ("retry", None)

                if lowered in STATUS_KEYWORDS:
                    send_telegram(
                        f"🔎 현재 상태: {status_text}\n경과 {int(elapsed)}s / {timeout}s"
                    )
                    continue

                print(f"[OTP] 무시: {text[:60]}")
        except Exception as e:
            print(f"[!] getUpdates 오류: {e}")
            time.sleep(2)


# ============================================================
# 팝업
# ============================================================
def _find_top_info_modal(page):
    """화면에 보이는 BootstrapVue 모달 중 z-index 최상단의 '안내 팝업'을 반환.
    안내 팝업 = '확인' 버튼은 있고 '취소' 버튼은 없는 것 (입력 모달 제외)."""
    try:
        modals = page.evaluate(
            """
            () => {
                const outers = Array.from(document.querySelectorAll('[id$="___BV_modal_outer_"]'));
                return outers
                    .filter(o => {
                        if (o.offsetParent === null) return false;
                        const inner = o.querySelector('.modal.show, .modal[style*="display: block"]');
                        return !!inner;
                    })
                    .map(o => ({
                        id: o.id,
                        z: parseInt(getComputedStyle(o).zIndex) || 0,
                        text: (o.innerText || '').slice(0, 400),
                    }));
            }
            """
        )
    except Exception as e:
        print(f"[팝업] modal 조회 실패: {e}")
        return None
    if not modals:
        return None
    modals.sort(key=lambda m: -m["z"])
    for m in modals:
        mid = m["id"]
        ok_count = page.locator(f'#{mid} button:has-text("확인")').count()
        cancel_count = page.locator(f'#{mid} button:has-text("취소")').count()
        if ok_count > 0 and cancel_count == 0:
            return mid
    return None


def dismiss_info_popup(page, max_wait_ms: int = 5000, poll_ms: int = 300) -> bool:
    end = time.time() + max_wait_ms / 1000
    while time.time() < end:
        mid = _find_top_info_modal(page)
        if mid:
            try:
                page.locator(f'#{mid} button:has-text("확인")').first.click()
                page.wait_for_timeout(700)
                print(f"[팝업] 안내 모달 #{mid} 확인 클릭")
                return True
            except Exception as e:
                print(f"[팝업] #{mid} 클릭 실패: {e}")
                break
        time.sleep(poll_ms / 1000)
    return False


# ============================================================
# 로그인 / 결제 진입
# ============================================================
def login_and_open_pay(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    page = context.new_page()
    page.on("response", _make_api_handler(API_STATE))
    try:
        delay_login = random.randrange(5, 7)
        delay_pay_open = random.randrange(5, 10)

        page.goto(login_url, wait_until="domcontentloaded")
        print(f"{page.url}에 접속")
        page.wait_for_timeout(3000)

        page.locator(
            'button:has(img[alt*="카카오"]), button:has(img[src*="kakao"])'
        ).first.click(timeout=10000)
        print("카카오 로그인 버튼 클릭")
        page.wait_for_timeout(3000)

        page.fill("#loginId--1", kakao_id)
        page.fill("#password--2", kakao_pw)
        page.locator('button[type="submit"], button:has-text("로그인")').first.click()
        print("로그인 제출")
        time.sleep(delay_login)

        if "login" in page.url.lower():
            print("로그인 리다이렉트 대기")
            page.wait_for_timeout(3000)

        page.goto(pay_url, wait_until="domcontentloaded")
        print("결제 페이지 접속")
        page.wait_for_timeout(3000)

        try:
            page.locator('button:has-text("닫기")').first.click(timeout=3000)
            page.wait_for_timeout(2000)
            print("[+] 팝업 닫기")
        except Exception:
            print("[-] 팝업 없음")

        pay_btn = page.locator(
            'button:has-text("요금바로납부"), button:has-text("납부")'
        ).first
        pay_btn.wait_for(state="visible", timeout=30000)
        pay_btn.click()
        print(f"요금바로납부 클릭 후 {delay_pay_open}초 대기")
        time.sleep(delay_pay_open)
        return browser, context, page
    except Exception:
        try:
            browser.close()
        except Exception:
            pass
        raise


def fill_card_and_submit(page):
    try:
        page.locator("#cardNo").wait_for(state="visible", timeout=5000)
    except Exception:
        msg = "카드사 자동결제 처리날이라 프로세스를 종료합니다."
        print(msg)
        send_telegram("✅ 카드사 자동결제 처리일 — 스크립트 정상 종료")
        return False

    page.fill("#cardNo", card_num); print("카드번호 입력"); page.wait_for_timeout(500)
    page.fill('[name="cardCustName"]', card_name); print("카드 소유자 이름"); page.wait_for_timeout(500)
    page.fill('[name="cardCustbirth"]', card_birth); print("카드 소유자 생일"); page.wait_for_timeout(500)
    page.fill("#selCardDate-1", card_exp); print("카드 유효기간"); page.wait_for_timeout(500)
    pay_input = page.locator("#displayPayAmt")
    pay_input.click(); pay_input.fill(pay_amount)
    print(f"결제금액({pay_amount}원)"); page.wait_for_timeout(500)
    page.select_option("#cardMonth", value="0"); print("일시불"); page.wait_for_timeout(500)

    page.locator('button:has-text("납부"), button:has-text("결제")').last.click()
    print("결제(납부) 버튼 클릭 → 본인인증 모달 대기")
    time.sleep(3)
    return True


# ============================================================
# 본인인증 — SMS 발송 / 재전송 / 재인증
# ============================================================
def _click_send_sms(page) -> bool:
    """'인증번호 받기' 버튼 클릭. 초기 발송 & 재인증 공용."""
    try:
        btn = page.locator('button:has-text("인증번호 받기")').first
        btn.wait_for(state="visible", timeout=5000)
        btn.click()
        dismiss_info_popup(page, max_wait_ms=5000)
        return True
    except Exception as e:
        print(f"[SMS] '인증번호 받기' 클릭 실패: {e}")
        return False


def _click_resend_sms(page) -> bool:
    """'재전송' 버튼 클릭. SMS 세션이 살아있을 때만 유효."""
    try:
        btn = page.locator('button:has-text("재전송")').first
        btn.wait_for(state="visible", timeout=5000)
        btn.click()
        dismiss_info_popup(page, max_wait_ms=5000)
        return True
    except Exception as e:
        print(f"[SMS] '재전송' 클릭 실패: {e}")
        return False


def _open_phone_auth_modal(page):
    """본인인증 모달 초기 설정: 휴대폰 라디오 → 휴대폰으로 인증하기 → 전체동의."""
    page.locator('label:has(span.txt:has-text("휴대폰"))').first.click()
    print("[인증] '휴대폰' 라디오 선택")
    page.wait_for_timeout(800)

    page.locator('button:has-text("휴대폰으로 인증하기")').first.click()
    print("[인증] '휴대폰으로 인증하기' 클릭")
    time.sleep(3)

    page.locator('label:has-text("전체동의")').first.click()
    print("[인증] '전체동의' 체크")
    page.wait_for_timeout(800)


def _restart_phone_auth(page) -> bool:
    """SMS 세션 만료 후 재인증. 전략:
       1) 현재 모달에서 '재전송' 버튼 재시도
       2) 실패 시 ESC 후 납부 버튼 재클릭 → 모달 초기화부터 다시
    """
    dump_debug(page, "reauth_start")

    if _click_resend_sms(page):
        print("[재인증] '재전송' 버튼으로 SMS 재발송")
        return True

    print("[재인증] '재전송' 실패 → 납부 버튼부터 재진입 시도")
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(1000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

        page.locator('button:has-text("납부"), button:has-text("결제")').last.click()
        print("[재인증] 납부 버튼 재클릭")
        time.sleep(3)
        _open_phone_auth_modal(page)
        return _click_send_sms(page)
    except Exception as e:
        print(f"[재인증] 실패: {e}")
        dump_debug(page, "reauth_fail")
        return False


def _enter_otp_and_confirm(page, otp: str):
    # OTP 입력 필드를 포함하는 모달로 확인 버튼 클릭 스코프 좁히기 (다른 모달의 확인 오클릭 방지)
    auth_modal_id = page.evaluate(
        """
        () => {
            const inp = document.querySelector('#uplus-autnNo');
            if (!inp) return null;
            const m = inp.closest('[id$="___BV_modal_outer_"]');
            return m ? m.id : null;
        }
        """
    )

    otp_input = page.locator("#uplus-autnNo")
    otp_input.wait_for(state="visible", timeout=5000)
    otp_input.click()
    otp_input.fill(otp)
    print(f"[인증] 인증번호 입력: {otp}")
    page.wait_for_timeout(1000)

    if auth_modal_id:
        confirm = page.locator(
            f'#{auth_modal_id} button.c-btn-solid-1-m:has-text("확인")'
        ).last
    else:
        confirm = page.locator('button.c-btn-solid-1-m:has-text("확인")').last
    for _ in range(10):
        if confirm.is_enabled():
            break
        page.wait_for_timeout(500)

    # OTP 검증 응답 대기를 위해 이전 결과 초기화 후 클릭
    API_STATE["otp_verify"] = None
    confirm.click()
    print("[인증] 확인 클릭 (OTP 검증)")

    # OTP 검증 API 응답 대기 (최대 15초)
    # Playwright sync API: 이벤트는 다른 Playwright 작업 중에만 발화 →
    # page.wait_for_timeout() 으로 이벤트 펌프를 유지해야 핸들러가 호출됨
    end = time.time() + 15
    while time.time() < end and API_STATE.get("otp_verify") is None:
        page.wait_for_timeout(300)
    verify = API_STATE.get("otp_verify")
    if verify is None:
        print("[인증] OTP 검증 API 응답 미수신 (계속 진행)")
    elif verify.get("isSuccess") != "Y":
        msg = verify.get("message") or "응답 메시지 없음"
        raise RuntimeError(f"OTP 검증 실패 (API): isSuccess={verify.get('isSuccess')} / {msg}")
    else:
        print("[인증] OTP 검증 성공 (API isSuccess=Y)")


def _notify_sms_sent(prefix: str):
    now_str = dt.datetime.now().strftime("%H:%M:%S")
    send_telegram(
        f"📲 {prefix}\n"
        f"시각: {now_str}  (유효 {SMS_VALID_SECONDS}초)\n"
        "받으신 6자리 인증번호를 이 채팅에 보내주세요.\n"
        "('다시' 재전송 / '취소' 중단 / '상태' 조회)"
    )


def handle_phone_auth(page):
    """본인인증 플로우 (재전송/재인증 자동 처리)."""
    _open_phone_auth_modal(page)

    if not _click_send_sms(page):
        dump_debug(page, "initial_send_fail")
        raise RuntimeError("최초 SMS 발송 버튼 클릭 실패")
    sms_sent_at = time.time()
    _notify_sms_sent("LG U+ 휴대폰 본인인증 SMS 발송됨")

    resend_count = 0
    reauth_count = 0
    otp = None

    while True:
        elapsed = time.time() - sms_sent_at
        status_text = (
            f"OTP 대기 (경과 {int(elapsed)}s, "
            f"재전송 {resend_count}/{MAX_SMS_RESENDS}, "
            f"재인증 {reauth_count}/{MAX_REAUTH_ATTEMPTS})"
        )
        kind, value = wait_for_otp_or_retry(
            timeout=OTP_WAIT_SECONDS, status_text=status_text
        )

        if kind == "code":
            otp = value
            break

        if kind == "cancel":
            dump_debug(page, "user_cancel")
            send_telegram("🛑 사용자 취소 — 스크립트 중단합니다.")
            raise RuntimeError("사용자가 취소 요청")

        elapsed = time.time() - sms_sent_at
        session_alive = elapsed < SMS_VALID_SECONDS

        if kind == "retry":
            if session_alive and resend_count < MAX_SMS_RESENDS:
                if _click_resend_sms(page):
                    resend_count += 1
                    sms_sent_at = time.time()
                    _notify_sms_sent(f"재전송 완료 ({resend_count}/{MAX_SMS_RESENDS})")
                    continue
                print("[인증] '재전송' 클릭 실패 → 재인증으로 폴백")
            else:
                reason = "세션 만료" if not session_alive else "재전송 한도 도달"
                print(f"[인증] {reason} → 재인증 진행")
                send_telegram(f"🔁 {reason} — 재인증을 진행합니다.")

            if reauth_count >= MAX_REAUTH_ATTEMPTS:
                dump_debug(page, "reauth_limit")
                raise RuntimeError("재인증 한도 초과")
            if _restart_phone_auth(page):
                reauth_count += 1
                sms_sent_at = time.time()
                _notify_sms_sent(
                    f"재인증 완료 ({reauth_count}/{MAX_REAUTH_ATTEMPTS}) — 새 SMS 발송"
                )
                continue
            raise RuntimeError("재인증 실패")

        if kind == "timeout":
            print("[인증] 타임아웃 → 자동 재인증")
            send_telegram("⏰ 타임아웃 — 자동으로 재인증을 진행합니다.")
            if reauth_count >= MAX_REAUTH_ATTEMPTS:
                dump_debug(page, "timeout_limit")
                raise RuntimeError("타임아웃 & 재인증 한도 초과")
            if _restart_phone_auth(page):
                reauth_count += 1
                sms_sent_at = time.time()
                _notify_sms_sent(
                    f"자동 재인증 ({reauth_count}/{MAX_REAUTH_ATTEMPTS}) — 새 SMS 발송"
                )
                continue
            raise RuntimeError("자동 재인증 실패")

    dismiss_info_popup(page, max_wait_ms=3000)

    # 결제 API 응답 대기를 위해 이전 결과 초기화
    API_STATE["payment"] = None

    _enter_otp_and_confirm(page, otp)

    # __BVID__844 "휴대폰 인증이 완료되었습니다" 팝업 닫기 → 자동으로 결제 API 호출됨
    auth_complete = dismiss_info_popup(page, max_wait_ms=15000)
    if not auth_complete:
        print("[경고] 인증완료 팝업 미감지 — 디버그 덤프 저장")
        dump_debug(page, "auth_complete_popup_miss")

    # 결제 API (online-payment-comple-auth) 응답 대기 (최대 30초)
    # rsltYn / message 가 결제 성공/실패의 최종 근거
    print("[결제] 결제 처리 응답 대기 중...")
    end = time.time() + 30
    while time.time() < end and API_STATE.get("payment") is None:
        page.wait_for_timeout(500)
    payment = API_STATE.get("payment")
    if payment is None:
        print("[결제] 30초 내 결제 API 응답 미수신")
        dump_debug(page, "payment_api_timeout")
    else:
        print(
            f"[결제] API 응답 수신: rsltYn={payment.get('rsltYn')} "
            f"code={payment.get('messageCode')} message={payment.get('message')!r}"
        )

    # 결제 결과 팝업 (예: "요금이 납부되었습니다") 닫기 — 없어도 OK
    dismiss_info_popup(page, max_wait_ms=10000)
    page.wait_for_timeout(2000)


# ============================================================
# 결제 완료 검증
# ============================================================
def verify_payment_success(page) -> Tuple[bool, str]:
    """결제 성공 여부 + 사유 메시지 반환.
    우선순위:
      1) 결제 API 응답 (online-payment-comple-auth) — rsltYn=Y/N 가 진실
      2) OTP 검증 API 응답 — isSuccess=N 이면 결제 자체 미진행
      3) 페이지 텍스트 매칭 (백업)
    페이지에 단서가 없으면 미확정(False) — 인증 모달 사라짐을 성공으로 간주하던 폴백은 제거됨.
    """
    payment = API_STATE.get("payment")
    if payment:
        rsltYn = payment.get("rsltYn")
        message = payment.get("message") or ""
        code = payment.get("messageCode")
        if rsltYn == "Y":
            return (True, f"API rsltYn=Y · {message}")
        return (False, f"API rsltYn={rsltYn} · code={code} · {message}")

    verify = API_STATE.get("otp_verify")
    if verify and verify.get("isSuccess") != "Y":
        return (False, f"OTP 검증 실패: {verify.get('message')}")

    success_texts = [
        "요금이 납부되었습니다",
        "납부가 완료",
        "결제가 완료",
        "납부 완료",
        "결제 완료",
        "정상 처리",
    ]
    for txt in success_texts:
        if page.locator(f'text="{txt}"').count() > 0:
            return (True, f"페이지 텍스트 매칭(성공): '{txt}'")

    failure_texts = [
        "결제가 실패",
        "결제 실패",
        "납부 실패",
        "거절",
        "한도 초과",
        "한도초과",
        "잔액 부족",
        "잔액부족",
        "처리되지 않",
    ]
    for txt in failure_texts:
        if page.locator(f'text="{txt}"').count() > 0:
            return (False, f"페이지 텍스트 매칭(실패): '{txt}'")

    return (False, "결제 결과 미확정 — API 응답 없음 & 텍스트 매칭 실패")


# ============================================================
# Main
# ============================================================
with sync_playwright() as p:
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    send_telegram(
        f"📞 LG U+ 자동납부 시작\n"
        f"시각: {now_str}\n"
        "본인인증 SMS 수신 예정입니다. 대기해주세요.\n"
        "(명령: '다시' 재전송 / '취소' 중단 / '상태' 조회)"
    )

    browser = context = page = None

    # 1) 로그인 ~ 납부 버튼 클릭까지 (재시도 가능)
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        try:
            browser, context, page = login_and_open_pay(p)
            break
        except Exception as e:
            print(f"[시도 {attempt}/{MAX_LOGIN_ATTEMPTS} 실패] {e}")
            now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if attempt < MAX_LOGIN_ATTEMPTS:
                send_telegram(
                    f"🔄 {attempt}차 시도 실패 → 재시도\n"
                    f"오류: {e}\n시각: {now}"
                )
                time.sleep(5)
            else:
                send_telegram(
                    f"❌ 모든 재시도 실패 ({MAX_LOGIN_ATTEMPTS}회)\n"
                    f"마지막 오류: {e}\n시각: {now}"
                )
                raise SystemExit(1)

    # 2) 카드 정보 입력 + 납부 + 본인인증 + 검증 (재시도 X, 이중결제 방지)
    try:
        proceed = fill_card_and_submit(page)
        if not proceed:
            browser.close()
            quit()

        handle_phone_auth(page)

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success, detail = verify_payment_success(page)
        if success:
            send_telegram(f"✅ 납부 완료: {pay_amount}원 ({now})\n{detail}")
        else:
            send_telegram(
                f"⚠️ 결제 미확정/실패 — 카드사/LG U+ 수동 확인 필요\n"
                f"세부: {detail}\n"
                f"확인 시각: {now}"
            )
            try:
                dump_debug(page, "verify_unconfirmed")
            except Exception:
                pass

    except Exception as exc:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        send_telegram(
            f"❌ 결제/인증 오류 (중복결제 방지를 위해 재시도하지 않음)\n"
            f"오류: {exc}\n시각: {now}"
        )
        try:
            if page:
                dump_debug(page, "fatal_error")
        except Exception:
            pass
        print(f"[ERROR] {exc}")

    finally:
        try:
            browser.close()
        except Exception:
            pass
        print("브라우저 종료")
