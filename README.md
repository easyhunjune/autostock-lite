# autostock-lite

한국 상장기업의 공시와 재무제표를 AI 도구에서 읽기 전용으로 조회하는 MCP 서버입니다.
Codex 등의 MCP 클라이언트에 연결하면 "삼성물산 최근 3년 매출 알려줘"와 같은 질문에 OpenDART 공시를 근거로 답합니다.

- **필요한 키는 OpenDART 인증키 하나입니다.** 이용자가 각자 무료로 발급받아 사용합니다.
- **읽기 전용입니다.** 주문이나 계좌 기능은 제공하지 않습니다.
- **투자 자문이 아닙니다.** 서버는 AI가 매수, 매도, 보유를 권하거나 목표가를 제시하지 않고, 도구가 반환한 숫자만 사용하도록 안내합니다.

## 할 수 있는 것

| 도구 | 하는 일 |
|---|---|
| `resolve_symbol` | 회사 이름이나 종목코드로 종목을 찾고, 현재 코스피와 코스닥 상장 여부를 판정합니다 |
| `get_company_profile` | 회사 개황 |
| `get_financial_statements` | 사업보고서와 분기보고서의 재무제표를 공시된 계정과 금액 그대로 가져옵니다 |
| `list_filings` | 기간별 공시 목록 |
| `get_filing_document`, `get_filing_section`, `diff_filing_sections` | 사업보고서, 반기보고서, 분기보고서, 증권신고서 원문 및 특정 장 조회, 두 보고서의 동일 장 비교 |
| `get_major_shareholders`, `get_major_shareholder_changes`, `get_large_holdings`, `get_insider_holdings`, `get_shares_outstanding` | 최대주주, 지분 변동, 5% 대량보유, 임원과 주요주주 소유, 발행주식 수 |
| `growth_metrics` | 입력한 숫자 계열로 전년 대비 증감률과 연평균 성장률을 계산합니다 |

토스증권 Open API 키를 등록하면 시세, 일봉, 호가, 순위, 투자자별 매매와 같은 시장 데이터 도구 14개가 추가로 활성화됩니다. 세부 내용은 아래 "토스증권 키 (선택)" 항목을 참고하세요.

## 준비물

- **Python 3.10 이상.** Windows 환경에서는 [python.org](https://www.python.org/downloads/)에서 설치하며, 설치 첫 화면에서 "Add python.exe to PATH"에 체크합니다.
- **Codex CLI** 또는 다른 MCP 클라이언트
- **OpenDART 인증키.** [OpenDART](https://opendart.fss.or.kr)에 가입한 뒤 "인증키 신청"에서 발급받습니다. 40자리 영문과 숫자로 구성된 키입니다.
  인증키는 1인당 1개만 발급되며 제3자에게 공유할 수 없습니다(OpenDART 이용약관 제19조). 따라서 가족이어도 각자 발급받아야 합니다.

## 설치

[저장소 페이지](https://github.com/easyhunjune/autostock-lite)에서 Code > Download ZIP으로 내려받아 압축을 풀거나, Git이 설치되어 있다면 `git clone https://github.com/easyhunjune/autostock-lite.git`으로 복제합니다.
해당 폴더로 이동한 뒤 아래 명령을 실행합니다. 전용 가상환경을 생성하고 그 안에 패키지를 설치하는 방식입니다.

Windows (PowerShell):

```powershell
python -m venv "$env:USERPROFILE\autostock-lite-venv"
& "$env:USERPROFILE\autostock-lite-venv\Scripts\python.exe" -m pip install .
```

macOS / Linux:

```bash
python3 -m venv ~/autostock-lite-venv
~/autostock-lite-venv/bin/python -m pip install .
```

## 키 넣기

OpenDART 키를 사용자 설정 파일에 기록합니다. `여기에_발급받은_키` 부분을 실제 발급받은 키로 바꾼 뒤 실행하세요.

Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\autostock-lite" | Out-Null
Set-Content -Encoding utf8 "$env:APPDATA\autostock-lite\.env" "DART_API_KEY=여기에_발급받은_키"
```

macOS / Linux:

```bash
mkdir -p ~/.config/autostock-lite
printf 'DART_API_KEY=%s\n' '여기에_발급받은_키' > ~/.config/autostock-lite/.env
chmod 600 ~/.config/autostock-lite/.env
```

설정값은 환경변수가 가장 우선하고, 그 다음 `--env-file`로 지정한 파일, 마지막으로 위 사용자 설정 파일 순서로 적용됩니다.

## 연결 확인

```powershell
& "$env:USERPROFILE\autostock-lite-venv\Scripts\autostock-lite.exe" doctor
```

macOS와 Linux는 `~/autostock-lite-venv/bin/autostock-lite doctor`를 실행합니다.
실행 결과의 마지막 줄이 `doctor: PASS`로 끝나면 인증키와 DART API 연결이 모두 정상입니다. 보안을 위해 키 값은 화면에 출력하지 않습니다.

## Codex에 등록

Windows (PowerShell):

```powershell
codex mcp add autostock-lite -- "$env:USERPROFILE\autostock-lite-venv\Scripts\autostock-lite.exe" mcp
```

macOS / Linux:

```bash
codex mcp add autostock-lite -- ~/autostock-lite-venv/bin/autostock-lite mcp
```

`codex mcp get autostock-lite` 명령으로 등록 내용을 확인한 뒤, Codex를 새로 실행해 질문을 시작하면 됩니다.

다른 MCP 클라이언트를 사용할 때는 위 실행 파일을 `mcp` 인자와 함께 표준 입출력(stdio) 서버로 등록합니다.

## 질문 예시

질문 앞에 **"autostock-lite로"** 를 붙이세요. 도구 이름을 명시하지 않으면 Codex가 autostock-lite 대신 일반 웹 검색을 실행해 웹 페이지를 근거로 답변할 수 있습니다.

- autostock-lite로 삼성물산의 최근 3개 사업연도 매출액과 영업이익 추이를 알려줘. 숫자마다 어느 공시에서 왔는지도 밝혀줘.
- autostock-lite로 005930 최근 사업보고서의 "사업의 내용"을 요약해 줘.
- autostock-lite로 삼성전자 최대주주와 최근 1년 지분 변동을 보여 줘.

처음 종목을 찾을 때는 DART 회사 목록을 내려받아 색인을 생성하므로 몇 초가 더 걸립니다. 색인이 생성된 뒤에는 저장된 캐시를 참조해 바로 응답합니다.

재무제표와 공시 원문 조회 도구는 반환 데이터양이 많습니다. 따라서 단일 질문에 여러 연도나 복수 보고서를 요청하면 AI 토큰 사용량이 함께 늘어납니다.

## 토스증권 키 (선택)

토스증권 계좌가 있으면 토스증권 웹(WTS)의 설정 > Open API에서 키를 발급받을 수 있습니다.
같은 메뉴에서 이 프로그램을 실행할 컴퓨터의 IP를 허용 IP로 등록해야 합니다. 등록되지 않은 IP에서 호출하면 403 오류가 발생해 요청이 차단됩니다.

발급받은 키는 설정 파일에 아래 두 줄을 추가해 등록합니다. 두 항목 중 하나만 입력하면 설정 오류가 발생합니다.

```text
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
```

토스 키를 등록하면 종목 조회 시 토스 종목 정보를 활용해 상장 여부를 판정하므로 거래정지와 정리매매 상태까지 확인할 수 있습니다.
키가 없으면 DART 회사개황의 법인 구분을 기준으로 판정하며, 이때는 거래정지 여부를 판별할 수 없다는 안내를 함께 표시합니다.

토스 인증 토큰은 단일 키당 하나만 유효합니다. 그래서 동일한 키로 서버 인스턴스를 2개 실행하면 두 번째 서버는 기동하지 않고 멈춥니다.
예를 들어 Codex 창을 2개 열 경우 두 번째 창에는 관련 도구가 표시되지 않습니다. 단, 토스 키를 설정하지 않은 기본 상태에서는 이러한 제약이 발생하지 않습니다.

토스 키를 쓸 때 알아 둘 점:

- **토스 키는 주문 API에도 쓰이는 인증 정보입니다.** 토스 공식 문서에는 조회 전용 권한이 분리되어 있지 않습니다. 이 프로그램은 조회 기능만 수행하지만, 키가 외부로 유출되면 주문 권한 탈취와 같은 위험이 발생합니다. 키를 설정 파일 외부로 옮기거나 외부에 노출하지 마세요.
- **토스증권 Open API 데이터 이용 정책 준수:** 토스증권 정책에 따라 API로 수신한 정보는 이용자 본인의 매매 목적으로만 사용해야 합니다. 상업적 활용은 물론 비상업적 용도라도 제3자 배포는 금지되어 있으므로, 조회 결과를 외부에 공유하지 마세요.
- 키를 발급받을 때 동의한 토스증권 Open API 이용약관 내용을 사전에 직접 확인해야 합니다.

## 저장 위치와 삭제

| 내용 | Windows | macOS / Linux |
|---|---|---|
| 설정 파일 (키) | `%APPDATA%\autostock-lite\.env` | `~/.config/autostock-lite/.env` |
| 회사 목록 색인 | `%LOCALAPPDATA%\autostock-lite\` | `~/.local/state/autostock-lite/` |

프로그램과 관련 데이터를 모두 삭제하려면 `codex mcp remove autostock-lite`를 실행한 뒤, 가상환경 폴더와 위 두 디렉터리를 직접 삭제합니다.

## 주의

- 이 프로그램은 금융감독원(OpenDART)이나 토스증권이 공식적으로 제작하거나 보증한 소프트웨어가 아닙니다.
- 이 프로그램을 통해 조회되는 정보는 참고용 데이터입니다. 투자 권유 또는 자문에 해당하지 않으므로, 모든 투자 판단과 결과에 대한 책임은 이용자 본인에게 있습니다.
- 데이터는 금융감독원 OpenDART와 토스증권 Open API(키 설정 시)를 통해 수신합니다. 공시 정보는 제출 법인이 작성 책임을 지며 금융감독원은 정확성과 완전성을 보장하지 않습니다(OpenDART 이용약관 제23조). 따라서 원천 데이터의 오류나 공시 지연이 결과에 그대로 반영될 수 있습니다.
- API 인증키는 이용자가 각자 직접 발급받아 사용해야 하며, 각 서비스의 이용약관을 준수해야 합니다. 이 저장소는 이용자의 키를 수집하거나 보관하지 않으며, 두 서비스에서 수신한 원본 데이터 또한 저장소에 포함하지 않습니다. 테스트 코드에는 직접 작성한 목(mock) 데이터만 사용합니다.
- OpenDART API는 인증키별 호출 한도가 정해져 있습니다. 호출 한도를 초과하면 오류 코드 020이 반환되며, 공식 개발 가이드는 일반적으로 20,000건 이상 요청 시 해당 오류가 발생한다고 안내합니다.
- AI 모델이 도구의 반환 결과를 전달하는 과정에서 수치 오기가 발생할 수 있습니다. 중요한 수치는 답변과 함께 제공되는 공시 링크를 통해 원문 서류를 직접 대조해 확인하세요.

## 라이선스

MIT 라이선스를 따릅니다. 세부 조항은 [LICENSE](LICENSE) 파일을 참고하세요.
