# Jira Daily Report Generator

지정한 날짜의 Jira 활동을 Markdown 보고서로 만드는 스크립트입니다.

이 스크립트는 기본적으로 `JIRA_BASE_URL`에 연결된 Jira 사이트 전체를 조회합니다. 즉 `https://your-site.atlassian.net` 자체가 조회 범위이고, `--projects`를 따로 주지 않으면 사이트 전체에서 해당 날짜 활동을 찾습니다.

기본 리포트에는 아래 내용이 들어갑니다.

- 해당 날짜에 활동이 있었던 이슈 목록
- 전체 요약
- 담당자별 활동 내역
- 오늘 볼 만한 오픈 이슈 제안
- 완료 / 진행중 / 대기 이슈 요약
- 가능하면 해당 날짜의 worklog
- 가능하면 Jira `Start date`가 그 날짜인 이슈

## 설치

```powershell
cd path\to\jira-report-project
python -m pip install -r .\requirements-jira-report.txt
```

## 환경변수

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

생성 파일:

- `reports/jira_daily_report_YYYY-MM-DD.md`

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

## 기본 조회 원리

`--jql`을 직접 주지 않으면 스크립트가 날짜 기반 JQL을 자동 생성합니다. 기본적으로 아래 항목 중 하나라도 만족하면 리포트에 포함됩니다.

- 그 날짜에 `updated`
- 그 날짜에 `created`
- 그 날짜에 `resolved`
- 그 날짜에 `worklogDate`
- Jira의 실제 `Start date` 필드가 그 날짜와 일치

즉, 이 스크립트는 "해당 날짜에 활동이 있었던 작업"과 "해당 날짜 시작 예정으로 잡힌 작업"을 같이 보려는 용도입니다.

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