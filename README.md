# LG U+ 요금 자동납부 (Playwright)

카카오 로그인 → LG U+ 결제페이지 → 카드정보 입력 → 휴대폰 본인인증(SMS OTP) 까지 자동으로 처리하는 개인용 월납 자동화 스크립트.

OTP 수신·재전송·취소 등 모든 상호작용과 실행 결과 알림은 **Telegram 봇** 하나로 처리됩니다.

> ⚠️ **면책** — 본인 명의·본인 카드로 **본인 계정** 에 자동납부하기 위한 개인용 템플릿입니다. 타인 계정 자동화, 크롤링, 약관 위반 용도 등으로 사용하지 마세요. 사이트 구조 변경 시 동작하지 않을 수 있고, 이중결제 등 오작동 책임은 사용자에게 있습니다.

---

## 기능

- 카카오 로그인 → 요금바로납부 → 카드정보 입력 → 납부 버튼 클릭
- **휴대폰 본인인증 SMS OTP** 자동 처리
  - 수신된 SMS 를 Telegram 에 붙여넣으면 6자리 인증번호를 자동 인식해서 입력
  - `다시` / `재전송` / `retry` / `resend` → 재전송 버튼 자동 클릭 (최대 3회)
  - SMS 세션 만료(170초) 또는 타임아웃 시 **자동 재인증** (최대 2회)
  - `취소` / `/cancel` → 스크립트 중단
  - `상태` / `/status` → 현재 진행 상황 조회
- 카드사 **자동결제일** 이면 입력란이 나타나지 않음 → 정상 종료
- 로그인~납부버튼까지만 재시도(2회). **카드정보 입력 이후는 재시도하지 않음** (이중결제 방지).
- 실패/미확정 시 스크린샷 + HTML 을 `debug_runtime/` 에 저장
- Telegram 에 단계별 진행 안내 + 성공/재시도/실패 알림

## 요구사항

- Python 3.10+
- Chromium (Playwright 에서 자동 설치)
- 텔레그램 봇 토큰 + 채팅 ID

## 설치

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>

python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 설정

1. `.env.example` 을 복사해 `.env` 파일을 만듭니다.

   ```bash
   cp .env.example .env
   ```

2. `.env` 값을 채웁니다.

   | Key | 설명 |
   |-----|------|
   | `KAKAO_ID` / `KAKAO_PW` | 카카오 계정 (LG U+ 연동용) |
   | `CARD_NUM` / `CARD_NAME` / `CARD_BIRTH` / `CARD_EXP` | 결제 카드 정보 |
   | `PAY_AMOUNT` | 납부 금액 (기본 5999) |
   | `TELEGRAM_BOT_TOKEN` | `@BotFather` 로 발급 |
   | `TELEGRAM_CHAT_ID` | `@userinfobot` 로 확인 |

3. Telegram 봇을 만들고 본인 계정에 `/start` 를 보내 채팅이 열려있어야 합니다.

## 실행

```bash
python lguplus_auto_pay_naver.py
```

스크립트가 시작되면 Telegram 에 "자동납부 시작" 알림이 옵니다. 본인인증 SMS 가 수신되면 **Telegram 채팅에 문자 전체를 붙여넣기** 하거나 **6자리 숫자만** 보내세요. 나머지는 자동 처리됩니다.

## Cron 예시 (매일 오전 9시)

```cron
0 9 * * * cd /home/user/lguplus_auto_pay && /home/user/lguplus_auto_pay/venv/bin/python lguplus_auto_pay_naver.py >> /home/user/lguplus_auto_pay/cron.log 2>&1
```

## Telegram 명령어

| 입력 | 동작 |
|------|------|
| `123456` (6자리 숫자) | OTP 로 입력, 본인인증 완료 |
| `[Web발신] ... 인증번호[123456] ...` | SMS 전체 붙여넣기 OK (6자리 자동 추출) |
| `다시` / `재전송` / `retry` / `resend` | SMS 재전송 (세션 유효 시) / 세션 만료 시 재인증 |
| `취소` / `중단` / `/cancel` | 스크립트 중단 |
| `상태` / `/status` | 현재 진행 단계 회신 |

## 트러블슈팅

- **셀렉터 못 찾음** — LG U+ DOM 이 바뀌었을 수 있습니다. `debug_runtime/*.png` / `*.html` 확인 후 셀렉터 수정.
- **OTP 타임아웃** — 170초 내에 6자리를 보내지 않으면 자동 재인증이 1회 진행됩니다. 재인증도 실패하면 수동 납부 필요.
- **카카오 2단계 인증** — 카카오 계정에 2FA 가 걸려있으면 로그인 단계에서 막힙니다. 서비스 계정 또는 앱 비밀번호로 분리 권장.
- **headless 차단** — `launch(headless=True)` 에서 탐지되면 `False` 로 전환해 원인 분석.

## 라이선스

MIT (원하는 경우 변경)
