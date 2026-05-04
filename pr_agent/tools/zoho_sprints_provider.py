"""
Zoho Sprints ticket provider — fetches sprint item details for PR review context.

This is a ticket provider (not a git provider). It works with any git provider
(CodeCommit, GitHub, GitLab, etc.) by extracting a Zoho Sprints item number from the
PR title and fetching the item's details via the Zoho Sprints REST API.

PR title formats (all extract the digit sequence as the item number):
  [I2131] Fix login bug
  I2131: Fix login bug
  ZS-2131 Fix login bug
  #2131 Fix login bug
  #I2131 Fix login bug

Configuration (configuration.toml):
  [zoho_sprints]
  enabled = true
  domain = "zoho.in"
  team_id = "YOUR_TEAM_ID"
  repo_project_map = {my-repo = {project_id = "49899000000053149"}}
  # sprint_id is optional (legacy, kept for backward compatibility)

Secrets (.secrets.toml):
  [zoho_sprints]
  client_id = "..."
  client_secret = "..."
  refresh_token = "..."
"""

import fnmatch
import re
import time

import requests

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

logger = get_logger()

# Match the first unbroken digit sequence anywhere in the PR title.
# Handles: [I2131] Fix, I2131: Fix, ZS-2131 Fix, #2131 Fix, #I2131 Fix
_ITEM_NUMBER_PATTERN = re.compile(r"\d+")

_ZOHO_SPRINTS_API_BASE = "https://sprintsapi.{domain}/zsapi"
_ZOHO_ACCOUNTS_TOKEN_URL = "https://accounts.{domain}/oauth/v2/token"


def extract_zoho_item_number(pr_title: str) -> str | None:
    """
    Return the first sequence of digits found in the PR title, or None.

    Examples:
      "[I2131] Fix login bug"  -> "2131"
      "I2131: Fix login bug"   -> "2131"
      "ZS-2131 Fix login bug"  -> "2131"
      "#2131 Fix login bug"    -> "2131"
      "#I2131 Fix login bug"   -> "2131"
    """
    if not pr_title:
        return None
    m = _ITEM_NUMBER_PATTERN.search(pr_title.strip())
    return m.group(0) if m else None


class ZohoSprintsProvider:
    """Fetches Zoho Sprints item details using the REST API."""

    def __init__(self):
        settings = get_settings()

        # Config
        self.domain = settings.get("ZOHO_SPRINTS.DOMAIN", "zoho.in")
        self.team_id = settings.get("ZOHO_SPRINTS.TEAM_ID", "")
        self.repo_project_map = settings.get("ZOHO_SPRINTS.REPO_PROJECT_MAP", {})

        # Secrets
        self.client_id = settings.get("ZOHO_SPRINTS.CLIENT_ID", "")
        self.client_secret = settings.get("ZOHO_SPRINTS.CLIENT_SECRET", "")
        self.refresh_token = settings.get("ZOHO_SPRINTS.REFRESH_TOKEN", "")

        # Token cache
        self._access_token: str | None = None
        self._token_expiry: float = 0

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        url = _ZOHO_ACCOUNTS_TOKEN_URL.format(domain=self.domain)
        resp = requests.post(
            url,
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        # Zoho tokens typically expire in 3600s; subtract 60s safety margin.
        self._token_expiry = time.time() + data.get("expires_in", 3600) - 60
        logger.info("Zoho Sprints access token refreshed")
        return self._access_token

    def _invalidate_token(self) -> None:
        """Force token refresh on the next call to _get_access_token."""
        self._access_token = None
        self._token_expiry = 0

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._get_access_token()}"}

    def _get_with_retry(self, url: str, params: dict | None = None) -> dict:
        """
        GET url with automatic one-shot 401 retry (token refresh).

        If the first request returns HTTP 401 (token expired or revoked), the
        token cache is cleared and the request is retried exactly once with a
        freshly obtained token. Any other non-2xx status raises HTTPError.

        Returns parsed JSON dict on success.
        """
        resp = requests.get(url, params=params, headers=self._headers(), timeout=30)
        if resp.status_code == 401:
            logger.warning(
                "Zoho Sprints: 401 Unauthorized — invalidating cached token and retrying"
            )
            self._invalidate_token()
            resp = requests.get(url, params=params, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Project resolution
    # ------------------------------------------------------------------

    def _resolve_project_for_repo(self, repo_name: str) -> dict:
        """
        Look up repo_name in repo_project_map.

        Resolution order:
          1. Exact match  — "my-repo"
          2. Glob pattern — "abc-*", "service-*-v2", "*" (catch-all)

        Glob patterns are checked in the order they appear in the config.
        Returns dict with at least 'project_id'. 'sprint_id' is optional.
        """
        mapping = self.repo_project_map
        if hasattr(mapping, "to_dict"):
            mapping = mapping.to_dict()

        # 1. Exact match (case-insensitive)
        repo_name_lower = repo_name.lower()
        project_cfg = mapping.get(repo_name) or next(
            (cfg for key, cfg in mapping.items() if key.lower() == repo_name_lower),
            None,
        )

        # 2. Glob fallback — iterate keys in order, first match wins (case-insensitive)
        if not project_cfg:
            for pattern, cfg in mapping.items():
                if fnmatch.fnmatch(repo_name_lower, pattern.lower()):
                    project_cfg = cfg
                    logger.debug(
                        f"Zoho Sprints: repo '{repo_name}' matched pattern '{pattern}'"
                    )
                    break

        if not project_cfg:
            raise ValueError(
                f"Zoho Sprints: repo '{repo_name}' not found in repo_project_map "
                f"(no exact match or glob pattern matched). "
                f"Available keys: {list(mapping.keys())}"
            )

        if hasattr(project_cfg, "to_dict"):
            project_cfg = project_cfg.to_dict()

        if "project_id" not in project_cfg:
            raise ValueError(
                f"Zoho Sprints: repo_project_map entry for '{repo_name}' must contain "
                f"'project_id'. Got: {project_cfg}"
            )
        return project_cfg

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_item_response(
        self, data: dict, item_number: str, project_id: str
    ) -> dict | None:
        """
        Parse the multipledetails response format:

          {
            "item_prop": {"itemName": 1, "description": 3, "itemNo": 17, ...},
            "itemJObj":  {"INTERNAL_ID": [val0, val1, val2, ...]}
          }

        item_prop maps field names to integer indices into the itemJObj value array.
        Returns a standard ticket dict or None if parsing fails.
        """
        item_prop = data.get("item_prop")
        item_j_obj = data.get("itemJObj")

        if not item_prop or not item_j_obj:
            logger.warning(
                f"Zoho Sprints: unexpected response structure for item {item_number}: "
                f"missing item_prop or itemJObj. Keys present: {list(data.keys())}"
            )
            return None

        internal_id = next(iter(item_j_obj), None)
        if not internal_id:
            logger.warning(
                f"Zoho Sprints: no item found in itemJObj for item number {item_number}"
            )
            return None

        item_data = item_j_obj[internal_id]

        def _get_field(field_name: str) -> str:
            """Safe index lookup — returns '' on missing field or index error."""
            idx = item_prop.get(field_name)
            if idx is None:
                return ""
            try:
                val = item_data[idx]
                return str(val) if val is not None else ""
            except (IndexError, TypeError):
                return ""

        item_name = _get_field("itemName")
        description = _get_field("description")
        item_no_from_response = _get_field("itemNo") or item_number

        return {
            "ticket_id": f"I{item_no_from_response}",
            "ticket_url": self._build_ticket_url(project_id, item_number),
            "title": item_name,
            "body": description[:10000] if description else "",
            "labels": "",
        }

    # ------------------------------------------------------------------
    # URL builder
    # ------------------------------------------------------------------

    def _build_ticket_url(self, project_id: str, item_number: str) -> str:
        """
        Build a browser-navigable URL to the Zoho Sprints item.
        Uses project-level item view — sprint_id is not required.
        """
        return (
            f"https://sprints.{self.domain}/app/#{self.team_id}"
            f"/projectview/{project_id}/itemdetails/{item_number}"
        )

    # ------------------------------------------------------------------
    # Fetch item
    # ------------------------------------------------------------------

    def fetch_item_by_number(self, repo_name: str, item_number: str) -> dict | None:
        """
        Fetch a Zoho Sprints item by its human-readable number (digits only)
        using the project-level multipledetails endpoint.

        Does NOT require sprint_id. Returns a standard ticket dict or None.

        Args:
            repo_name:   Repository name as configured in repo_project_map.
            item_number: Digit-only item number, e.g. "2131" for item I2131.
        """
        project_cfg = self._resolve_project_for_repo(repo_name)
        project_id = project_cfg["project_id"]

        base = _ZOHO_SPRINTS_API_BASE.format(domain=self.domain)
        url = f"{base}/team/{self.team_id}/projects/{project_id}/item/"

        logger.info(
            f"Fetching Zoho Sprints item I{item_number} via multipledetails endpoint"
        )
        # Note: if Zoho rejects URL-encoded brackets (%5B/%5D), change to:
        #   data = self._get_with_retry(url + f"?action=multipledetails&itemnoarr=[{item_number}]")
        data = self._get_with_retry(
            url,
            params={"action": "multipledetails", "itemnoarr": f"[{item_number}]"},
        )

        return self._parse_item_response(data, item_number, project_id)


# ------------------------------------------------------------------
# Public entry point — called from ticket_pr_compliance_check.py
# ------------------------------------------------------------------

def _get_repo_name(git_provider) -> str:
    """Best-effort repo name extraction across different git providers."""
    for attr in ("repo_name", "repo", "repo_slug"):
        name = getattr(git_provider, attr, None)
        if name:
            return str(name).split("/")[-1]  # strip org prefix if present
    return ""


async def fetch_zoho_tickets(git_provider) -> list:
    """
    Extract a Zoho Sprints item number from the PR title and fetch its details.
    Returns a list of ticket dicts (0 or 1 items), or empty list on skip/failure.
    """
    import traceback

    settings = get_settings()

    if not settings.get("ZOHO_SPRINTS.ENABLED", False):
        logger.debug("Zoho Sprints: integration disabled (zoho_sprints.enabled = false), skipping")
        return []

    team_id = settings.get("ZOHO_SPRINTS.TEAM_ID", "")
    if not team_id:
        logger.warning("Zoho Sprints: team_id is not configured — set zoho_sprints.team_id")
        return []

    if not settings.get("ZOHO_SPRINTS.CLIENT_ID", ""):
        logger.warning("Zoho Sprints: client_id is not configured — set zoho_sprints.client_id in .secrets.toml")
        return []

    if not settings.get("ZOHO_SPRINTS.CLIENT_SECRET", ""):
        logger.warning(
            "Zoho Sprints: client_secret is not configured — set zoho_sprints.client_secret in .secrets.toml"
        )
        return []

    if not settings.get("ZOHO_SPRINTS.REFRESH_TOKEN", ""):
        logger.warning(
            "Zoho Sprints: refresh_token is not configured — set zoho_sprints.refresh_token in .secrets.toml"
        )
        return []

    # Get PR title from the git provider
    pr_title = ""
    if hasattr(git_provider, "pr") and hasattr(git_provider.pr, "title"):
        pr_title = git_provider.pr.title
    elif hasattr(git_provider, "get_pr_title"):
        pr_title = git_provider.get_pr_title()

    if not pr_title:
        logger.warning("Zoho Sprints: could not read PR title from git provider")
        return []

    logger.info(f"Zoho Sprints: scanning PR title for item number: '{pr_title}'")

    item_number = extract_zoho_item_number(pr_title)
    if not item_number:
        logger.info(
            f"Zoho Sprints: no item number found in PR title '{pr_title}' — "
            f"title must contain digits (e.g. '[I2131] Fix bug')"
        )
        return []

    logger.info(f"Zoho Sprints: extracted item number '{item_number}' from PR title")

    repo_name = _get_repo_name(git_provider)
    if not repo_name:
        logger.warning(
            "Zoho Sprints: could not determine repo name from git provider — "
            f"provider type: {type(git_provider).__name__}"
        )
        return []

    logger.info(f"Zoho Sprints: resolved repo name '{repo_name}'")

    try:
        provider = ZohoSprintsProvider()
        ticket = provider.fetch_item_by_number(repo_name, item_number)
        if ticket:
            logger.info(
                "Zoho Sprints: ticket fetched successfully",
                artifact={"ticket_id": ticket["ticket_id"], "title": ticket["title"], "url": ticket["ticket_url"]},
            )
            return [ticket]
        else:
            logger.warning(f"Zoho Sprints: fetch returned no data for item I{item_number}")
    except Exception as e:
        logger.error(
            f"Zoho Sprints: failed to fetch item I{item_number}: {e}",
            artifact={"traceback": traceback.format_exc()},
        )

    return []
