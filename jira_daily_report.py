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
    return "Unassigned"


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
        summary=(fields.get("summary") or "(no summary)").strip(),
        team=classify_team(project_key, dev_projects, planning_projects),
        project_key=project_key,
        issue_type=issue_type.get("name", "Unknown"),
        status_name=status.get("name", "Unknown"),
        status_category_key=status_category.get("key", "unknown"),
        assignee=assignee.get("displayName") or "Unassigned",
        reporter=reporter.get("displayName") or "Unknown",
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
                author=(raw.get("author") or {}).get("displayName", "Unknown"),
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
        f"type={issue.issue_type}",
        f"status={issue.status_name}",
        f"priority={issue.priority}",
    ]
    if issue.components:
        meta_bits.append(f"components={', '.join(issue.components)}")
    if issue.labels:
        meta_bits.append(f"labels={', '.join(issue.labels)}")
    lines.append(f"  - {' / '.join(meta_bits)}")
    lines.append(f"  - assignee={issue.assignee} / reporter={issue.reporter}")

    if issue.start_date:
        lines.append(f"  - start date: {issue.start_date.isoformat()}")

    created_text = fmt_local(issue.created, timezone_name)
    updated_text = fmt_local(issue.updated, timezone_name)
    resolved_text = fmt_local(issue.resolutiondate, timezone_name)
    if created_text:
        lines.append(f"  - created: {created_text}")
    if updated_text:
        lines.append(f"  - updated: {updated_text}")
    if resolved_text:
        lines.append(f"  - resolved: {resolved_text}")

    lines.append(f"  - link: {issue.url}")

    if include_worklogs and person_worklogs:
        worklog_seconds = sum(item.time_spent_seconds for item in person_worklogs)
        lines.append(f"  - work logged: {seconds_to_human(worklog_seconds)}")
        for worklog in person_worklogs:
            comment = f" / {worklog.comment}" if worklog.comment else ""
            started_text = worklog.started.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
            lines.append(f"    - {started_text} / {seconds_to_human(worklog.time_spent_seconds)}{comment}")
    elif include_worklogs and issue.worklogs_today:
        lines.append(f"  - total worklogs today: {seconds_to_human(issue.total_worklog_seconds_today)}")

    return lines


def build_action_hint(issue: IssueRecord) -> str:
    if issue.is_in_progress:
        return "continue"
    if issue.status_category_key in TODO_STATUS_KEYS:
        return "pick up"
    return "check"


def action_rank(issue: IssueRecord) -> tuple[int, int, datetime]:
    status_rank = 0 if issue.is_in_progress else 1 if issue.status_category_key in TODO_STATUS_KEYS else 2
    priority_rank_map = {
        "Highest": 0,
        "High": 1,
        "Medium": 2,
        "Low": 3,
        "Lowest": 4,
    }
    priority_rank = priority_rank_map.get(issue.priority, 5)
    recency = issue.updated or issue.created or datetime.min.replace(tzinfo=timezone.utc)
    return (status_rank, priority_rank, recency)


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
        contributor_names = {issue.assignee for issue in issues if issue.assignee != "Unassigned"}
    total_people = len(contributor_names)
    done_count = sum(1 for issue in issues if issue.is_done)
    in_progress_count = sum(1 for issue in issues if issue.is_in_progress)
    todo_count = sum(1 for issue in issues if issue.status_category_key in TODO_STATUS_KEYS)
    unknown_count = total_issues - done_count - in_progress_count - todo_count
    total_worklog_seconds = sum(issue.total_worklog_seconds_today for issue in issues)

    lines: list[str] = [
        f"# Jira Daily Activity Report - {target_day.isoformat()}",
        "",
        f"- Timezone: `{timezone_name}`",
        f"- Total touched issues: **{total_issues}**",
        f"- Distinct contributors: **{total_people}**",
        (
            f"- Done: **{done_count}** / In progress: **{in_progress_count}** / "
            f"To do: **{todo_count}** / Other: **{unknown_count}**"
        ),
    ]
    if include_worklogs:
        lines.append(f"- Logged time on the day: **{seconds_to_human(total_worklog_seconds)}**")

    lines.extend(["", "## Query used", "", "```jql", jql, "```", ""])

    if not issues:
        lines.extend(["## Summary", "", "No matching Jira activity was found on the selected date."])
        return "\n".join(lines) + "\n"

    lines.extend(["## Overall summary", ""])
    top_projects = Counter(issue.project_key for issue in issues).most_common(5)
    if top_projects:
        project_summary = ", ".join(f"{project} ({count})" for project, count in top_projects)
        lines.append(f"- Most active projects: {project_summary}")
    top_statuses = Counter(issue.status_name for issue in issues).most_common(5)
    if top_statuses:
        status_summary = ", ".join(f"{status} ({count})" for status, count in top_statuses)
        lines.append(f"- Status distribution highlights: {status_summary}")
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
            lines.append(f"- Top logged contributors: {worker_summary}")
    lines.append("")

    lines.extend(["## All activity items", ""])
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
                by_author[worklog.author or "Unknown"].append(worklog)
            for author, author_worklogs in by_author.items():
                person_entries[author].append((issue, sorted(author_worklogs, key=lambda item: item.started)))
        else:
            person_entries[issue.assignee or "Unassigned"].append((issue, []))

    lines.extend(["## Activity by person", ""])
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
                f"- Related issues: **{touched_issue_count}** / Logged time: **{seconds_to_human(person_work_seconds)}**"
            )
        else:
            lines.append(f"- Related issues: **{touched_issue_count}**")
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
    lines.extend(["## Today's suggested next items by assignee", ""])
    if not actionable_issues:
        lines.extend(["- No open items found from the selected date's activity.", ""])
    else:
        actionable_by_person: dict[str, list[IssueRecord]] = defaultdict(list)
        for issue in actionable_issues:
            actionable_by_person[issue.assignee or "Unassigned"].append(issue)

        for person in sorted(actionable_by_person.keys()):
            person_items = sorted(actionable_by_person[person], key=action_rank)
            lines.extend([f"### {person}", "", f"- Open items to look at today: **{len(person_items)}**", ""])
            for issue in person_items:
                lines.append(
                    f"- **{issue.key}** - {build_action_hint(issue)} / "
                    f"{issue.summary} [{issue.status_name}] / priority={issue.priority}"
                )
                lines.append(f"  - {issue.project_key} / type={issue.issue_type} / reporter={issue.reporter}")
                if issue.start_date:
                    lines.append(f"  - start date: {issue.start_date.isoformat()}")
                updated_text = fmt_local(issue.updated, timezone_name)
                created_text = fmt_local(issue.created, timezone_name)
                if updated_text:
                    lines.append(f"  - latest activity: {updated_text}")
                elif created_text:
                    lines.append(f"  - created: {created_text}")
                if issue.components:
                    lines.append(f"  - components: {', '.join(issue.components)}")
                if issue.labels:
                    lines.append(f"  - labels: {', '.join(issue.labels)}")
                lines.append(f"  - link: {issue.url}")
            lines.append("")

    completed = [issue for issue in issues if issue.is_done]
    in_progress = [issue for issue in issues if issue.is_in_progress]
    backlogish = [issue for issue in issues if issue.status_category_key in TODO_STATUS_KEYS]

    def add_issue_section(title: str, items: list[IssueRecord]) -> None:
        lines.extend([f"## {title}", ""])
        if not items:
            lines.extend(["- None", ""])
            return
        for issue in items:
            suffix = f" ({issue.assignee})" if issue.assignee else ""
            lines.append(f"- **{issue.key}** - {issue.summary} [{issue.status_name}] - {issue.project_key}{suffix}")
        lines.append("")

    add_issue_section("Done items", completed)
    add_issue_section("In-progress items", in_progress)
    add_issue_section("To-do or waiting items", backlogish)
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
