# Jira 일일 보고서 생성기

지정한 날짜의 Jira 활동을 모아서 Markdown 보고서로 만들고, 원하면 Notion DB에도 같은 날짜로 업로드할 수 있는 스크립트입니다.

기본 동작 범위는 `JIRA_BASE_URL`에 연결된 Jira 사이트 전체입니다. 즉 `https://your-site.atlassian.net` 자체가 조회 범위이고, `--projects`를 주지 않으면 해당 사이트 안에서 날짜 조건에 맞는 이슈를 찾습니다.

현재 보고서에는 아래 내용이 포함됩니다.

- 전체 요약
- 전체 활동 항목
- 담당자별 활동
- 담당자별 오늘 확인할 항목
- 완료 / 진행중 / 대기 항목 요약
- 가능하면 해당 날짜의 worklog
- 가능하면 Jira `Start date`가 그 날짜인 항목

아침 1회 운영이 필요하면 `--morning-brief` 모드를 사용할 수 있습니다. 이 모드는 실행 날짜를 기준으로 아래 내용을 한 번에 묶습니다.

- 어제 무엇을 했는지
- 어제 무엇이 완료됐는지
- 오늘 무엇을 해야 하는지
- 담당자별 오늘 확인할 항목

## 설치

```powershell
cd path\to\jira-report-project
python -m pip install -r .\requirements-jira-report.txt
```

## 가장 쉬운 실행 방법

1. `setup_project.bat`를 실행합니다.
2. 생성된 `.env` 파일에 Jira와 Notion 값을 채웁니다.
3. `run_morning_brief.bat`를 더블클릭합니다.

`setup_project.bat`가 해주는 일:

- `.venv` 가상환경 생성
- 필요한 패키지 설치
- `.env.example`을 `.env`로 복사

이제 스크립트는 기본적으로 현재 폴더의 `.env`를 자동으로 읽습니다. 그래서 매번 PowerShell에서 `$env:...`를 다시 입력할 필요가 없습니다. 또한 `run_morning_brief.bat`는 `.venv`가 있으면 그 Python을 자동으로 사용합니다.

예시:

```powershell
.\setup_project.bat
```

배치 실행기 사용:

- 그냥 더블클릭: 오늘 날짜 기준 아침 브리프 실행
- 날짜를 직접 넣고 싶을 때:

```powershell
.\run_morning_brief.bat 2026-04-22
```

## Jira 환경변수

`.env`를 쓰지 않고 직접 실행하고 싶다면 아래처럼 환경변수를 넣을 수도 있습니다.

PowerShell 기준:

```powershell
$env:JIRA_BASE_URL="https://your-site.atlassian.net"
$env:JIRA_EMAIL="you@example.com"
$env:JIRA_API_TOKEN="your_api_token"
```

필수 값:

- `JIRA_BASE_URL`
- `JIRA_EMAIL`
- `JIRA_API_TOKEN`

## 기본 실행

```powershell
python .\jira_daily_report.py --date 2026-04-17 --verbose
```

다른 `.env` 파일을 쓰고 싶으면:

```powershell
python .\jira_daily_report.py --date 2026-04-17 --env-file .\.env
```

생성 파일:

- `reports/jira_daily_report_YYYY-MM-DD.md`

`--report-label`을 주면 파일명에도 같이 붙습니다.

```powershell
python .\jira_daily_report.py --date 2026-04-17 --report-label 저녁
```

예시 출력 파일:

- `reports/jira_daily_report_2026-04-17_저녁.md`

## 선택 옵션

특정 프로젝트만 보고 싶을 때:

```powershell
python .\jira_daily_report.py --date 2026-04-17 --projects ABC,XYZ
```

worklog를 빼고 싶을 때:

```powershell
python .\jira_daily_report.py --date 2026-04-17 --no-include-worklogs
```

직접 JQL을 넣고 싶을 때:

```powershell
python .\jira_daily_report.py --date 2026-04-17 --jql 'project = ABC AND statusCategory != Done ORDER BY updated DESC'
```

아침 브리프를 만들고 싶을 때:

```powershell
python .\jira_daily_report.py --date 2026-04-22 --morning-brief --publish-notion --verbose
```

위 명령은 `2026-04-22` 아침 기준으로:

- 어제 `2026-04-21`의 활동
- 오늘 `2026-04-22` 시작 예정 항목
- 어제 하던 일 중 오늘도 이어봐야 할 미완료 항목

을 한 보고서로 묶어줍니다.

## 조회 원리

`--jql`을 직접 주지 않으면 스크립트가 날짜 기반 JQL을 자동 생성합니다. 기본적으로 아래 중 하나라도 만족하면 리포트에 포함됩니다.

- 그 날짜에 `updated`
- 그 날짜에 `created`
- 그 날짜에 `resolved`
- 그 날짜에 `worklogDate`
- Jira의 실제 `Start date` 필드가 그 날짜와 일치

즉, 이 스크립트는 "해당 날짜에 실제 활동이 있었던 작업"과 "해당 날짜 시작 예정으로 잡힌 작업"을 함께 보려는 용도입니다.

`--morning-brief`는 이 원리를 아침 관점으로 다시 묶은 모드입니다. 일반 일일 보고서와 달리, 보고서 날짜를 `오늘`로 두고 내부적으로는 `어제 활동 + 오늘 계획`을 나눠서 조회합니다.

## Notion 업로드

이제 생성된 보고서를 Notion DB에도 올릴 수 있습니다. 핵심은 "캘린더에 직접 업로드"가 아니라 "날짜 속성이 있는 DB 행(page)을 만들거나 갱신"하는 방식입니다. 캘린더 뷰는 그 DB의 날짜 속성을 기준으로 보여줍니다.

### Notion 준비

1. 보고서를 올릴 Notion DB를 하나 만듭니다.
2. 그 DB에 `title` 타입 속성 1개가 있어야 합니다.
3. 그 DB에 `date` 타입 속성 1개가 있어야 합니다.
4. Notion integration을 만들고, 해당 DB에 연결합니다.
5. integration token과 Notion DB ID를 준비합니다.

필수 환경변수:

```powershell
$env:NOTION_API_KEY="secret_xxx"
$env:NOTION_DATABASE_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

`NOTION_DATABASE_ID`와 `NOTION_DATA_SOURCE_ID`는 아래 형태를 모두 받을 수 있습니다.

- 하이픈 없는 32자리 ID
- 하이픈이 들어간 UUID 형태
- 전체 Notion URL

선택 환경변수:

```powershell
$env:NOTION_DATA_SOURCE_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
$env:NOTION_TITLE_PROPERTY="이름"
$env:NOTION_DATE_PROPERTY="날짜"
```

현재 스크립트는 아래 순서로 Notion 대상을 찾습니다.

- `NOTION_DATA_SOURCE_ID`가 있으면 그 값을 바로 사용
- 없으면 `NOTION_DATABASE_ID`로 DB를 조회해서 그 안의 data source를 자동 탐색

즉, 대부분의 경우에는 URL에서 확인한 DB ID만 넣어도 됩니다.

실수로 DB URL 또는 DB ID를 `NOTION_DATA_SOURCE_ID`에 넣어도, 현재 스크립트는 한 번 더 DB로 간주해서 자동 재시도합니다. 그래도 헷갈리지 않게 쓰려면 아래처럼 구분하는 편이 가장 안전합니다.

- `NOTION_DATABASE_ID`: 보통 Notion URL의 `?` 앞 긴 ID
- `NOTION_DATA_SOURCE_ID`: 정말 data source ID를 따로 알고 있을 때만 사용

속성 이름을 지정하지 않으면 스크립트가 아래 순서로 자동 추정합니다.

- 제목 속성: `title` 타입 첫 번째 속성
- 날짜 속성: `날짜`, `Date`, `일자`, `보고일`, `Report Date` 중 하나
- 위 이름이 없으면 `date` 타입 속성이 딱 1개일 때 그 속성 사용

### 특정 날짜 1건 업로드 테스트

```powershell
$env:JIRA_BASE_URL="https://your-site.atlassian.net"
$env:JIRA_EMAIL="you@example.com"
$env:JIRA_API_TOKEN="your_jira_api_token"
$env:NOTION_API_KEY="secret_xxx"
$env:NOTION_DATABASE_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
python .\jira_daily_report.py --date 2026-04-21 --publish-notion --verbose
```

동작 방식:

- 로컬 `reports` 폴더에 Markdown 저장
- Notion DB에서 같은 날짜의 같은 제목 페이지를 찾음
- 있으면 본문을 비우고 다시 채우며 업데이트
- 없으면 새 페이지 생성

Notion URL 예시가 아래처럼 생겼다면:

```text
https://www.notion.so/341d7e...6?v=341d7e...8f&source=copy_link
```

보통 `?` 앞의 `341d7e...6` 부분을 `NOTION_DATABASE_ID`로 넣으면 됩니다. `v=` 뒤 값은 보통 뷰 ID라서 넣지 않습니다.

기본 제목은 아래 형식입니다.

- `Jira 보고서 - 2026-04-21`

아침 브리프 제목은 아래 형식입니다.

- `Jira 아침 브리프 - 2026-04-21`

`--report-label`을 주면 제목이 이렇게 바뀝니다.

- `Jira 보고서 - 2026-04-21 - 아침`
- `Jira 보고서 - 2026-04-21 - 저녁`

예시:

```powershell
python .\jira_daily_report.py --date 2026-04-21 --report-label 아침 --publish-notion
python .\jira_daily_report.py --date 2026-04-21 --report-label 저녁 --publish-notion
```

이렇게 하면 같은 날짜에 아침/저녁 보고서를 따로 쌓는 구조로도 쓸 수 있습니다.

제목을 직접 고정하고 싶으면:

```powershell
python .\jira_daily_report.py --date 2026-04-21 --notion-title "2026-04-21 Jira 저녁 보고" --publish-notion
```

## Start Date 관련 주의

`Start date`가 Jira 이슈 필드에 실제로 저장된 경우에는 자동으로 감지해서 검색에 포함합니다.

하지만 아래 경우는 잡히지 않을 수 있습니다.

- Advanced Roadmaps 같은 플랜 화면에서만 보이는 roll-up 날짜
- `Start date`가 아니라 별도 커스텀 날짜 필드에 저장된 값

`--verbose`로 실행했을 때 아래처럼 보이면 `Start date` 필드를 인식한 것입니다.

```text
[debug] detected start-date field: Start date (customfield_xxxxx)
```

## 참고

- 기본 timezone은 `Asia/Seoul`
- `--spaces`는 이전 호환용 별칭이고 현재는 `--projects`와 동일하게 동작
- `--dev-projects`, `--planning-projects`는 예전 팀 분류용 옵션이라 지금은 선택사항
- codex를 통해 생성됨