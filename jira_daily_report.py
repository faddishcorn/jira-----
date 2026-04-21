#!/usr/bin/env python3
"""Generate a one-day Jira activity report as Markdown.

Assumptions:
- Jira Cloud
- Authentication with Atlassian account email + API token
- Report date interpreted in a configurable timezone (default: Asia/Seoul)

Environment variables:
- JIRA_BASE_URL   e.g. https://your-domain.atlassian.net
- JIRA_EMAIL      Atlassian account email
- JIRA_API_TOKEN  Atlassian API token

Output:
- markdown file under ./reports by default
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import textwrap
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import requests
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Seoul"
SEARCH_ENDPOINT = "/rest/api/3/search/jql"
FIELD_ENDPOINT = "/rest/api/3/field"
WORKLOG_ENDPOINT_TEMPLATE = "/rest/api/2/issue/{issue_key}/worklog"
DEFAULT_FIELDS = [
    "summary",
    "status",
    "assignee",
    "reporter",
    "project",
    "issuetype",
    "labels",
    "components",
    "created",
    "updated",
    "resolutiondate",
    "priority",
]
DONE_STATUS_KEYS = {"done"}
IN_PROGRESS_STATUS_KEYS = {"indeterminate"}
TODO_STATUS_KEYS = {"new"}


class JiraAPIError(RuntimeError):
    """Raised when Jira returns an unexpected API response."""


@dataclass
class WorklogEntry:
    author: str
    started: datetime
    time_spent_seconds: int
    comment: str = ""


@dataclass
class IssueRecord:
    key: str
    summary: str
    team: str
    project_key: str
    issue_type: str
    status_name: str
    status_category_key: str
    assignee: str
    reporter: str
    labels: list[str]
    components: list[str]
    created: datetime | None
    start_date: date | None
    updated: datetime | None
    resolutiondate: datetime | None
    priority: str
    url: str
    worklogs_today: list[WorklogEntry] = field(default_factory=list)

    @property
    def is_done(self) -> bool:
        return self.status_category_key in DONE_STATUS_KEYS

    @property
    def is_in_progress(self) -> bool:
        return self.status_category_key in IN_PROGRESS_STATUS_KEYS

    @property
    def total_worklog_seconds_today(self) -> int:
        return sum(item.time_spent_seconds for item in self.worklogs_today)


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        token = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Basic {token}",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        retries = 4
        for attempt in range(retries):
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            if response.status_code == 429 and attempt < retries - 1:
                retry_after = response.headers.get("Retry-After")
                sleep_seconds = float(retry_after) if retry_after else (2**attempt)
                time.sleep(sleep_seconds)
                continue
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except Exception:
                    payload = response.text
                raise JiraAPIError(f"Jira API error {response.status_code} for {url}: {payload}")
            return response.json()
        raise JiraAPIError(f"Jira API retry limit exceeded for {url}")

    def get_fields(self) -> list[dict[str, Any]]:
        payload = self._request("GET", FIELD_ENDPOINT)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        raise JiraAPIError(
            f"Unexpected field metadata payload for {self.base_url}{FIELD_ENDPOINT}: {payload!r}"
        )

    def search_issues(self, *, jql: str, fields: list[str], page_size: int = 100) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        next_page_token: str | None = None
        while True:
            payload: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": fields,
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            data = self._request("POST", SEARCH_ENDPOINT, json_body=payload)
            page_issues = data.get("issues", [])
            issues.extend(page_issues)
            next_page_token = data.get("nextPageToken")
            is_last = bool(data.get("isLast"))
            if is_last or not page_issues or not next_page_token:
                break
        return issues

    def get_issue_worklogs(
        self,
        issue_key: str,
        *,
        started_after_ms: int,
        started_before_ms: int,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        worklogs: list[dict[str, Any]] = []
        start_at = 0
        while True:
            params = {
                "startAt": start_at,
                "maxResults": page_size,
                "startedAfter": started_after_ms,
                "startedBefore": started_before_ms,
            }
            path = WORKLOG_ENDPOINT_TEMPLATE.format(issue_key=quote(issue_key, safe=""))
            data = self._request("GET", path, params=params)
            page_worklogs = data.get("worklogs", [])
            worklogs.extend(page_worklogs)
            total = data.get("total", len(worklogs))
            if len(worklogs) >= total or not page_worklogs:
                break
            start_at += len(page_worklogs)
        return worklogs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a one-day Jira markdown activity report.")
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD")
    parser.add_argument("--timezone", default=DEFAULT_TZ, help=f"Timezone name (default: {DEFAULT_TZ})")
    parser.add_argument(
        "--dev-projects",
        default="",
        help="Legacy optional grouping by project keys for development team",
    )
    parser.add_argument(
        "--planning-projects",
        default="",
        help="Legacy optional grouping by project keys for planning team",
    )
    parser.add_argument(
        "--projects",
        default="",
        help="Comma-separated Jira project keys to include (optional). Omit to search the whole site.",
    )
    parser.add_argument(
        "--spaces",
        default="",
        help="Legacy alias of --projects. Jira site scope already comes from JIRA_BASE_URL.",
    )
    parser.add_argument(
        "--jql",
        default="",
        help="Optional custom JQL. If omitted, the script builds a date-based JQL automatically.",
    )
    parser.add_argument(
        "--include-worklogs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include worklogs written on the target day (default: true)",
    )
    parser.add_argument("--max-issues", type=int, default=1000, help="Safety cap for returned issues (default: 1000)")
    parser.add_argument("--output-dir", default="reports", help="Directory to write markdown report into")
    parser.add_argument("--verbose", action="store_true", help="Print debug information")
    return parser.parse_args()


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing environment variable: {name}")
    return value


def parse_date_ymd(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid --date value '{value}'. Use YYYY-MM-DD.") from exc


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def get_day_bounds(target_day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(target_day, dt_time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def to_jira_jql_datetime(value: datetime) -> str:
    local_naive = value.replace(tzinfo=None)
    return local_naive.strftime("%Y-%m-%d %H:%M")


def to_epoch_millis(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def parse_jira_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def parse_jira_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def html_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        if "content" in value and isinstance(value["content"], list):
            return " ".join(filter(None, (html_to_text(item) for item in value["content"]))).strip()
        if value.get("type") == "text":
            return str(value.get("text", "")).strip()
        if "text" in value and isinstance(value["text"], str):
            return value["text"].strip()
        return " ".join(filter(None, (html_to_text(item) for item in value.values()))).strip()
    if isinstance(value, list):
        return " ".join(filter(None, (html_to_text(item) for item in value))).strip()
    return str(value).strip()


def find_start_date_field(fields_metadata: Iterable[dict[str, Any]]) -> dict[str, str] | None:
    for field in fields_metadata:
        schema = field.get("schema") or {}
        if schema.get("system") == "startdate":
            return {"id": str(field.get("id", "startdate")), "jql_name": str(field.get("name", "Start date"))}
    for field in fields_metadata:
        if str(field.get("name", "")).strip().lower() == "start date":
            return {"id": str(field.get("id", "")), "jql_name": str(field.get("name", "Start date"))}
    return None


def build_default_jql(
    *,
    start: datetime,
    end: datetime,
    dev_projects: list[str],
    planning_projects: list[str],
    all_projects: list[str],
    include_worklog_date: bool = True,
    start_date_jql_name: str | None = None,
) -> str:
    projects = all_projects or (dev_projects + planning_projects)
    start_text = to_jira_jql_datetime(start)
    end_text = to_jira_jql_datetime(end)

    activity_clauses = [
        f'(updated >= "{start_text}" AND updated < "{end_text}")',
        f'(created >= "{start_text}" AND created < "{end_text}")',
        f'(resolved >= "{start_text}" AND resolved < "{end_text}")',
    ]
    if include_worklog_date:
        activity_clauses.append(
            f'(worklogDate >= "{start.date().isoformat()}" AND worklogDate < "{end.date().isoformat()}")'
        )
    if start_date_jql_name:
        escaped_name = start_date_jql_name.replace('"', '\\"')
        activity_clauses.append(f'("{escaped_name}" = "{start.date().isoformat()}")')

    project_filter = ""
    if projects:
        project_clause = ", ".join(sorted(set(projects)))
        project_filter = f"project in ({project_clause})\n        AND "

    activity_block = "\n        OR ".join(activity_clauses)
    return textwrap.dedent(
        f"""
        {project_filter}(
        {activity_block}
        )
        ORDER BY assignee, updated DESC
        """
    ).strip()


def classify_team(project_key: str, dev_projects: set[str], planning_projects: set[str]) -> str:
    if project_key in dev_projects:
        return "Development"
    if project_key in planning_projects:
        return "Planning"
    return "미분류"


def normalize_issue(
    raw: dict[str, Any],
    *,
    base_url: str,
    dev_projects: set[str],
    planning_projects: set[str],
    start_date_field_id: str | None = None,
) -> IssueRecord:
    fields = raw.get("fields", {})
    assignee = fields.get("assignee") or {}
    reporter = fields.get("reporter") or {}
    project = fields.get("project") or {}
    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    status_category = status.get("statusCategory") or {}
    labels = fields.get("labels") or []
    components = fields.get("components") or []
    project_key = project.get("key", "UNKNOWN")
    start_date_value = parse_jira_date(fields.get(start_date_field_id)) if start_date_field_id else None

    return IssueRecord(
        key=raw.get("key", "UNKNOWN"),
        summary=(fields.get("summary") or "(제목 없음)").strip(),
        team=classify_team(project_key, dev_projects, planning_projects),
        project_key=project_key,
        issue_type=issue_type.get("name", "Unknown"),
        status_name=status.get("name", "Unknown"),
        status_category_key=status_category.get("key", "unknown"),
        assignee=assignee.get("displayName") or "미배정",
        reporter=reporter.get("displayName") or "알 수 없음",
        labels=[str(label) for label in labels],
        components=[item.get("name", "") for item in components if item.get("name")],
        created=parse_jira_datetime(fields.get("created")),
        start_date=start_date_value,
        updated=parse_jira_datetime(fields.get("updated")),
        resolutiondate=parse_jira_datetime(fields.get("resolutiondate")),
        priority=(fields.get("priority") or {}).get("name", "Unknown"),
        url=f"{base_url}/browse/{raw.get('key', '')}",
    )


def extract_today_worklogs(raw_worklogs: Iterable[dict[str, Any]], *, start: datetime, end: datetime) -> list[WorklogEntry]:
    items: list[WorklogEntry] = []
    for raw in raw_worklogs:
        started = parse_jira_datetime(raw.get("started"))
        if not started:
            continue
        started_local = started.astimezone(start.tzinfo)
        if not (start <= started_local < end):
            continue
        items.append(
            WorklogEntry(
                author=(raw.get("author") or {}).get("displayName", "알 수 없음"),
                started=started_local,
                time_spent_seconds=int(raw.get("timeSpentSeconds") or 0),
                comment=html_to_text(raw.get("comment")),
            )
        )
    return items


def seconds_to_human(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def fmt_local(value: datetime | None, timezone_name: str) -> str | None:
    if not value:
        return None
    return value.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M %Z")


def issue_activity_time(issue: IssueRecord) -> datetime:
    return issue.updated or issue.created or datetime.min.replace(tzinfo=timezone.utc)


def priority_rank(issue: IssueRecord) -> int:
    priority_rank_map = {
        "Highest": 0,
        "High": 1,
        "Medium": 2,
        "Low": 3,
        "Lowest": 4,
    }
    return priority_rank_map.get(issue.priority, 5)


def build_issue_detail_lines(
    issue: IssueRecord,
    *,
    timezone_name: str,
    person_worklogs: list[WorklogEntry] | None = None,
    include_worklogs: bool = True,
) -> list[str]:
    lines = [f"- **{issue.key}** - {issue.summary}"]
    meta_bits = [
        issue.project_key,
        f"유형={issue.issue_type}",
        f"상태={issue.status_name}",
        f"우선순위={issue.priority}",
    ]
    if issue.components:
        meta_bits.append(f"컴포넌트={', '.join(issue.components)}")
    if issue.labels:
        meta_bits.append(f"라벨={', '.join(issue.labels)}")
    lines.append(f"  - {' / '.join(meta_bits)}")
    lines.append(f"  - 담당자={issue.assignee} / 보고자={issue.reporter}")

    if issue.start_date:
        lines.append(f"  - 시작일: {issue.start_date.isoformat()}")

    created_text = fmt_local(issue.created, timezone_name)
    updated_text = fmt_local(issue.updated, timezone_name)
    resolved_text = fmt_local(issue.resolutiondate, timezone_name)
    if created_text:
        lines.append(f"  - 생성: {created_text}")
    if updated_text:
        lines.append(f"  - 수정: {updated_text}")
    if resolved_text:
        lines.append(f"  - 완료: {resolved_text}")

    lines.append(f"  - 링크: {issue.url}")

    if include_worklogs and person_worklogs:
        worklog_seconds = sum(item.time_spent_seconds for item in person_worklogs)
        lines.append(f"  - 작업 기록: {seconds_to_human(worklog_seconds)}")
        for worklog in person_worklogs:
            comment = f" / {worklog.comment}" if worklog.comment else ""
            started_text = worklog.started.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
            lines.append(f"    - {started_text} / {seconds_to_human(worklog.time_spent_seconds)}{comment}")
    elif include_worklogs and issue.worklogs_today:
        lines.append(f"  - 오늘 작업 기록 합계: {seconds_to_human(issue.total_worklog_seconds_today)}")

    return lines


def build_action_hint(issue: IssueRecord) -> str:
    if issue.is_in_progress:
        return "계속 진행"
    if issue.status_category_key in TODO_STATUS_KEYS:
        return "착수 필요"
    return "확인 필요"


def action_rank(issue: IssueRecord) -> tuple[int, int, datetime]:
    status_rank = 0 if issue.is_in_progress else 1 if issue.status_category_key in TODO_STATUS_KEYS else 2
    recency = issue_activity_time(issue)
    return (status_rank, priority_rank(issue), recency)


def build_executive_summary_lines(
    *,
    target_day: date,
    timezone_name: str,
    issues: list[IssueRecord],
    include_worklogs: bool,
) -> list[str]:
    lines = ["## 전체 요약", ""]
    if not issues:
        lines.extend(["- 선택한 날짜 기준으로 Jira 활동이나 시작 예정 항목이 확인되지 않았습니다.", ""])
        return lines

    done_items = sorted((issue for issue in issues if issue.is_done), key=issue_activity_time, reverse=True)
    in_progress_items = sorted(
        (issue for issue in issues if issue.is_in_progress),
        key=lambda issue: (priority_rank(issue), -issue_activity_time(issue).timestamp()),
    )
    open_items = [issue for issue in issues if not issue.is_done]
    scheduled_today = sorted(
        (issue for issue in open_items if issue.start_date == target_day),
        key=lambda issue: (priority_rank(issue), issue.project_key, issue.key),
    )
    stale_in_progress = sorted(
        (
            issue
            for issue in in_progress_items
            if issue.updated and issue.updated.date() <= target_day - timedelta(days=2)
        ),
        key=lambda issue: (priority_rank(issue), issue.updated or datetime.min.replace(tzinfo=timezone.utc)),
    )
    should_have_started = sorted(
        (
            issue
            for issue in open_items
            if issue.start_date and issue.start_date < target_day and not issue.is_in_progress
        ),
        key=lambda issue: (priority_rank(issue), issue.start_date or date.min, issue.project_key, issue.key),
    )

    total_worklog_seconds = sum(issue.total_worklog_seconds_today for issue in issues)
    overall_bits = [
        f"총 {len(issues)}건 추적",
        f"완료 {len(done_items)}건",
        f"진행중 {len(in_progress_items)}건",
        f"미완료 {len(open_items) - len(in_progress_items)}건",
    ]
    if include_worklogs and total_worklog_seconds:
        overall_bits.append(f"작업 기록 {seconds_to_human(total_worklog_seconds)}")
    lines.append(f"- 전체 현황: {', '.join(overall_bits)}.")
    top_projects = Counter(issue.project_key for issue in issues).most_common(5)
    if top_projects:
        project_summary = ", ".join(f"{project} ({count})" for project, count in top_projects)
        lines.append(f"- 활동이 많았던 프로젝트: {project_summary}")
    top_statuses = Counter(issue.status_name for issue in issues).most_common(5)
    if top_statuses:
        status_summary = ", ".join(f"{status} ({count})" for status, count in top_statuses)
        lines.append(f"- 상태 분포 요약: {status_summary}")
    if include_worklogs and total_worklog_seconds:
        top_workers = Counter()
        for issue in issues:
            for worklog in issue.worklogs_today:
                top_workers[worklog.author] += worklog.time_spent_seconds
        worker_summary = ", ".join(
            f"{person} ({seconds_to_human(seconds)})"
            for person, seconds in top_workers.most_common(5)
        )
        if worker_summary:
            lines.append(f"- 작업 기록 상위 기여자: {worker_summary}")
    lines.append("")

    lines.append("### 주요 진척")
    if done_items:
        for issue in done_items[:3]:
            resolved_text = fmt_local(issue.resolutiondate or issue.updated, timezone_name)
            suffix = f" / 완료 시각 {resolved_text}" if resolved_text else ""
            lines.append(f"- **{issue.key}** - {issue.summary}{suffix}")
    else:
        lines.append("- 선택한 날짜에 완료로 잡힌 항목은 없습니다.")
    lines.append("")

    lines.append("### 당일 시작 예정")
    if scheduled_today:
        for issue in scheduled_today[:5]:
            lines.append(
                f"- **{issue.key}** - {issue.summary} / {issue.project_key} / "
                f"{issue.status_name} / 우선순위={issue.priority}"
            )
    else:
        lines.append("- 해당 날짜에 시작일이 지정된 미완료 항목은 없습니다.")
    lines.append("")

    lines.append("### 리스크 및 확인 필요")
    attention_items = stale_in_progress[:3] + [
        item for item in should_have_started[:3] if item not in stale_in_progress[:3]
    ]
    if attention_items:
        for issue in attention_items[:5]:
            reason_bits: list[str] = []
            if issue.is_in_progress and issue.updated:
                reason_bits.append(f"{issue.updated.date().isoformat()} 이후 업데이트 없음")
            if issue.start_date and issue.start_date < target_day and not issue.is_in_progress:
                reason_bits.append(f"시작일 경과 ({issue.start_date.isoformat()})")
            reason_text = f" / {'; '.join(reason_bits)}" if reason_bits else ""
            lines.append(
                f"- **{issue.key}** - {issue.summary} / {issue.status_name} / 우선순위={issue.priority}{reason_text}"
            )
    else:
        lines.append("- 현재 데이터 기준으로 뚜렷한 지연 또는 시작일 경과 항목은 보이지 않습니다.")
    lines.append("")

    lines.append("### 담당자별 최우선 확인 항목")
    owner_focus: dict[str, list[IssueRecord]] = defaultdict(list)
    for issue in open_items:
        owner_focus[issue.assignee or "미배정"].append(issue)
    owner_order = sorted(
        owner_focus.keys(),
        key=lambda person: (-len(owner_focus[person]), person),
    )
    for person in owner_order[:5]:
        ranked_items = sorted(owner_focus[person], key=action_rank)
        top_issue = ranked_items[0]
        lines.append(
            f"- {person}: 미완료 {len(ranked_items)}건 / 최우선 **{top_issue.key}** "
            f"({build_action_hint(top_issue)}, {top_issue.priority})"
        )
    lines.append("")
    return lines


def build_report_markdown(
    *,
    target_day: date,
    timezone_name: str,
    jql: str,
    issues: list[IssueRecord],
    include_worklogs: bool,
) -> str:
    total_issues = len(issues)
    contributor_names = {
        worklog.author
        for issue in issues
        for worklog in issue.worklogs_today
        if worklog.author
    }
    if not contributor_names:
        contributor_names = {issue.assignee for issue in issues if issue.assignee != "미배정"}
    total_people = len(contributor_names)
    done_count = sum(1 for issue in issues if issue.is_done)
    in_progress_count = sum(1 for issue in issues if issue.is_in_progress)
    todo_count = sum(1 for issue in issues if issue.status_category_key in TODO_STATUS_KEYS)
    unknown_count = total_issues - done_count - in_progress_count - todo_count
    total_worklog_seconds = sum(issue.total_worklog_seconds_today for issue in issues)

    lines: list[str] = [
        f"# Jira 일일 활동 보고서 - {target_day.isoformat()}",
        "",
        # f"- 타임존: `{timezone_name}`",
        f"- 집계된 이슈 수: **{total_issues}**",
        f"- 기여자 수: **{total_people}**",
        (
            f"- 완료: **{done_count}** / 진행중: **{in_progress_count}** / "
            f"대기: **{todo_count}** / 기타: **{unknown_count}**"
        ),
    ]
    if include_worklogs:
        lines.append(f"- 당일 작업 기록 시간: **{seconds_to_human(total_worklog_seconds)}**")

    lines.append("")

    if not issues:
        lines.extend(["## 요약", "", "선택한 날짜에 해당하는 Jira 활동이 없습니다."])
        return "\n".join(lines) + "\n"

    lines.extend(
        build_executive_summary_lines(
            target_day=target_day,
            timezone_name=timezone_name,
            issues=issues,
            include_worklogs=include_worklogs,
        )
    )

    lines.extend(["## 전체 활동 항목", ""])
    all_items = sorted(
        issues,
        key=lambda issue: (
            issue.updated or issue.created or datetime.min.replace(tzinfo=timezone.utc),
            issue.project_key,
            issue.key,
        ),
        reverse=True,
    )
    for issue in all_items:
        lines.extend(build_issue_detail_lines(issue, timezone_name=timezone_name, include_worklogs=include_worklogs))
        lines.append("")

    person_entries: dict[str, list[tuple[IssueRecord, list[WorklogEntry]]]] = defaultdict(list)
    for issue in issues:
        if include_worklogs and issue.worklogs_today:
            by_author: dict[str, list[WorklogEntry]] = defaultdict(list)
            for worklog in issue.worklogs_today:
                by_author[worklog.author or "알 수 없음"].append(worklog)
            for author, author_worklogs in by_author.items():
                person_entries[author].append((issue, sorted(author_worklogs, key=lambda item: item.started)))
        else:
            person_entries[issue.assignee or "미배정"].append((issue, []))

    lines.extend(["## 담당자별 활동", ""])
    for person in sorted(person_entries.keys()):
        entries = sorted(
            person_entries[person],
            key=lambda item: (
                item[0].updated or item[0].created or datetime.min.replace(tzinfo=timezone.utc),
                item[0].project_key,
                item[0].key,
            ),
            reverse=True,
        )
        touched_issue_count = len({issue.key for issue, _ in entries})
        person_work_seconds = sum(worklog.time_spent_seconds for _, worklogs in entries for worklog in worklogs)
        lines.extend([f"### {person}", ""])
        if include_worklogs:
            lines.append(
                f"- 관련 이슈: **{touched_issue_count}** / 작업 기록 시간: **{seconds_to_human(person_work_seconds)}**"
            )
        else:
            lines.append(f"- 관련 이슈: **{touched_issue_count}**")
        lines.append("")
        for issue, person_worklogs in entries:
            lines.extend(
                build_issue_detail_lines(
                    issue,
                    timezone_name=timezone_name,
                    person_worklogs=person_worklogs,
                    include_worklogs=include_worklogs,
                )
            )
            lines.append("")

    actionable_issues = [issue for issue in issues if not issue.is_done]
    lines.extend(["## 담당자별 오늘 확인할 항목", ""])
    if not actionable_issues:
        lines.extend(["- 선택한 날짜의 활동 기준으로 추가 확인이 필요한 미완료 항목이 없습니다.", ""])
    else:
        actionable_by_person: dict[str, list[IssueRecord]] = defaultdict(list)
        for issue in actionable_issues:
            actionable_by_person[issue.assignee or "미배정"].append(issue)

        for person in sorted(actionable_by_person.keys()):
            person_items = sorted(actionable_by_person[person], key=action_rank)
            lines.extend([f"### {person}", "", f"- 오늘 확인할 미완료 항목: **{len(person_items)}**", ""])
            for issue in person_items:
                lines.append(
                    f"- **{issue.key}** - {build_action_hint(issue)} / "
                    f"{issue.summary} [{issue.status_name}] / 우선순위={issue.priority}"
                )
                lines.append(f"  - {issue.project_key} / 유형={issue.issue_type} / 보고자={issue.reporter}")
                if issue.start_date:
                    lines.append(f"  - 시작일: {issue.start_date.isoformat()}")
                updated_text = fmt_local(issue.updated, timezone_name)
                created_text = fmt_local(issue.created, timezone_name)
                if updated_text:
                    lines.append(f"  - 최근 활동: {updated_text}")
                elif created_text:
                    lines.append(f"  - 생성: {created_text}")
                if issue.components:
                    lines.append(f"  - 컴포넌트: {', '.join(issue.components)}")
                if issue.labels:
                    lines.append(f"  - 라벨: {', '.join(issue.labels)}")
                lines.append(f"  - 링크: {issue.url}")
            lines.append("")

    completed = [issue for issue in issues if issue.is_done]
    in_progress = [issue for issue in issues if issue.is_in_progress]
    backlogish = [issue for issue in issues if issue.status_category_key in TODO_STATUS_KEYS]

    def add_issue_section(title: str, items: list[IssueRecord]) -> None:
        lines.extend([f"## {title}", ""])
        if not items:
            lines.extend(["- 없음", ""])
            return
        for issue in items:
            suffix = f" ({issue.assignee})" if issue.assignee else ""
            lines.append(f"- **{issue.key}** - {issue.summary} [{issue.status_name}] - {issue.project_key}{suffix}")
        lines.append("")

    add_issue_section("완료 항목", completed)
    add_issue_section("진행중 항목", in_progress)
    add_issue_section("대기 또는 예정 항목", backlogish)
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    args = parse_args()

    base_url = require_env("JIRA_BASE_URL")
    email = require_env("JIRA_EMAIL")
    api_token = require_env("JIRA_API_TOKEN")

    target_day = parse_date_ymd(args.date)
    start, end = get_day_bounds(target_day, args.timezone)
    dev_projects = parse_csv(args.dev_projects)
    planning_projects = parse_csv(args.planning_projects)
    all_projects = list(dict.fromkeys(parse_csv(args.projects) + parse_csv(args.spaces)))

    if args.verbose:
        print(f"[debug] report date: {target_day}")
        print(f"[debug] timezone: {args.timezone}")

    client = JiraClient(base_url=base_url, email=email, api_token=api_token)
    start_date_field_meta: dict[str, str] | None = None
    extra_fields: list[str] = []
    try:
        start_date_field_meta = find_start_date_field(client.get_fields())
    except JiraAPIError as exc:
        if args.verbose:
            print(f"[warn] failed to inspect Jira fields: {exc}", file=sys.stderr)

    if start_date_field_meta and start_date_field_meta.get("id"):
        extra_fields.append(start_date_field_meta["id"])

    auto_jql = build_default_jql(
        start=start,
        end=end,
        dev_projects=dev_projects,
        planning_projects=planning_projects,
        all_projects=all_projects,
        start_date_jql_name=start_date_field_meta["jql_name"] if start_date_field_meta else None,
    )
    jql = args.jql or auto_jql

    if args.verbose:
        if start_date_field_meta:
            print(
                f"[debug] detected start-date field: "
                f"{start_date_field_meta['jql_name']} ({start_date_field_meta['id']})"
            )
        else:
            print(
                "[warn] no Jira Start date field detected; scheduled items that only have a start date may be missed.",
                file=sys.stderr,
            )
        print(f"[debug] using JQL:\n{jql}\n")

    try:
        raw_issues = client.search_issues(jql=jql, fields=DEFAULT_FIELDS + extra_fields)
    except JiraAPIError as exc:
        if args.jql or "worklogDate" not in str(exc):
            raise
        jql = build_default_jql(
            start=start,
            end=end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            all_projects=all_projects,
            include_worklog_date=False,
            start_date_jql_name=start_date_field_meta["jql_name"] if start_date_field_meta else None,
        )
        if args.verbose:
            print(
                "[warn] worklogDate is unavailable in this Jira instance; retrying without worklogDate filter.",
                file=sys.stderr,
            )
            print(f"[debug] fallback JQL:\n{jql}\n")
        raw_issues = client.search_issues(jql=jql, fields=DEFAULT_FIELDS + extra_fields)

    if len(raw_issues) > args.max_issues:
        raise SystemExit(
            f"Safety cap exceeded: fetched {len(raw_issues)} issues, above --max-issues={args.max_issues}. "
            "Narrow your JQL."
        )

    normalized: list[IssueRecord] = []
    dev_set = set(dev_projects)
    planning_set = set(planning_projects)
    started_after_ms = to_epoch_millis(start)
    started_before_ms = to_epoch_millis(end)

    for raw in raw_issues:
        issue = normalize_issue(
            raw,
            base_url=base_url,
            dev_projects=dev_set,
            planning_projects=planning_set,
            start_date_field_id=start_date_field_meta["id"] if start_date_field_meta else None,
        )
        if args.include_worklogs:
            try:
                raw_worklogs = client.get_issue_worklogs(
                    issue.key,
                    started_after_ms=started_after_ms,
                    started_before_ms=started_before_ms,
                )
                issue.worklogs_today = extract_today_worklogs(raw_worklogs, start=start, end=end)
            except JiraAPIError as exc:
                if args.verbose:
                    print(f"[warn] failed to fetch worklogs for {issue.key}: {exc}", file=sys.stderr)
        normalized.append(issue)

    markdown = build_report_markdown(
        target_day=target_day,
        timezone_name=args.timezone,
        jql=jql,
        issues=normalized,
        include_worklogs=args.include_worklogs,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"jira_daily_report_{target_day.isoformat()}.md"
    output_path.write_text(markdown, encoding="utf-8")

    summary = {
        "date": target_day.isoformat(),
        "timezone": args.timezone,
        "issue_count": len(normalized),
        "output": str(output_path.resolve()),
        "teams": dict(Counter(issue.team for issue in normalized)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
