# autostock-lite

한국 상장기업의 공시와 재무제표를 AI 도구에서 읽기 전용으로 조회하는 MCP 서버입니다.
Codex 같은 MCP 클라이언트에 연결하면 "삼성물산 최근 3년 매출 알려줘" 같은 질문에 OpenDART 공시를 근거로 답합니다.

- **필요한 키는 OpenDART 인증키 하나입니다.** 각자 무료로 발급받아 씁니다.
- **읽기 전용입니다.** 주문이나 계좌 기능은 없습니다.
- **투자 자문이 아닙니다.** 서버는 AI에게 매수·매도·보유를 권하지 말고, 목표가를 내지 말고, 도구가 돌려준 숫자만 쓰라고 안내합니다.

## 할 수 있는 것

| 도구 | 하는 일 |
|---|---|
| `resolve_symbol` | 회사 이름이나 종목코드로 종목을 찾고, 지금 코스피·코스닥에 상장돼 있는지 판정합니다 |
| `get_company_profile` | 회사 개황 |
| `get_financial_statements` | 사업보고서·분기보고서의 재무제표를 공시된 계정과 금액 그대로 가져옵니다 |
| `list_filings` | 기간별 공시 목록 |
| `get_filing_document`, `get_filing_section`, `diff_filing_sections` | 사업보고서·반기보고서·분기보고서·증권신고서 원문, 특정 장, 두 보고서의 같은 장 비교 |
| `get_major_shareholders`, `get_major_shareholder_changes`, `get_large_holdings`, `get_insider_holdings`, `get_shares_outstanding` | 최대주주, 지분 변동, 5% 대량보유, 임원·주요주주 소유, 발행주식 수 |
| `growth_metrics` | 입력한 숫자 계열로 전년 대비 증감률과 연평균 성장률을 계산합니다 |

토스증권 Open API 키가 있으면 시세, 일봉, 호가, 순위, 투자자별 매매 같은 시장 데이터 도구 14개가 더 나타납니다. 아래 "토스증권 키 (선택)"를 보세요.

## 준비물

- **Python 3.10 이상.** Windows라면 [python.org](https://www.python.org/downloads/)에서 설치하고, 설치 첫 화면에서 "Add python.exe to PATH"를 체크합니다.
- **Codex CLI** 또는 다른 MCP 클라이언트
- **OpenDART 인증키.** [OpenDART](https://opendart.fss.or.kr)에 가입한 뒤 "인증키 신청"에서 발급받습니다. 40자리 영문·숫자입니다.
  인증키는 한 사람에 하나만 발급되고, 다른 사람이 쓰게 해서는 안 됩니다(OpenDART 이용약관 제19조). 가족이라도 각자 발급받으세요.

## 설치

이 저장소를 내려받아 압축을 푼 폴더에서 아래 명령을 실행합니다.
전용 가상환경을 만들고 그 안에 설치하는 방식입니다.

Windows (PowerShell):

```powershell
python -m venv "$env:USERPROFILE\autostock-lite-venv"
& "$env:USERPROFILE\autostock-lite-venv\Scripts\python.exe" -m pip install .
```

macOS·Linux:

```bash
python3 -m venv ~/autostock-lite-venv
~/autostock-lite-venv/bin/python -m pip install .
```

## 키 넣기

OpenDART 키를 사용자 설정 파일에 한 줄로 적습니다. `여기에_발급받은_키` 부분을 실제 키로 바꿔서 실행하세요.

Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\autostock-lite" | Out-Null
Set-Content -Encoding utf8 "$env:APPDATA\autostock-lite\.env" "DART_API_KEY=여기에_발급받은_키"
```

macOS·Linux:

```bash
mkdir -p ~/.config/autostock-lite
printf 'DART_API_KEY=%s\n' '여기에_발급받은_키' > ~/.config/autostock-lite/.env
chmod 600 ~/.config/autostock-lite/.env
```

설정은 환경변수가 가장 먼저이고, 그다음 `--env-file`로 지정한 파일, 마지막으로 위 사용자 설정 파일 순서로 읽습니다.

## 연결 확인

```powershell
& "$env:USERPROFILE\autostock-lite-venv\Scripts\autostock-lite.exe" doctor
```

macOS·Linux에서는 `~/autostock-lite-venv/bin/autostock-lite doctor`입니다.
마지막 줄이 `doctor: PASS`이면 키와 DART 연결이 정상입니다. 키 값은 화면에 나오지 않습니다.

## Codex에 등록

Windows (PowerShell):

```powershell
codex mcp add autostock-lite -- "$env:USERPROFILE\autostock-lite-venv\Scripts\autostock-lite.exe" mcp
```

macOS·Linux:

```bash
codex mcp add autostock-lite -- ~/autostock-lite-venv/bin/autostock-lite mcp
```

`codex mcp get autostock-lite`로 등록 내용을 볼 수 있습니다. 이제 Codex를 새로 열고 질문하면 됩니다.

다른 MCP 클라이언트에서는 위 실행 파일을 `mcp` 인자와 함께 표준 입출력(stdio) 서버로 등록하면 됩니다.

## 질문 예시

질문 앞에 **"autostock-lite로"** 를 붙이세요. 붙이지 않으면 Codex가 이 도구 대신 웹 검색으로 답할 수 있습니다. 그러면 공시 원문이 아니라 웹 페이지가 근거가 됩니다.

- autostock-lite로 삼성물산의 최근 3개 사업연도 매출액과 영업이익 추이를 알려줘. 숫자마다 어느 공시에서 왔는지도 밝혀줘.
- autostock-lite로 005930 최근 사업보고서의 "사업의 내용"을 요약해 줘.
- autostock-lite로 삼성전자 최대주주와 최근 1년 지분 변동을 보여 줘.

처음 종목을 찾을 때는 DART 회사 목록을 내려받아 색인을 만들기 때문에 몇 초 더 걸립니다. 그다음부터는 저장된 색인을 씁니다.

재무제표와 공시 원문 도구는 응답이 깁니다. 한 질문에 여러 해나 여러 보고서를 물으면 그만큼 AI 사용량이 늘어납니다.

## 토스증권 키 (선택)

토스증권 계좌가 있으면 토스증권 웹(WTS)의 설정 > Open API에서 키를 발급받을 수 있습니다.
같은 메뉴에서 이 프로그램을 실행할 컴퓨터의 IP를 허용 IP로 등록해야 합니다. 등록되지 않은 IP에서 호출하면 403으로 막힙니다.

키가 있으면 설정 파일에 두 줄을 더 적습니다. 둘 중 하나만 있으면 설정 오류로 처리합니다.

```text
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
```

키가 있으면 종목 찾기도 토스 종목 정보로 상장 여부를 판정하므로 거래정지·정리매매 여부까지 알 수 있습니다.
키가 없으면 DART 회사개황의 법인 구분으로 판정하며, 이때는 거래정지 여부를 알 수 없다고 함께 표시합니다.

토스 토큰은 키 하나에 하나만 유효합니다. 그래서 같은 키로 서버를 두 개 띄우면 두 번째 서버는 시작하지 않고 멈춥니다.
예를 들어 Codex 창을 두 개 열면 두 번째 창에서는 이 도구들이 보이지 않습니다. 토스 키가 없으면 이 제한도 없습니다.

토스 키를 쓸 때 알아 둘 것:

- **토스 키는 주문 API에도 쓰이는 자격입니다.** 토스 문서에 조회 전용 권한이 따로 나와 있지 않습니다. 이 프로그램은 조회만 하지만, 키가 새면 위험은 같습니다. 설정 파일 밖으로 옮기거나 남에게 보여 주지 마세요.
- **토스증권 Open API의 데이터 이용 정책**은 API로 받은 정보를 투자자 본인의 매매 목적으로만 쓰도록 합니다. 상업적 이용을 금지하고, 비상업 용도라도 제3자에게 배포하는 것을 금지합니다. 조회 결과를 다른 사람에게 퍼뜨리지 마세요.
- 키 발급 때 동의한 토스증권 Open API 이용약관을 직접 확인하세요.

## 저장 위치와 삭제

| 내용 | Windows | macOS·Linux |
|---|---|---|
| 설정 파일 (키) | `%APPDATA%\autostock-lite\.env` | `~/.config/autostock-lite/.env` |
| 회사 목록 색인 | `%LOCALAPPDATA%\autostock-lite\` | `~/.local/state/autostock-lite/` |

모두 지우려면 `codex mcp remove autostock-lite`를 실행한 뒤 가상환경 폴더와 위 두 폴더를 삭제합니다.

## 주의

- 이 프로그램은 금융감독원(OpenDART)이나 토스증권이 만들거나 보증한 것이 아닙니다.
- 이 프로그램이 보여 주는 정보는 참고용입니다. 투자 권유나 자문이 아니며, 투자 판단과 그 결과의 책임은 이용자에게 있습니다.
- 데이터는 금융감독원 OpenDART와, 키를 설정한 경우 토스증권 Open API에서 옵니다. 공시정보는 제출한 회사가 책임지고 작성하며, 금융감독원은 그 정확성과 완전성을 보장하지 않습니다(OpenDART 이용약관 제23조). 원천 데이터의 오류나 지연이 그대로 반영될 수 있습니다.
- 키는 각자 발급받아 씁니다. 각 서비스의 이용약관을 확인하고 지켜 주세요. 이 저장소는 키를 받거나 보관하지 않으며, 저장소에는 두 서비스에서 받은 데이터가 들어 있지 않습니다. 테스트는 손으로 만든 데이터만 씁니다.
- OpenDART는 키마다 호출 허용량이 있습니다. 넘으면 오류 코드 020이 나옵니다. 개발 가이드는 보통 20,000건 이상 요청하면 이 오류가 난다고 안내합니다.
- AI가 도구 결과를 잘못 옮길 수 있습니다. 중요한 숫자는 답에 함께 나오는 공시 링크로 원문을 확인하세요.

## 라이선스

MIT. [LICENSE](LICENSE)를 보세요.
