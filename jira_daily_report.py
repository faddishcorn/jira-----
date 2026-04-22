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
import re
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
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
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


class NotionAPIError(RuntimeError):
    """Raised when Notion returns an unexpected API response."""


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


class NotionClient:
    def __init__(self, api_key: str, *, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
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
        url = f"{NOTION_API_BASE}{path}"
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            raise NotionAPIError(f"Notion API error {response.status_code} for {url}: {payload}")
        return response.json()

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/data_sources/{data_source_id}")
        if isinstance(payload, dict):
            return payload
        raise NotionAPIError(f"Unexpected data source payload for {data_source_id}: {payload!r}")

    def retrieve_database(self, database_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/databases/{database_id}")
        if isinstance(payload, dict):
            return payload
        raise NotionAPIError(f"Unexpected database payload for {database_id}: {payload!r}")

    def query_data_source(
        self,
        data_source_id: str,
        *,
        filter_payload: dict[str, Any] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        next_cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": page_size, "result_type": "page"}
            if filter_payload:
                payload["filter"] = filter_payload
            if next_cursor:
                payload["start_cursor"] = next_cursor
            data = self._request("POST", f"/data_sources/{data_source_id}/query", json_body=payload)
            page_results = data.get("results", [])
            if isinstance(page_results, list):
                results.extend(item for item in page_results if isinstance(item, dict))
            if not data.get("has_more"):
                break
            next_cursor = data.get("next_cursor")
            if not next_cursor:
                break
        return results

    def create_page(
        self,
        *,
        data_source_id: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/pages",
            json_body={
                "parent": {"data_source_id": data_source_id},
                "properties": properties,
            },
        )

    def update_page(
        self,
        page_id: str,
        *,
        properties: dict[str, Any],
        erase_content: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"properties": properties}
        if erase_content:
            payload["erase_content"] = True
        return self._request("PATCH", f"/pages/{page_id}", json_body=payload)

    def append_block_children(self, block_id: str, children: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/blocks/{block_id}/children",
            json_body={"children": children},
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a one-day Jira markdown activity report.")
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Optional env file to load before reading environment variables (default: .env)",
    )
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
        "--morning-brief",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Build a morning brief focused on yesterday's work and today's plan (default: false)",
    )
    parser.add_argument(
        "--publish-notion",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Publish the generated report to a Notion data source (default: false)",
    )
    parser.add_argument(
        "--report-label",
        default="",
        help="Optional label appended to the report title, useful for variants like morning/evening",
    )
    parser.add_argument(
        "--notion-title",
        default="",
        help="Optional exact Notion page title override",
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


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_env_file(env_file: str, *, verbose: bool = False) -> Path | None:
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, strip_wrapping_quotes(value.strip()))

    if verbose:
        print(f"[debug] loaded env file: {env_path}", file=sys.stderr)
    return env_path


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


def build_activity_only_jql(
    *,
    start: datetime,
    end: datetime,
    dev_projects: list[str],
    planning_projects: list[str],
    all_projects: list[str],
    include_worklog_date: bool = True,
) -> str:
    return build_default_jql(
        start=start,
        end=end,
        dev_projects=dev_projects,
        planning_projects=planning_projects,
        all_projects=all_projects,
        include_worklog_date=include_worklog_date,
        start_date_jql_name=None,
    )


def build_start_date_only_jql(
    *,
    target_day: date,
    dev_projects: list[str],
    planning_projects: list[str],
    all_projects: list[str],
    start_date_jql_name: str | None,
) -> str:
    if not start_date_jql_name:
        return ""

    projects = all_projects or (dev_projects + planning_projects)
    escaped_name = start_date_jql_name.replace('"', '\\"')
    project_filter = ""
    if projects:
        project_clause = ", ".join(sorted(set(projects)))
        project_filter = f"project in ({project_clause})\nAND "

    return textwrap.dedent(
        f"""
        {project_filter}statusCategory != Done
        AND "{escaped_name}" = "{target_day.isoformat()}"
        ORDER BY assignee, priority DESC, updated DESC
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


def dedupe_issues_by_key(*issue_lists: Iterable[IssueRecord]) -> list[IssueRecord]:
    merged: dict[str, IssueRecord] = {}
    for issues in issue_lists:
        for issue in issues:
            existing = merged.get(issue.key)
            if not existing:
                merged[issue.key] = issue
                continue
            existing_score = (
                issue_activity_time(existing),
                bool(existing.start_date),
                existing.total_worklog_seconds_today,
            )
            candidate_score = (
                issue_activity_time(issue),
                bool(issue.start_date),
                issue.total_worklog_seconds_today,
            )
            if candidate_score >= existing_score:
                if issue.worklogs_today or not existing.worklogs_today:
                    merged[issue.key] = issue
    return list(merged.values())


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
    return add_dividers_under_h2("\n".join(lines).rstrip() + "\n")


def build_morning_brief_markdown(
    *,
    briefing_day: date,
    timezone_name: str,
    yesterday_issues: list[IssueRecord],
    today_focus_issues: list[IssueRecord],
    include_worklogs: bool,
) -> str:
    yesterday_day = briefing_day - timedelta(days=1)
    yesterday_done = sorted(
        (issue for issue in yesterday_issues if issue.is_done),
        key=issue_activity_time,
        reverse=True,
    )
    yesterday_active = sorted(
        (issue for issue in yesterday_issues if not issue.is_done),
        key=issue_activity_time,
        reverse=True,
    )
    today_focus = sorted(today_focus_issues, key=action_rank)
    today_started = [issue for issue in today_focus if issue.start_date == briefing_day]
    today_in_progress = [issue for issue in today_focus if issue.is_in_progress]
    today_todo = [issue for issue in today_focus if issue.status_category_key in TODO_STATUS_KEYS]
    today_people = sorted({issue.assignee for issue in today_focus if issue.assignee})
    yesterday_worklog_seconds = sum(issue.total_worklog_seconds_today for issue in yesterday_issues)

    lines: list[str] = [
        f"# Jira 아침 브리프 - {briefing_day.isoformat()}",
        "",
        f"- 어제 활동 이슈 수: **{len(yesterday_issues)}**",
        f"- 어제 완료 항목 수: **{len(yesterday_done)}**",
        f"- 오늘 확인할 항목 수: **{len(today_focus)}**",
        f"- 오늘 확인할 담당자 수: **{len(today_people)}**",
    ]
    if include_worklogs:
        lines.append(f"- 어제 작업 기록 시간: **{seconds_to_human(yesterday_worklog_seconds)}**")
    lines.append("")

    lines.extend(["## 전체 요약", ""])
    if not yesterday_issues and not today_focus:
        lines.extend(["- 어제 활동도 없었고, 오늘 바로 확인할 시작 예정 항목도 확인되지 않았습니다.", ""])
    else:
        summary_bits = [
            f"어제 {yesterday_day.isoformat()}에는 총 {len(yesterday_issues)}건의 활동이 있었고 완료는 {len(yesterday_done)}건입니다.",
            f"오늘 {briefing_day.isoformat()} 기준 확인할 항목은 {len(today_focus)}건입니다.",
        ]
        if today_started:
            summary_bits.append(f"오늘 시작 예정은 {len(today_started)}건입니다.")
        if today_in_progress:
            summary_bits.append(f"이미 진행중인 이어서 볼 항목은 {len(today_in_progress)}건입니다.")
        lines.append(f"- {' '.join(summary_bits)}")

        top_projects = Counter(issue.project_key for issue in yesterday_issues + today_focus).most_common(5)
        if top_projects:
            project_summary = ", ".join(f"{project} ({count})" for project, count in top_projects)
            lines.append(f"- 우선 봐야 할 프로젝트 분포: {project_summary}")
        if yesterday_done:
            done_preview = ", ".join(f"{issue.key}" for issue in yesterday_done[:5])
            lines.append(f"- 어제 완료된 대표 항목: {done_preview}")
        if today_focus:
            focus_preview = ", ".join(f"{issue.key}" for issue in today_focus[:5])
            lines.append(f"- 오늘 우선 확인 항목: {focus_preview}")
        lines.append("")

    lines.extend(["## 어제 완료된 항목", ""])
    if not yesterday_done:
        lines.extend(["- 어제 완료로 집계된 항목은 없습니다.", ""])
    else:
        for issue in yesterday_done:
            resolved_text = fmt_local(issue.resolutiondate or issue.updated, timezone_name)
            suffix = f" / 완료 {resolved_text}" if resolved_text else ""
            lines.append(
                f"- **{issue.key}** - {issue.summary} [{issue.status_name}] - {issue.project_key} ({issue.assignee}){suffix}"
            )
        lines.append("")

    lines.extend(["## 어제 진행되었지만 오늘 이어볼 항목", ""])
    carry_over_items = [issue for issue in yesterday_active if issue.key in {item.key for item in today_focus}]
    if not carry_over_items:
        lines.extend(["- 어제 활동한 미완료 항목 중 오늘 이어서 볼 항목은 없습니다.", ""])
    else:
        for issue in carry_over_items:
            lines.append(
                f"- **{issue.key}** - {issue.summary} [{issue.status_name}] / 우선순위={issue.priority}"
            )
            lines.append(f"  - {issue.project_key} / 담당자={issue.assignee} / 유형={issue.issue_type}")
            updated_text = fmt_local(issue.updated, timezone_name)
            if updated_text:
                lines.append(f"  - 최근 활동: {updated_text}")
            if issue.start_date:
                lines.append(f"  - 시작일: {issue.start_date.isoformat()}")
            lines.append(f"  - 링크: {issue.url}")
        lines.append("")

    lines.extend(["## 오늘 할 일", ""])
    if not today_focus:
        lines.extend(["- 오늘 시작 예정이거나 이어서 확인할 미완료 항목이 없습니다.", ""])
    else:
        for issue in today_focus:
            lines.append(
                f"- **{issue.key}** - {build_action_hint(issue)} / {issue.summary} [{issue.status_name}] / 우선순위={issue.priority}"
            )
            lines.append(f"  - {issue.project_key} / 담당자={issue.assignee} / 유형={issue.issue_type}")
            if issue.start_date:
                lines.append(f"  - 시작일: {issue.start_date.isoformat()}")
            updated_text = fmt_local(issue.updated, timezone_name)
            created_text = fmt_local(issue.created, timezone_name)
            if updated_text:
                lines.append(f"  - 최근 활동: {updated_text}")
            elif created_text:
                lines.append(f"  - 생성: {created_text}")
            if issue.labels:
                lines.append(f"  - 라벨: {', '.join(issue.labels)}")
            lines.append(f"  - 링크: {issue.url}")
        lines.append("")

    lines.extend(["## 담당자별 오늘 할 일", ""])
    if not today_focus:
        lines.extend(["- 배정된 오늘 할 일 항목이 없습니다.", ""])
    else:
        by_person: dict[str, list[IssueRecord]] = defaultdict(list)
        for issue in today_focus:
            by_person[issue.assignee or "미배정"].append(issue)
        for person in sorted(by_person.keys()):
            person_items = sorted(by_person[person], key=action_rank)
            lines.extend([f"### {person}", "", f"- 오늘 확인할 항목: **{len(person_items)}**", ""])
            for issue in person_items:
                lines.append(
                    f"- **{issue.key}** - {build_action_hint(issue)} / {issue.summary} [{issue.status_name}]"
                )
                lines.append(f"  - {issue.project_key} / 우선순위={issue.priority}")
                if issue.start_date:
                    lines.append(f"  - 시작일: {issue.start_date.isoformat()}")
                lines.append(f"  - 링크: {issue.url}")
            lines.append("")

    lines.extend(["## 오늘 관점 메모", ""])
    lines.append(
        f"- 오늘 보고서는 `{briefing_day.isoformat()}` 아침 기준으로 작성되었고, "
        f"어제 `{yesterday_day.isoformat()}` 활동과 오늘 시작 예정/이어지는 오픈 항목을 함께 보여줍니다."
    )
    lines.append(
        f"- 오늘 시작 예정 {len(today_started)}건 / 진행중 {len(today_in_progress)}건 / 착수 필요 {len(today_todo)}건"
    )
    lines.append("")
    return add_dividers_under_h2("\n".join(lines).rstrip() + "\n")


def build_report_title(target_day: date, report_label: str = "", *, morning_brief: bool = False) -> str:
    base_title = (
        f"Jira 아침 브리프 - {target_day.isoformat()}"
        if morning_brief
        else f"Jira 보고서 - {target_day.isoformat()}"
    )
    if report_label:
        return f"{base_title} - {report_label.strip()}"
    return base_title


def add_dividers_under_h2(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        output.append(line)
        if line.startswith("## "):
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            if next_line != "---":
                output.append("")
                output.append("---")
    return "\n".join(output).rstrip() + "\n"


def normalize_notion_id(value: str, *, kind: str) -> str:
    raw = value.strip()
    if not raw:
        return ""

    uuid_match = re.search(
        r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})",
        raw,
    )
    if uuid_match:
        compact = re.sub(r"[^0-9a-fA-F]", "", uuid_match.group(1))
        if len(compact) == 32:
            return (
                f"{compact[0:8]}-"
                f"{compact[8:12]}-"
                f"{compact[12:16]}-"
                f"{compact[16:20]}-"
                f"{compact[20:32]}"
            ).lower()

    raise SystemExit(
        f"Invalid {kind}: {value!r}. "
        f"Use a 32-character Notion ID, a hyphenated UUID, or paste the full Notion URL."
    )


def split_text_chunks(text: str, limit: int = 1800) -> list[str]:
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    chunks.append(remaining)
    return chunks


def append_rich_text_segment(
    segments: list[dict[str, Any]],
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    code: bool = False,
) -> None:
    if not text:
        return
    if segments:
        last = segments[-1]
        same_annotations = (
            last.get("annotations", {}).get("bold", False) == bold
            and last.get("annotations", {}).get("italic", False) == italic
            and last.get("annotations", {}).get("code", False) == code
        )
        if same_annotations:
            last["text"]["content"] += text
            return
    segments.append(
        {
            "type": "text",
            "text": {
                "content": text,
            },
            "annotations": {
                "bold": bold,
                "italic": italic,
                "code": code,
            },
        }
    )


def parse_inline_markdown_to_notion(text: str) -> list[dict[str, Any]]:
    safe_text = text if text else " "
    segments: list[dict[str, Any]] = []
    index = 0

    while index < len(safe_text):
        if safe_text.startswith("**", index):
            end = safe_text.find("**", index + 2)
            if end != -1 and end > index + 2:
                append_rich_text_segment(segments, safe_text[index + 2 : end], bold=True)
                index = end + 2
                continue
        if safe_text.startswith("*", index):
            end = safe_text.find("*", index + 1)
            if end != -1 and end > index + 1:
                append_rich_text_segment(segments, safe_text[index + 1 : end], italic=True)
                index = end + 1
                continue
        if safe_text.startswith("`", index):
            end = safe_text.find("`", index + 1)
            if end != -1 and end > index + 1:
                append_rich_text_segment(segments, safe_text[index + 1 : end], code=True)
                index = end + 1
                continue

        next_special_positions = [
            pos for pos in (safe_text.find("**", index), safe_text.find("*", index), safe_text.find("`", index)) if pos != -1
        ]
        next_special = min(next_special_positions) if next_special_positions else len(safe_text)
        if next_special == index:
            next_special += 1
        append_rich_text_segment(segments, safe_text[index:next_special])
        index = next_special

    if not segments:
        append_rich_text_segment(segments, " ")
    return segments


def build_notion_rich_text(text: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for segment in parse_inline_markdown_to_notion(text):
        annotations = segment.get("annotations", {})
        content = str((segment.get("text") or {}).get("content", " "))
        for chunk in split_text_chunks(content):
            output.append(
                {
                    "type": "text",
                    "text": {
                        "content": chunk,
                    },
                    "annotations": {
                        "bold": bool(annotations.get("bold", False)),
                        "italic": bool(annotations.get("italic", False)),
                        "code": bool(annotations.get("code", False)),
                    },
                }
            )
    return output or [{"type": "text", "text": {"content": " "}}]


def build_notion_text_block(block_type: str, text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": block_type,
        block_type: {
            "rich_text": build_notion_rich_text(text),
        },
    }


def count_indent_depth(raw_line: str) -> int:
    expanded = raw_line.expandtabs(4)
    leading_spaces = len(expanded) - len(expanded.lstrip(" "))
    return leading_spaces // 2


def append_notion_block(
    blocks: list[dict[str, Any]],
    list_stack: list[tuple[int, dict[str, Any]]],
    block: dict[str, Any],
    *,
    depth: int | None = None,
) -> None:
    if depth is None:
        list_stack.clear()
        blocks.append(block)
        return

    while list_stack and list_stack[-1][0] >= depth:
        list_stack.pop()

    if depth > 0 and list_stack:
        parent_block = list_stack[-1][1]
        parent_type = str(parent_block.get("type", ""))
        parent_payload = parent_block.get(parent_type)
        if isinstance(parent_payload, dict):
            parent_payload.setdefault("children", []).append(block)
        else:
            blocks.append(block)
    else:
        blocks.append(block)

    list_stack.append((depth, block))


def markdown_to_notion_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    code_lines: list[str] = []
    in_code_block = False
    list_stack: list[tuple[int, dict[str, Any]]] = []

    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                code_text = "\n".join(code_lines).rstrip() or " "
                blocks.append(
                    {
                        "object": "block",
                        "type": "code",
                        "code": {
                            "rich_text": build_notion_rich_text(code_text),
                            "language": "plain text",
                        },
                    }
                )
                list_stack.clear()
                code_lines = []
                in_code_block = False
            else:
                list_stack.clear()
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(raw_line)
            continue

        if not stripped:
            continue
        if stripped in {"---", "***", "___"}:
            append_notion_block(
                blocks,
                list_stack,
                {
                    "object": "block",
                    "type": "divider",
                    "divider": {},
                },
            )
            continue
        if raw_line.startswith("# "):
            append_notion_block(blocks, list_stack, build_notion_text_block("heading_1", raw_line[2:].strip()))
            continue
        if raw_line.startswith("## "):
            append_notion_block(blocks, list_stack, build_notion_text_block("heading_2", raw_line[3:].strip()))
            continue
        if raw_line.startswith("### "):
            append_notion_block(blocks, list_stack, build_notion_text_block("heading_3", raw_line[4:].strip()))
            continue
        if re.match(r"^\s*[-*]\s+", raw_line):
            text = re.sub(r"^\s*[-*]\s+", "", raw_line).strip()
            append_notion_block(
                blocks,
                list_stack,
                build_notion_text_block("bulleted_list_item", text),
                depth=count_indent_depth(raw_line),
            )
            continue
        if re.match(r"^\s*\d+\.\s+", raw_line):
            text = re.sub(r"^\s*\d+\.\s+", "", raw_line).strip()
            append_notion_block(
                blocks,
                list_stack,
                build_notion_text_block("numbered_list_item", text),
                depth=count_indent_depth(raw_line),
            )
            continue
        append_notion_block(blocks, list_stack, build_notion_text_block("paragraph", stripped))

    if code_lines:
        code_text = "\n".join(code_lines).rstrip() or " "
        blocks.append(
            {
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": build_notion_rich_text(code_text),
                    "language": "plain text",
                },
            }
        )

    return blocks


def extract_notion_plain_text(rich_items: Iterable[dict[str, Any]]) -> str:
    return "".join(str(item.get("plain_text", "")) for item in rich_items if isinstance(item, dict)).strip()


def describe_notion_properties(properties: dict[str, Any]) -> str:
    pairs = []
    for name, meta in properties.items():
        if isinstance(meta, dict):
            pairs.append(f"{name} ({meta.get('type', 'unknown')})")
    return ", ".join(sorted(pairs))


def resolve_notion_property_names(
    *,
    data_source: dict[str, Any],
    title_property_hint: str,
    date_property_hint: str,
) -> tuple[str, str]:
    properties = data_source.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise NotionAPIError("The Notion data source has no readable properties.")

    title_property_name = title_property_hint
    if not title_property_name:
        for name, meta in properties.items():
            if isinstance(meta, dict) and meta.get("type") == "title":
                title_property_name = name
                break
    if not title_property_name or title_property_name not in properties:
        raise NotionAPIError(
            "Could not find the title property in the Notion data source. "
            f"Available properties: {describe_notion_properties(properties)}"
        )

    date_property_name = date_property_hint
    if not date_property_name:
        preferred_names = ("날짜", "Date", "일자", "보고일", "Report Date")
        for preferred_name in preferred_names:
            meta = properties.get(preferred_name)
            if isinstance(meta, dict) and meta.get("type") == "date":
                date_property_name = preferred_name
                break
    if not date_property_name:
        date_candidates = [
            name for name, meta in properties.items() if isinstance(meta, dict) and meta.get("type") == "date"
        ]
        if len(date_candidates) == 1:
            date_property_name = date_candidates[0]
    if not date_property_name or date_property_name not in properties:
        raise NotionAPIError(
            "Could not find a date property in the Notion data source. "
            "Set NOTION_DATE_PROPERTY if the field name is unusual. "
            f"Available properties: {describe_notion_properties(properties)}"
        )
    if properties[date_property_name].get("type") != "date":
        raise NotionAPIError(
            f"Property '{date_property_name}' exists but is not a date field. "
            f"Available properties: {describe_notion_properties(properties)}"
        )

    return title_property_name, date_property_name


def build_notion_page_properties(
    *,
    title_property_name: str,
    date_property_name: str,
    page_title: str,
    target_day: date,
) -> dict[str, Any]:
    return {
        title_property_name: {
            "title": build_notion_rich_text(page_title),
        },
        date_property_name: {
            "date": {
                "start": target_day.isoformat(),
            }
        },
    }


def resolve_notion_data_source(
    notion_client: NotionClient,
    *,
    data_source_id: str,
    database_id: str,
) -> tuple[str, dict[str, Any]]:
    if data_source_id:
        try:
            return data_source_id, notion_client.retrieve_data_source(data_source_id)
        except NotionAPIError as exc:
            # People often paste a database URL into NOTION_DATA_SOURCE_ID by mistake.
            # If that happens, try resolving it as a database before failing hard.
            if "object_not_found" not in str(exc):
                raise
            try:
                database = notion_client.retrieve_database(data_source_id)
            except NotionAPIError:
                raise exc
            data_sources = database.get("data_sources") or []
            candidates = [item for item in data_sources if isinstance(item, dict) and item.get("id")]
            if not candidates:
                raise NotionAPIError(
                    "The value in NOTION_DATA_SOURCE_ID appears to point to a database, "
                    "but no child data sources were found. "
                    "Check whether this is the original database and whether the integration has access."
                ) from exc
            if len(candidates) > 1:
                names = ", ".join(str(item.get("name", "(unnamed)")) for item in candidates)
                raise NotionAPIError(
                    "The value in NOTION_DATA_SOURCE_ID appears to be a database ID, "
                    "and that database has multiple data sources. "
                    f"Set the exact data source ID instead. Available data sources: {names}"
                ) from exc
            resolved_data_source_id = str(candidates[0]["id"])
            return resolved_data_source_id, notion_client.retrieve_data_source(resolved_data_source_id)

    if not database_id:
        raise SystemExit("Missing environment variable: NOTION_DATA_SOURCE_ID or NOTION_DATABASE_ID")

    database = notion_client.retrieve_database(database_id)
    data_sources = database.get("data_sources") or []
    candidates = [item for item in data_sources if isinstance(item, dict) and item.get("id")]
    if not candidates:
        raise NotionAPIError(
            "No data sources were found under the specified Notion database. "
            "Check whether this page is really a database and whether the integration has access."
        )
    if len(candidates) > 1:
        names = ", ".join(str(item.get("name", "(unnamed)")) for item in candidates)
        raise NotionAPIError(
            "Multiple data sources were found under the specified Notion database. "
            f"Set NOTION_DATA_SOURCE_ID explicitly. Available data sources: {names}"
        )

    resolved_data_source_id = str(candidates[0]["id"])
    return resolved_data_source_id, notion_client.retrieve_data_source(resolved_data_source_id)


def find_existing_notion_page(
    *,
    notion_client: NotionClient,
    data_source_id: str,
    date_property_name: str,
    title_property_name: str,
    target_day: date,
    page_title: str,
) -> dict[str, Any] | None:
    pages = notion_client.query_data_source(
        data_source_id,
        filter_payload={
            "property": date_property_name,
            "date": {
                "equals": target_day.isoformat(),
            },
        },
    )
    for page in pages:
        properties = page.get("properties") or {}
        title_property = properties.get(title_property_name) or {}
        title_items = title_property.get("title") or []
        current_title = extract_notion_plain_text(title_items)
        if current_title == page_title:
            return page
    return None


def fetch_normalized_issues(
    *,
    client: JiraClient,
    base_url: str,
    jql: str,
    fields: list[str],
    verbose: bool,
    timezone_name: str,
    start: datetime,
    end: datetime,
    dev_projects: list[str],
    planning_projects: list[str],
    start_date_field_id: str | None,
    include_worklogs: bool,
    max_issues: int,
    allow_worklog_fallback: bool = False,
    worklog_fallback_jql: str = "",
) -> list[IssueRecord]:
    try:
        raw_issues = client.search_issues(jql=jql, fields=fields)
    except JiraAPIError as exc:
        if not allow_worklog_fallback or "worklogDate" not in str(exc) or not worklog_fallback_jql:
            raise
        if verbose:
            print(
                "[warn] worklogDate is unavailable in this Jira instance; retrying without worklogDate filter.",
                file=sys.stderr,
            )
            print(f"[debug] fallback JQL:\n{worklog_fallback_jql}\n")
        raw_issues = client.search_issues(jql=worklog_fallback_jql, fields=fields)

    if len(raw_issues) > max_issues:
        raise SystemExit(
            f"Safety cap exceeded: fetched {len(raw_issues)} issues, above --max-issues={max_issues}. "
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
            start_date_field_id=start_date_field_id,
        )
        if include_worklogs:
            try:
                raw_worklogs = client.get_issue_worklogs(
                    issue.key,
                    started_after_ms=started_after_ms,
                    started_before_ms=started_before_ms,
                )
                issue.worklogs_today = extract_today_worklogs(raw_worklogs, start=start, end=end)
            except JiraAPIError as exc:
                if verbose:
                    print(f"[warn] failed to fetch worklogs for {issue.key}: {exc}", file=sys.stderr)
        normalized.append(issue)

    return normalized


def publish_report_to_notion(
    *,
    markdown: str,
    target_day: date,
    report_title: str,
    verbose: bool,
) -> dict[str, Any]:
    notion_api_key = require_env("NOTION_API_KEY")
    data_source_id = normalize_notion_id(get_env("NOTION_DATA_SOURCE_ID"), kind="NOTION_DATA_SOURCE_ID")
    database_id = normalize_notion_id(get_env("NOTION_DATABASE_ID"), kind="NOTION_DATABASE_ID")
    title_property_hint = get_env("NOTION_TITLE_PROPERTY")
    date_property_hint = get_env("NOTION_DATE_PROPERTY")

    notion_client = NotionClient(notion_api_key)
    resolved_data_source_id, data_source = resolve_notion_data_source(
        notion_client,
        data_source_id=data_source_id,
        database_id=database_id,
    )
    title_property_name, date_property_name = resolve_notion_property_names(
        data_source=data_source,
        title_property_hint=title_property_hint,
        date_property_hint=date_property_hint,
    )

    if verbose:
        print(
            f"[debug] Notion property mapping: title={title_property_name}, date={date_property_name}",
            file=sys.stderr,
        )

    page_properties = build_notion_page_properties(
        title_property_name=title_property_name,
        date_property_name=date_property_name,
        page_title=report_title,
        target_day=target_day,
    )
    existing_page = find_existing_notion_page(
        notion_client=notion_client,
        data_source_id=resolved_data_source_id,
        date_property_name=date_property_name,
        title_property_name=title_property_name,
        target_day=target_day,
        page_title=report_title,
    )

    if existing_page:
        page_id = str(existing_page.get("id", ""))
        page = notion_client.update_page(page_id, properties=page_properties, erase_content=True)
        mode = "updated"
    else:
        page = notion_client.create_page(data_source_id=resolved_data_source_id, properties=page_properties)
        page_id = str(page.get("id", ""))
        mode = "created"

    blocks = markdown_to_notion_blocks(markdown)
    for index in range(0, len(blocks), 100):
        notion_client.append_block_children(page_id, blocks[index : index + 100])

    return {
        "mode": mode,
        "page_id": page_id,
        "url": page.get("url", ""),
        "title": report_title,
        "date": target_day.isoformat(),
        "property_mapping": {
            "title": title_property_name,
            "date": date_property_name,
        },
        "data_source_id": resolved_data_source_id,
    }


def main() -> None:
    args = parse_args()
    load_env_file(args.env_file, verbose=args.verbose)

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

    fields = DEFAULT_FIELDS + extra_fields
    summary_teams: dict[str, int]

    if args.morning_brief:
        yesterday_day = target_day - timedelta(days=1)
        yesterday_start, yesterday_end = get_day_bounds(yesterday_day, args.timezone)
        yesterday_jql = args.jql or build_activity_only_jql(
            start=yesterday_start,
            end=yesterday_end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            all_projects=all_projects,
        )
        yesterday_fallback_jql = (
            ""
            if args.jql
            else build_activity_only_jql(
                start=yesterday_start,
                end=yesterday_end,
                dev_projects=dev_projects,
                planning_projects=planning_projects,
                all_projects=all_projects,
                include_worklog_date=False,
            )
        )
        today_plan_jql = build_start_date_only_jql(
            target_day=target_day,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            all_projects=all_projects,
            start_date_jql_name=start_date_field_meta["jql_name"] if start_date_field_meta else None,
        )

        if args.verbose:
            print(f"[debug] morning brief - yesterday JQL:\n{yesterday_jql}\n")
            if today_plan_jql:
                print(f"[debug] morning brief - today plan JQL:\n{today_plan_jql}\n")

        yesterday_issues = fetch_normalized_issues(
            client=client,
            base_url=base_url,
            jql=yesterday_jql,
            fields=fields,
            verbose=args.verbose,
            timezone_name=args.timezone,
            start=yesterday_start,
            end=yesterday_end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            start_date_field_id=start_date_field_meta["id"] if start_date_field_meta else None,
            include_worklogs=args.include_worklogs,
            max_issues=args.max_issues,
            allow_worklog_fallback=not bool(args.jql),
            worklog_fallback_jql=yesterday_fallback_jql,
        )
        today_plan_issues = (
            fetch_normalized_issues(
                client=client,
                base_url=base_url,
                jql=today_plan_jql,
                fields=fields,
                verbose=args.verbose,
                timezone_name=args.timezone,
                start=start,
                end=end,
                dev_projects=dev_projects,
                planning_projects=planning_projects,
                start_date_field_id=start_date_field_meta["id"] if start_date_field_meta else None,
                include_worklogs=False,
                max_issues=args.max_issues,
            )
            if today_plan_jql
            else []
        )
        carry_over_issues = [issue for issue in yesterday_issues if not issue.is_done]
        today_focus_issues = dedupe_issues_by_key(carry_over_issues, today_plan_issues)
        markdown = build_morning_brief_markdown(
            briefing_day=target_day,
            timezone_name=args.timezone,
            yesterday_issues=yesterday_issues,
            today_focus_issues=today_focus_issues,
            include_worklogs=args.include_worklogs,
        )
        summary_teams = dict(Counter(issue.team for issue in dedupe_issues_by_key(yesterday_issues, today_focus_issues)))
        issue_count = len(yesterday_issues)
        today_focus_count = len(today_focus_issues)
    else:
        auto_jql = build_default_jql(
            start=start,
            end=end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            all_projects=all_projects,
            start_date_jql_name=start_date_field_meta["jql_name"] if start_date_field_meta else None,
        )
        fallback_jql = build_default_jql(
            start=start,
            end=end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            all_projects=all_projects,
            include_worklog_date=False,
            start_date_jql_name=start_date_field_meta["jql_name"] if start_date_field_meta else None,
        )
        jql = args.jql or auto_jql

        if args.verbose:
            print(f"[debug] using JQL:\n{jql}\n")

        normalized = fetch_normalized_issues(
            client=client,
            base_url=base_url,
            jql=jql,
            fields=fields,
            verbose=args.verbose,
            timezone_name=args.timezone,
            start=start,
            end=end,
            dev_projects=dev_projects,
            planning_projects=planning_projects,
            start_date_field_id=start_date_field_meta["id"] if start_date_field_meta else None,
            include_worklogs=args.include_worklogs,
            max_issues=args.max_issues,
            allow_worklog_fallback=not bool(args.jql),
            worklog_fallback_jql=fallback_jql,
        )
        markdown = build_report_markdown(
            target_day=target_day,
            timezone_name=args.timezone,
            issues=normalized,
            include_worklogs=args.include_worklogs,
        )
        summary_teams = dict(Counter(issue.team for issue in normalized))
        issue_count = len(normalized)
        today_focus_count = 0

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_title = args.notion_title.strip() or build_report_title(
        target_day,
        args.report_label,
        morning_brief=args.morning_brief,
    )
    report_filename_suffix = target_day.isoformat()
    if args.report_label.strip():
        safe_label = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", args.report_label.strip()).strip("_")
        if safe_label:
            report_filename_suffix = f"{report_filename_suffix}_{safe_label}"
    if args.morning_brief:
        report_filename_suffix = f"{report_filename_suffix}_morning_brief"
    output_path = output_dir / f"jira_daily_report_{report_filename_suffix}.md"
    output_path.write_text(markdown, encoding="utf-8")

    summary = {
        "date": target_day.isoformat(),
        "timezone": args.timezone,
        "title": report_title,
        "issue_count": issue_count,
        "output": str(output_path.resolve()),
        "teams": summary_teams,
    }
    if args.morning_brief:
        summary["today_focus_count"] = today_focus_count

    if args.publish_notion:
        notion_summary = publish_report_to_notion(
            markdown=markdown,
            target_day=target_day,
            report_title=report_title,
            verbose=args.verbose,
        )
        summary["notion"] = notion_summary

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
