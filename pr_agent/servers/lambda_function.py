"""
Unified Lambda handler for CodeCommit PR automation.

Handles two concerns:
  1. Linting — starts a CodeBuild job on PR/push events; approves or revokes
     the PR based on build outcome.
  2. PR Agent — runs LLM-powered review ONLY after linting passes (SUCCEEDED),
     and ONLY once per PR revision (idempotency via a marker comment).

Event routing:
  "Records" key present               → _handle_codecommit_trigger  (legacy SNS)
  detail-type = CodeBuild Build State Change              → _handle_build_state_change
  detail-type = CodeCommit Pull Request State Change      → _handle_pr_event
  detail-type = CodeCommit Repository State Change        → _handle_push_event

Linting env vars:
  BACKEND_CODEBUILD_PROJECT   — CodeBuild project for backend repos
  FRONTEND_CODEBUILD_PROJECT  — CodeBuild project for frontend repos
  ALLOWED_REPOS               — comma-separated allow-list (empty = all repos)
  TARGET_BASE_BRANCHES        — comma-separated base branches (default: main)

LLM env vars (Dynaconf double-underscore syntax):
  ANTHROPIC__KEY, OPENAI__KEY, CONFIG__MODEL, CONFIG__LOG_LEVEL, etc.

AWS credentials:
  Not needed in production — Lambda execution role is used automatically.
  For local testing (Lambda RIE): pass AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
  AWS_DEFAULT_REGION as container env vars.
"""

import asyncio
import copy
import json
import os
from typing import Optional, Set

import boto3

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger

# ---------------------------------------------------------------------------
# Bootstrap: configure logging once at cold-start time.
# ---------------------------------------------------------------------------
setup_logger(fmt=LoggingFormat.JSON, level=os.environ.get("CONFIG__LOG_LEVEL", "INFO"))

logger = get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# PR agent
_CODECOMMIT_CONSOLE_BASE = (
    "https://{region}.console.aws.amazon.com/codesuite/codecommit"
    "/repositories/{repo}/pull-requests/{pr_id}"
)
_DEFAULT_AUTO_COMMANDS = ["review"]

# Idempotency marker — written as a PR comment after a successful agent review.
# HTML comment syntax keeps it invisible in most renderers.
_PR_AGENT_MARKER_PREFIX = "<!-- pr-agent-reviewed:"

# Linting
_SUPPORTED_PR_EVENTS = {"pullRequestCreated", "pullRequestSourceBranchUpdated"}

# ── Linting env vars (read once at cold-start) ──────────────────
CC_BACKEND_PROJECT   = os.environ.get("CC_BACKEND_CODEBUILD_PROJECT", "").strip()
CC_FRONTEND_PROJECT  = os.environ.get("CC_FRONTEND_CODEBUILD_PROJECT", "").strip()
FYLZ_BACKEND_PROJECT = os.environ.get("FYLZ_BACKEND_CODEBUILD_PROJECT", "").strip()
FYLZ_UNIFIED_WORKER_PROJECT = os.environ.get("FYLZ_UNIFIED_WORKER_CODEBUILD_PROJECT", "").strip()
FYLZ_FRONTEND_PROJECT = os.environ.get("FYLZ_FRONTEND_CODEBUILD_PROJECT", "").strip()

# ── Release build env vars (read once at cold-start) ────────────
CC_BACKEND_RELEASE_PROJECT        = os.environ.get("CC_BACKEND_RELEASE_CODEBUILD_PROJECT", "").strip()
CC_FRONTEND_RELEASE_PROJECT       = os.environ.get("CC_FRONTEND_RELEASE_CODEBUILD_PROJECT", "").strip()
FYLZ_BACKEND_RELEASE_PROJECT      = os.environ.get("FYLZ_BACKEND_RELEASE_CODEBUILD_PROJECT", "").strip()
FYLZ_UNIFIED_WORKER_RELEASE_PROJECT = os.environ.get("FYLZ_UNIFIED_WORKER_RELEASE_CODEBUILD_PROJECT", "").strip()
FYLZ_FRONTEND_RELEASE_PROJECT     = os.environ.get("FYLZ_FRONTEND_RELEASE_CODEBUILD_PROJECT", "").strip()

# Branch prefix that identifies a release branch (override via RELEASE_BRANCH_PREFIX env var)
RELEASE_BRANCH_PREFIX = os.environ.get("RELEASE_BRANCH_PREFIX", "release/").strip()

REPO_PROJECT_MAP = {
    "cc-backend":               (CC_BACKEND_PROJECT, "backend"),
    "cc-frontend":              (CC_FRONTEND_PROJECT, "frontend"),
    "fylz-bank-connect-py":     (FYLZ_BACKEND_PROJECT, "backend"),
    "fylz-unified-worker":      (FYLZ_UNIFIED_WORKER_PROJECT, "backend"),
    "fylz-bank-connect-ui-v2":  (FYLZ_FRONTEND_PROJECT, "frontend"),
}

# Maps CodeCommit repo name → (gate_project, gate_repo) as used in the S3 key scheme.
# gate_project: fylz | carrier-connect | 1987
# gate_repo:    backend | frontend | lambda-unified | ...
REPO_S3_GATE_MAP = {
    "cc-backend":               ("carrier-connect", "backend"),
    "cc-frontend":              ("carrier-connect", "frontend"),
    "fylz-bank-connect-py":     ("fylz",            "backend"),
    "fylz-unified-worker":      ("fylz",            "lambda-unified"),
    "fylz-bank-connect-ui-v2":  ("fylz",            "frontend"),
}

# Maps each repo to its dedicated release CodeBuild project (same source_identifier convention).
REPO_RELEASE_PROJECT_MAP = {
    "cc-backend":               (CC_BACKEND_RELEASE_PROJECT, "backend"),
    "cc-frontend":              (CC_FRONTEND_RELEASE_PROJECT, "frontend"),
    "fylz-bank-connect-py":     (FYLZ_BACKEND_RELEASE_PROJECT, "backend"),
    "fylz-unified-worker":      (FYLZ_UNIFIED_WORKER_RELEASE_PROJECT, "backend"),
    "fylz-bank-connect-ui-v2":  (FYLZ_FRONTEND_RELEASE_PROJECT, "frontend"),
}

# ---------------------------------------------------------------------------
# boto3 client cache  (reused across warm Lambda invocations)
# ---------------------------------------------------------------------------

_cc_client = None
_cb_client = None
_s3_client = None

GATE_RESULT_BUCKET = os.environ.get("GATE_RESULT_S3_BUCKET", "untie-it-codebuild-artifacts").strip()


def _get_codecommit_client():
    global _cc_client
    if _cc_client is None:
        _cc_client = boto3.client("codecommit")
    return _cc_client


def _get_codebuild_client():
    global _cb_client
    if _cb_client is None:
        _cb_client = boto3.client("codebuild")
    return _cb_client


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _extract_branch_name(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def _build_pr_url(region: str, repo_name: str, pr_id: str) -> str:
    """Construct the CodeCommit console URL for a pull request."""
    return _CODECOMMIT_CONSOLE_BASE.format(
        region=region,
        repo=repo_name,
        pr_id=pr_id,
    )


# ---------------------------------------------------------------------------
# Linting helpers
# ---------------------------------------------------------------------------

def _get_project_and_identifier(repo_name: str):
    """
    Maps a repo name to (codebuild_project, source_identifier).
    Returns (None, None) if the repo is not mapped.
    """
    return REPO_PROJECT_MAP.get(repo_name, (None, None))


def _get_release_project_and_identifier(repo_name: str):
    """
    Maps a repo name to its release (codebuild_project, source_identifier).
    Returns (None, None) if the repo has no release project configured.
    """
    return REPO_RELEASE_PROJECT_MAP.get(repo_name, (None, None))


def _get_build_env_var(env_vars: list, name: str) -> Optional[str]:
    for var in env_vars:
        if var.get("name") == name:
            return var.get("value")
    return None


def _has_open_pr_to_target(repo_name: str, source_branch: str, target_branches: Set[str]) -> bool:
    """Returns True if an OPEN PR exists where source == source_branch and dest in target_branches."""
    cc = _get_codecommit_client()
    next_token = None
    while True:
        kwargs = {"repositoryName": repo_name, "pullRequestStatus": "OPEN"}
        if next_token:
            kwargs["nextToken"] = next_token
        response = cc.list_pull_requests(**kwargs)
        for pr_id in response.get("pullRequestIds", []):
            pr = cc.get_pull_request(pullRequestId=pr_id)["pullRequest"]
            for target in pr.get("pullRequestTargets", []):
                src = _extract_branch_name(target.get("sourceReference"))
                dst = _extract_branch_name(target.get("destinationReference"))
                if src == source_branch and dst in target_branches:
                    return True
        next_token = response.get("nextToken")
        if not next_token:
            break
    return False


def _get_open_pr(repo_name: str, source_branch: str) -> Optional[dict]:
    """Returns PR details dict if an OPEN PR exists for the given source branch targeting TARGET_BASE_BRANCHES."""
    target_branches = _get_target_base_branches()
    cc = _get_codecommit_client()
    next_token = None
    while True:
        kwargs = {"repositoryName": repo_name, "pullRequestStatus": "OPEN"}
        if next_token:
            kwargs["nextToken"] = next_token
        response = cc.list_pull_requests(**kwargs)
        for pr_id in response.get("pullRequestIds", []):
            pr = cc.get_pull_request(pullRequestId=pr_id)["pullRequest"]
            for target in pr.get("pullRequestTargets", []):
                src = _extract_branch_name(target.get("sourceReference"))
                dst = _extract_branch_name(target.get("destinationReference"))
                if src == source_branch and dst in target_branches:
                    return {
                        "pullRequestId":    pr_id,
                        "revisionId":       pr.get("revisionId"),
                        "sourceBranch":     src,
                        "destBranch":       dst,
                        "sourceCommit":     target.get("sourceCommit", ""),
                        "destinationCommit": target.get("destinationCommit", ""),
                    }
        next_token = response.get("nextToken")
        if not next_token:
            break
    return None


def _get_allowed_repos() -> set:
    raw = os.environ.get("ALLOWED_REPOS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _get_target_base_branches() -> set:
    raw = os.environ.get("TARGET_BASE_BRANCHES", "main")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _get_allowed_projects() -> set:
    """Returns the set of CodeBuild project names this Lambda is allowed to process."""
    allowed_repos = _get_allowed_repos()
    repos = allowed_repos if allowed_repos else REPO_PROJECT_MAP.keys()
    return {
        REPO_PROJECT_MAP[repo][0]
        for repo in repos
        if repo in REPO_PROJECT_MAP
    }


def _start_build(
    repo_name: str,
    source_branch: str,
    commit_id: str,
    trigger_name: str,
    pr_id: str = "",
    revision_id: str = "",
    destination_commit: str = "",
) -> dict:
    """
    Starts a CodeBuild build for the given repo/branch.
    PR context (pr_id, revision_id, destination_commit) is injected as env vars
    so _handle_build_state_change can recover them and trigger the PR agent.
    """
    project, source_identifier = _get_project_and_identifier(repo_name)

    if not project:
        logger.warning(f"No CodeBuild project mapped for repo '{repo_name}' — skipping build")
        return {"status": "skipped", "reason": "No project mapped for repo", "repo": repo_name}

    env_vars = [
        {"name": "REPO_NAME",     "value": repo_name,     "type": "PLAINTEXT"},
        {"name": "SOURCE_BRANCH", "value": source_branch, "type": "PLAINTEXT"},
        {"name": "COMMIT_ID",     "value": commit_id,     "type": "PLAINTEXT"},
        {"name": "TRIGGER_NAME",  "value": trigger_name,  "type": "PLAINTEXT"},
        {"name": "TRIGGER_REASON","value": "pr_build",    "type": "PLAINTEXT"},
        {"name": "SOURCE_COMMIT", "value": commit_id,     "type": "PLAINTEXT"},
    ]
    if pr_id:
        env_vars.append({"name": "PR_ID",              "value": pr_id,              "type": "PLAINTEXT"})
    if revision_id:
        env_vars.append({"name": "REVISION_ID",        "value": revision_id,        "type": "PLAINTEXT"})
    if destination_commit:
        env_vars.append({"name": "DESTINATION_COMMIT", "value": destination_commit, "type": "PLAINTEXT"})

    try:
        cb = _get_codebuild_client()
        response = cb.start_build(
            projectName=project,
            secondarySourcesVersionOverride=[
                {
                    "sourceIdentifier": source_identifier,
                    "sourceVersion":    f"refs/heads/{source_branch}",
                }
            ],
            environmentVariablesOverride=env_vars,
        )
        build_id = response["build"]["id"]
        logger.info(
            "Build started",
            artifact={"buildId": build_id, "project": project, "repo": repo_name, "branch": source_branch},
        )
        return {"status": "started", "buildId": build_id, "project": project, "repo": repo_name}
    except Exception:
        logger.exception("Failed to start CodeBuild", artifact={"project": project, "repo": repo_name})
        return {"status": "error", "reason": "start_build failed", "repo": repo_name}


def _start_release_build(repo_name: str, branch_name: str, commit_id: str) -> dict:
    """
    Triggers the release CodeBuild project for a newly created release branch.
    No PR context is injected — release builds are fire-and-forget from this Lambda.
    """
    project, source_identifier = _get_release_project_and_identifier(repo_name)

    if not project:
        logger.warning(f"No release CodeBuild project mapped for repo '{repo_name}' — skipping")
        return {"status": "skipped", "reason": "No release project mapped for repo", "repo": repo_name}

    env_vars = [
        {"name": "REPO_NAME",     "value": repo_name,   "type": "PLAINTEXT"},
        {"name": "SOURCE_BRANCH", "value": branch_name, "type": "PLAINTEXT"},
        {"name": "COMMIT_ID",     "value": commit_id,   "type": "PLAINTEXT"},
        {"name": "TRIGGER_REASON","value": "release_branch_created", "type": "PLAINTEXT"},
    ]

    try:
        cb = _get_codebuild_client()
        response = cb.start_build(
            projectName=project,
            secondarySourcesVersionOverride=[
                {
                    "sourceIdentifier": source_identifier,
                    "sourceVersion":    f"refs/heads/{branch_name}",
                }
            ],
            environmentVariablesOverride=env_vars,
        )
        build_id = response["build"]["id"]
        logger.info(
            "Release build started",
            artifact={"buildId": build_id, "project": project, "repo": repo_name, "branch": branch_name},
        )
        return {"status": "started", "buildId": build_id, "project": project, "repo": repo_name}
    except Exception:
        logger.exception("Failed to start release CodeBuild", artifact={"project": project, "repo": repo_name})
        return {"status": "error", "reason": "start_build failed", "repo": repo_name}


# ---------------------------------------------------------------------------
# PR approval
# ---------------------------------------------------------------------------

def _update_pr_approval(pr_id: str, revision_id: str, approve: bool) -> None:
    """Approves or revokes PR approval via CodeCommit."""
    approval_state = "APPROVE" if approve else "REVOKE"
    try:
        _get_codecommit_client().update_pull_request_approval_state(
            pullRequestId=pr_id,
            revisionId=revision_id,
            approvalState=approval_state,
        )
        logger.info(
            f"PR approval set to {approval_state}",
            artifact={"pr_id": pr_id, "revision_id": revision_id},
        )
    except Exception:
        logger.exception(
            "Failed to update PR approval state",
            artifact={"pr_id": pr_id, "revision_id": revision_id, "state": approval_state},
        )


# ---------------------------------------------------------------------------
# Idempotency helpers
# ---------------------------------------------------------------------------

def _check_already_reviewed(pr_id: str) -> bool:
    """
    Scans all PR comments for the reviewed-marker for this PR.
    Returns True if found (skip agent), False otherwise (run agent).
    PR review is once-per-PR — subsequent revisions do NOT trigger a re-review.
    On any API error, returns False — better to run agent twice than skip silently.
    """
    cc = _get_codecommit_client()
    kwargs = {"pullRequestId": pr_id}
    try:
        while True:
            response = cc.get_comments_for_pull_request(**kwargs)
            for thread in response.get("commentsForPullRequestData", []):
                for comment in thread.get("comments", []):
                    if _PR_AGENT_MARKER_PREFIX in comment.get("content", ""):
                        logger.info(
                            "PR already reviewed — skipping agent",
                            artifact={"pr_id": pr_id},
                        )
                        return True
            next_token = response.get("nextToken")
            if not next_token:
                break
            kwargs["nextToken"] = next_token
    except Exception:
        logger.exception(
            "Failed to check review status; treating as unreviewed",
            artifact={"pr_id": pr_id},
        )
    return False


def _build_codebuild_url(region: str, build_id: str) -> str:
    """Construct the CodeBuild console URL for a specific build."""
    return (
        f"https://{region}.console.aws.amazon.com/codesuite/codebuild"
        f"/projects/{build_id.split(':')[0]}/build/{build_id}"
    )


def _get_gate_result_s3_key(env_vars: list, build_id: str) -> Optional[str]:
    """
    Reconstruct the result.json S3 key from the build's injected env vars.
    gate_project is derived from REPO_NAME via REPO_S3_GATE_MAP; repo_name itself
    is used as the second path segment rather than a mapped identifier.
    Returns None if REPO_NAME is missing or unmapped.
    """
    repo_name = _get_build_env_var(env_vars, "REPO_NAME")
    if not repo_name or repo_name not in REPO_S3_GATE_MAP:
        logger.info(
            "Cannot derive S3 result key — REPO_NAME missing or not in REPO_S3_GATE_MAP",
            artifact={"repo_name": repo_name},
        )
        return None

    gate_project, _ = REPO_S3_GATE_MAP[repo_name]
    pr_id           = _get_build_env_var(env_vars, "PR_ID")
    build_id_safe   = build_id.split("/")[-1].replace(":", "_")

    if pr_id:
        run_id    = f"pr-{pr_id}_{build_id_safe}"
        gate_type = "pr"
    else:
        source_branch = _get_build_env_var(env_vars, "SOURCE_BRANCH") or "unknown"
        branch_safe   = source_branch.replace("/", "-")
        run_id        = f"release-{branch_safe}_{build_id_safe}"
        gate_type     = "release"

    return f"{gate_project}/{repo_name}/{gate_type}/{run_id}/result.json"


def _fetch_gate_result(env_vars: list, build_id: str) -> Optional[dict]:
    """
    Fetches and parses result.json from S3 for the given build.
    Returns the parsed dict, or None if unavailable (missing env vars, S3 error, parse error).
    """
    key = _get_gate_result_s3_key(env_vars, build_id)
    if not key:
        logger.info("Cannot derive S3 result key — GATE_PROJECT/GATE_REPO not set in build env")
        return None
    try:
        obj = _get_s3_client().get_object(Bucket=GATE_RESULT_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:
        logger.exception("Failed to fetch gate result from S3", artifact={"bucket": GATE_RESULT_BUCKET, "key": key})
        return None


# Maps failure_reason values to a plain-English explanation for developers.
_FAILURE_REASON_MESSAGES = {
    "lint": (
        "**Lint check failed.**\n"
        "One or more files have style or formatting violations. "
        "Run the linter locally (`ruff check .` / `eslint .` / `mvn checkstyle:check`) "
        "and fix the reported issues before pushing again."
    ),
    "test": (
        "**Test suite failed.**\n"
        "One or more tests are failing for the code changed in this PR. "
        "Run the tests locally and ensure they all pass before pushing again."
    ),
    "coverage": (
        "**Diff coverage below threshold.**\n"
        "The lines you changed are not sufficiently covered by tests. "
        "Add or update tests to cover the new/modified code paths."
    ),
    "security": (
        "**Security scan found high-severity findings.**\n"
        "Semgrep reported ERROR-severity issues in the changed code. "
        "Review the security findings in the build report and remediate before merging."
    ),
    "spotbugs": (
        "**SpotBugs found Priority-1 issues.**\n"
        "Static analysis detected high-priority bugs or potential vulnerabilities. "
        "Review the SpotBugs report in the build logs and fix the flagged code."
    ),
    "git_history": (
        "**Could not resolve base commit for PR diff.**\n"
        "The build could not determine the base commit to compare against. "
        "This is usually a shallow-clone issue — re-pushing your branch typically resolves it."
    ),
}


def _build_failure_comment(
    build_id: str,
    build_status: str,
    region: str,
    gate_result: Optional[dict],
) -> str:
    """
    Compose the PR comment body from gate result data when available,
    falling back to a generic message for STOPPED builds or missing S3 data.
    """
    build_url = _build_codebuild_url(region, build_id)

    if build_status == "STOPPED":
        return (
            "## ⚠️ Build stopped\n\n"
            "The quality-gate build was cancelled before it could finish. "
            "This is usually a transient issue — push a new commit or re-run the build to try again.\n\n"
            f"[View build logs]({build_url})"
        )

    if not gate_result:
        # S3 result unavailable — generic fallback for FAILED
        return (
            "## ❌ Quality gate failed\n\n"
            "The build failed but no detailed report is available yet. "
            "Check the build logs for more information.\n\n"
            f"[View build logs]({build_url})"
        )

    failure_reason = gate_result.get("failure_reason")
    gate_status    = gate_result.get("gate_status", "")

    # null failure_reason (or gate_status SUCCEEDED) means all quality gates
    # passed — the CodeBuild process itself crashed for an infrastructure reason.
    if not failure_reason or gate_status == "SUCCEEDED":
        return (
            "## ⚠️ Build process error\n\n"
            "All quality gates passed but the build process exited with a failure status. "
            "This is likely a transient infrastructure issue. "
            "Re-push your branch or ask an admin to re-run the build.\n\n"
            f"[View build logs]({build_url})"
        )

    failure_gate   = gate_result.get("failure_gate") or failure_reason
    detail_message = _FAILURE_REASON_MESSAGES.get(
        failure_reason,
        f"The build failed at gate **{failure_gate}**. Check the build logs for details.",
    )

    return (
        f"## ❌ Quality gate failed — {failure_gate}\n\n"
        f"{detail_message}\n\n"
        f"[View build logs]({build_url})"
    )


def _post_build_failure_comment(
    pr_id: str,
    repo_name: str,
    before_commit_id: str,
    after_commit_id: str,
    build_id: str,
    build_status: str,
    region: str,
    env_vars: list,
) -> None:
    """
    Posts a contextual failure comment on the PR, pulling structured gate
    results from S3 when available. Non-fatal — a failure here must not break
    the overall flow.
    """
    gate_result = _fetch_gate_result(env_vars, build_id)
    comment     = _build_failure_comment(build_id, build_status, region, gate_result)

    try:
        _get_codecommit_client().post_comment_for_pull_request(
            pullRequestId=pr_id,
            repositoryName=repo_name,
            beforeCommitId=before_commit_id,
            afterCommitId=after_commit_id,
            content=comment,
        )
        logger.info(
            "Posted build failure comment on PR",
            artifact={"pr_id": pr_id, "build_status": build_status,
                      "failure_reason": (gate_result or {}).get("failure_reason")},
        )
    except Exception:
        logger.exception(
            "Failed to post build failure comment (non-fatal)",
            artifact={"pr_id": pr_id, "build_id": build_id},
        )


def _mark_pr_reviewed(
    pr_id: str,
    repo_name: str,
    before_commit_id: str,
    after_commit_id: str,
) -> None:
    """
    Posts an invisible marker comment to the PR so all subsequent build
    completions skip the agent. Review is once-per-PR.
    Non-fatal — a failure here only risks a duplicate review on the next run.
    """
    marker = f"{_PR_AGENT_MARKER_PREFIX} {pr_id} -->"
    try:
        _get_codecommit_client().post_comment_for_pull_request(
            pullRequestId=pr_id,
            repositoryName=repo_name,
            beforeCommitId=before_commit_id,
            afterCommitId=after_commit_id,
            content=marker,
        )
        logger.info(
            "Marked PR as reviewed",
            artifact={"pr_id": pr_id},
        )
    except Exception:
        logger.exception(
            "Failed to post review marker (non-fatal)",
            artifact={"pr_id": pr_id},
        )


# ---------------------------------------------------------------------------
# PR agent helpers (unchanged from original)
# ---------------------------------------------------------------------------

def _should_ignore_pr(details: dict) -> bool:
    import re
    ignore_source_branches  = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
    ignore_target_branches  = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])
    ignore_authors          = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])

    source_branch      = details.get("source_branch", "")
    destination_branch = details.get("destination_branch", "")
    author             = details.get("author", "")

    for pattern in ignore_source_branches:
        if re.search(pattern, source_branch):
            logger.info(f"Ignoring PR: source branch '{source_branch}' matches ignore pattern '{pattern}'")
            return True
    for pattern in ignore_target_branches:
        if re.search(pattern, destination_branch):
            logger.info(f"Ignoring PR: destination branch '{destination_branch}' matches ignore pattern '{pattern}'")
            return True
    for ignored_author in ignore_authors:
        if ignored_author in author:
            logger.info(f"Ignoring PR: author '{author}' is in ignore list")
            return True
    return False


def _get_auto_commands() -> list[str]:
    configured = get_settings().get("CODECOMMIT.PR_COMMANDS", _DEFAULT_AUTO_COMMANDS)
    return [cmd.lstrip("/").split()[0] for cmd in configured if cmd.strip()]


async def _run_agent(pr_url: str) -> None:
    """
    Run all auto-commands against the given PR URL sequentially.
    Each command gets its own starlette-context scope with a fresh copy of
    global_settings so per-command overrides are fully isolated.
    """
    from starlette_context import request_cycle_context

    from pr_agent.agent.pr_agent import PRAgent

    commands = _get_auto_commands()
    for command in commands:
        try:
            with request_cycle_context({"settings": copy.deepcopy(global_settings), "git_provider": {}}):
                get_settings().set("CONFIG.GIT_PROVIDER", "codecommit")
                get_settings().set("CONFIG.IS_AUTO_COMMAND", True)
                logger.info(f"Running auto-command '{command}' on {pr_url}")
                agent = PRAgent()
                await agent.handle_request(pr_url, command)
                logger.info(f"Completed auto-command '{command}' on {pr_url}")
        except Exception:
            logger.exception(f"Auto-command '{command}' failed for {pr_url}")


# ---------------------------------------------------------------------------
# Event handler 1: CodeBuild Build State Change
# Triggered when a build SUCCEEDS / FAILS / STOPS.
# On SUCCEEDED → approve PR + run PR agent (once per revision).
# On FAILED/STOPPED → revoke PR approval.
# ---------------------------------------------------------------------------

def _handle_build_state_change(event: dict) -> dict:
    detail       = event.get("detail", {})
    build_status = detail.get("build-status", "")
    build_id     = detail.get("build-id", "")
    project_name = detail.get("project-name", "")

    logger.info(
        "Build state change",
        artifact={"build_id": build_id, "build_status": build_status, "project": project_name},
    )

    # Safety net — only process builds from our own projects
    allowed_projects = _get_allowed_projects()
    if project_name not in allowed_projects:
        logger.info(f"Skipping unrelated project: {project_name}")
        return {"statusCode": 200, "body": f"Project '{project_name}' not managed by this Lambda"}

    if build_status not in ("SUCCEEDED", "FAILED", "STOPPED"):
        return {"statusCode": 200, "body": f"Build status '{build_status}' ignored"}

    # Fetch full build details to read injected env vars
    try:
        builds = _get_codebuild_client().batch_get_builds(ids=[build_id]).get("builds", [])
    except Exception:
        logger.exception("Failed to fetch build details", artifact={"build_id": build_id})
        return {"statusCode": 200, "body": "Could not fetch build details"}

    if not builds:
        return {"statusCode": 200, "body": "Build not found"}

    env_vars          = builds[0].get("environment", {}).get("environmentVariables", [])
    pr_id             = _get_build_env_var(env_vars, "PR_ID")
    revision_id       = _get_build_env_var(env_vars, "REVISION_ID")
    repo_name         = _get_build_env_var(env_vars, "REPO_NAME")
    source_commit     = _get_build_env_var(env_vars, "SOURCE_COMMIT")
    destination_commit = _get_build_env_var(env_vars, "DESTINATION_COMMIT")
    region            = (
        event.get("region")
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION", "ap-south-1")
    )

    logger.info(
        "Build env",
        artifact={"pr_id": pr_id, "revision_id": revision_id, "repo": repo_name},
    )

    # Builds started before this change won't have PR_ID/REVISION_ID — skip safely
    if not pr_id or not revision_id:
        logger.info("Build missing PR_ID or REVISION_ID — skipping PR agent and approval")
        return {"statusCode": 200, "body": "No PR context in build env vars"}

    if build_status == "SUCCEEDED":
        _update_pr_approval(pr_id, revision_id, approve=True)

        # PR agent — only once per PR (not per revision)
        if _check_already_reviewed(pr_id):
            return {"statusCode": 200, "body": f"PR {pr_id} approved; agent already ran for this PR"}

        pr_url = _build_pr_url(region=region, repo_name=repo_name, pr_id=pr_id)
        logger.info(f"Running PR agent after lint success: {pr_url}")
        try:
            asyncio.run(_run_agent(pr_url))
        except Exception:
            logger.exception("PR agent failed after lint success", artifact={"pr_url": pr_url})
            # Do NOT mark as reviewed — allows retry on next SUCCEEDED build
            return {"statusCode": 200, "body": f"PR {pr_id} approved; agent failed"}

        _mark_pr_reviewed(
            pr_id=pr_id,
            repo_name=repo_name,
            before_commit_id=destination_commit or "",
            after_commit_id=source_commit or "",
        )
        return {"statusCode": 200, "body": f"PR {pr_id} approved and reviewed"}

    else:  # FAILED or STOPPED
        _update_pr_approval(pr_id, revision_id, approve=False)
        _post_build_failure_comment(
            pr_id=pr_id,
            repo_name=repo_name,
            before_commit_id=destination_commit or "",
            after_commit_id=source_commit or "",
            build_id=build_id,
            build_status=build_status,
            region=region,
            env_vars=env_vars,
        )
        return {"statusCode": 200, "body": f"PR {pr_id} approval revoked (build {build_status})"}


# ---------------------------------------------------------------------------
# Event handler 2: CodeCommit Pull Request State Change
# Triggered when PR is created or source branch is updated.
# → Starts CodeBuild linting job only. PR agent runs from build success.
# ---------------------------------------------------------------------------

def _handle_pr_event(event: dict) -> dict:
    logger.info("PR event received", artifact={"event": event})
    detail    = event.get("detail", {})
    pr_event  = detail.get("event", "")
    pr_status = detail.get("pullRequestStatus", "")

    if pr_status and pr_status != "Open":
        logger.info(f"PR is not Open (status={pr_status}) — skipping")
        return {"statusCode": 200, "body": f"PR not Open (status={pr_status})"}

    if pr_event not in _SUPPORTED_PR_EVENTS:
        logger.info(f"Ignoring PR event type: '{pr_event}'")
        return {"statusCode": 200, "body": f"PR event '{pr_event}' ignored"}

    pr_id         = str(detail.get("pullRequestId", ""))
    repo_name     = (detail.get("repositoryNames") or [""])[0]
    source_branch = (
        _extract_branch_name(detail.get("sourceReference", "")) or detail.get("sourceReference", "")
    )
    dest_branch = (
        _extract_branch_name(detail.get("destinationReference", "")) or detail.get("destinationReference", "")
    )
    source_commit = detail.get("sourceCommit", "") or detail.get("afterCommitId", "")

    logger.info(
        "PR event",
        artifact={"pr_id": pr_id, "pr_event": pr_event, "repo": repo_name,
                  "src": source_branch, "dst": dest_branch},
    )

    if not repo_name or not source_branch:
        return {"statusCode": 200, "body": "Missing repo or branch — skipping"}

    allowed_repos = _get_allowed_repos()
    if allowed_repos and repo_name not in allowed_repos:
        logger.info(f"Repo '{repo_name}' not in ALLOWED_REPOS")
        return {"statusCode": 200, "body": "Repo not in ALLOWED_REPOS"}

    target_branches = _get_target_base_branches()
    if dest_branch not in target_branches:
        logger.info(f"Destination '{dest_branch}' not in TARGET_BASE_BRANCHES")
        return {"statusCode": 200, "body": f"Destination branch '{dest_branch}' not targeted"}

    # Fetch revisionId and destinationCommit so the build can carry them forward
    revision_id       = ""
    destination_commit = ""
    try:
        pr_data           = _get_codecommit_client().get_pull_request(pullRequestId=pr_id)
        revision_id       = pr_data["pullRequest"].get("revisionId", "")
        targets           = pr_data["pullRequest"].get("pullRequestTargets", [{}])
        destination_commit = targets[0].get("destinationCommit", "") if targets else ""
        if not source_commit:
            source_commit = targets[0].get("sourceCommit", "") if targets else ""
    except Exception:
        logger.exception("Failed to fetch PR details", artifact={"pr_id": pr_id})
        return {"statusCode": 500, "body": "Failed to retrieve PR details"}

    trigger_name = f"pr-{pr_id}"
    result = _start_build(
        repo_name=repo_name,
        source_branch=source_branch,
        commit_id=source_commit,
        trigger_name=trigger_name,
        pr_id=pr_id,
        revision_id=revision_id,
        destination_commit=destination_commit,
    )
    return {"statusCode": 200, "body": f"Build triggered for PR {pr_id}", "detail": result}


# ---------------------------------------------------------------------------
# Event handler 3: CodeCommit Repository State Change (branch push)
# Triggered on branch push.
# → Starts build only if an open PR already exists for the branch.
# ---------------------------------------------------------------------------

def _handle_push_event(event: dict) -> dict:
    detail         = event.get("detail", {})
    event_type     = detail.get("event", "")
    reference_type = detail.get("referenceType", "")
    repo_name      = detail.get("repositoryName", "")
    source_branch  = detail.get("referenceName", "") or _extract_branch_name(detail.get("referenceFullName", ""))
    commit_id      = detail.get("commitId", "")

    logger.info(
        "Push event",
        artifact={"repo": repo_name, "branch": source_branch, "event": event_type},
    )

    if reference_type and reference_type != "branch":
        return {"statusCode": 200, "body": f"Not a branch ref: {reference_type}"}

    if event_type not in ("referenceUpdated", "referenceCreated"):
        return {"statusCode": 200, "body": f"Unhandled push event type: {event_type}"}

    if not repo_name or not source_branch:
        return {"statusCode": 200, "body": "Missing repo or branch"}

    allowed_repos = _get_allowed_repos()
    if allowed_repos and repo_name not in allowed_repos:
        return {"statusCode": 200, "body": "Repo not in ALLOWED_REPOS"}

    # Release branch created — trigger release project and skip PR agent entirely
    if event_type == "referenceCreated" and source_branch.startswith(RELEASE_BRANCH_PREFIX):
        logger.info(
            "Release branch created — triggering release build",
            artifact={"repo": repo_name, "branch": source_branch},
        )
        result = _start_release_build(repo_name, source_branch, commit_id)
        return {"statusCode": 200, "body": f"Release build triggered for {source_branch}", "detail": result}

    target_branches = _get_target_base_branches()
    if not _has_open_pr_to_target(repo_name, source_branch, target_branches):
        logger.info(f"No open PR for '{repo_name}/{source_branch}' — skipping build")
        return {"statusCode": 200, "body": "No open PR for this branch"}

    open_pr = _get_open_pr(repo_name, source_branch)
    if not open_pr:
        return {"statusCode": 200, "body": "No open PR found"}

    pr_id             = open_pr["pullRequestId"]
    revision_id       = open_pr.get("revisionId", "")
    destination_commit = open_pr.get("destinationCommit", "")

    result = _start_build(
        repo_name=repo_name,
        source_branch=source_branch,
        commit_id=commit_id,
        trigger_name=f"push-{source_branch}",
        pr_id=pr_id,
        revision_id=revision_id,
        destination_commit=destination_commit,
    )
    return {"statusCode": 200, "body": f"Build triggered for push to {source_branch}", "detail": result}


# ---------------------------------------------------------------------------
# Event handler 4: Legacy CodeCommit SNS trigger
# Fallback for native CodeCommit → SNS → Lambda triggers.
# Starts linting builds; PR agent NOT triggered from this path
# (no revisionId available without additional API calls).
# ---------------------------------------------------------------------------

def _repo_from_arn(arn: str) -> Optional[str]:
    if not arn:
        return None
    parts = arn.split(":")
    return parts[-1] if len(parts) >= 6 else None


def _handle_codecommit_trigger(event: dict) -> dict:
    records = event.get("Records", [])
    results = []
    allowed_repos = _get_allowed_repos()

    for record in records:
        if record.get("eventSource") != "aws:codecommit":
            continue

        repo_name    = _repo_from_arn(record.get("eventSourceARN", ""))
        trigger_name = record.get("eventTriggerName", "codecommit")
        refs         = record.get("codecommit", {}).get("references", [])

        if not repo_name:
            results.append({"status": "skipped", "reason": "Could not parse repo from ARN"})
            continue

        if allowed_repos and repo_name not in allowed_repos:
            results.append({"status": "skipped", "reason": "Repo not in ALLOWED_REPOS", "repo": repo_name})
            continue

        for ref_item in refs:
            source_branch = _extract_branch_name(ref_item.get("ref"))
            commit_id     = ref_item.get("commit", "")
            created       = ref_item.get("created", False)

            if not source_branch:
                continue
            if created and not commit_id:
                results.append({"status": "skipped", "reason": "New branch, no commit", "branch": source_branch})
                continue

            target_branches = _get_target_base_branches()
            if not _has_open_pr_to_target(repo_name, source_branch, target_branches):
                results.append({
                    "status": "skipped", "reason": "No open PR",
                    "repo": repo_name, "branch": source_branch,
                })
                continue

            results.append(_start_build(repo_name, source_branch, commit_id, trigger_name))

    return {"statusCode": 200, "body": "Legacy trigger processed", "results": results}


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, _context) -> dict:
    """
    AWS Lambda entry point — routes to the appropriate handler based on event type.

    Returns
    -------
    dict
        Always returns a dict with at least a statusCode key.
    """
    logger.info("Lambda invoked", artifact={"event_keys": list(event.keys())})

    # Legacy CodeCommit SNS trigger (no detail-type key)
    if "Records" in event:
        return _handle_codecommit_trigger(event)

    detail_type = event.get("detail-type", "")

    if detail_type == "CodeBuild Build State Change":
        return _handle_build_state_change(event)

    if detail_type == "CodeCommit Pull Request State Change":
        return _handle_pr_event(event)

    if detail_type == "CodeCommit Repository State Change":
        return _handle_push_event(event)

    logger.info(f"Ignoring unrecognised event detail-type: '{detail_type}'")
    return {"statusCode": 200, "body": "Event ignored"}
