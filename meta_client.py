"""HTTP wrapper for Meta Graph API with retry/backoff and rate-limit handling."""

import logging
import time
from urllib.parse import urlencode, urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

# Retryable HTTP status codes
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Graph API error codes that indicate rate limiting
RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613}


class GraphAPIError(Exception):
    """Structured error from the Graph API."""

    def __init__(self, message, error_type=None, code=None, subcode=None, fbtrace_id=None):
        super().__init__(message)
        self.error_type = error_type
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id

    def __str__(self):
        parts = [super().__str__()]
        if self.error_type:
            parts.append(f"type={self.error_type}")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.subcode is not None:
            parts.append(f"subcode={self.subcode}")
        if self.fbtrace_id:
            parts.append(f"fbtrace_id={self.fbtrace_id}")
        return " | ".join(parts)


class MetaClient:
    """Client for the Meta Graph API with automatic retry and pagination."""

    def __init__(self, access_token, graph_version="v22.0", sleep_ms=0, max_retries=5):
        self.access_token = access_token
        self.graph_version = graph_version
        self.sleep_ms = sleep_ms
        self.max_retries = max_retries
        self.base_url = f"https://graph.facebook.com/{graph_version}"
        self.session = requests.Session()

    def _throttle(self):
        """Sleep between requests if configured."""
        if self.sleep_ms > 0:
            time.sleep(self.sleep_ms / 1000.0)

    def _parse_graph_error(self, response):
        """Parse a Graph API error response into a GraphAPIError."""
        try:
            body = response.json()
        except ValueError:
            return GraphAPIError(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        err = body.get("error", {})
        return GraphAPIError(
            message=err.get("message", response.text[:500]),
            error_type=err.get("type"),
            code=err.get("code"),
            subcode=err.get("error_subcode"),
            fbtrace_id=err.get("fbtrace_id"),
        )

    def _request(self, method, url, **kwargs):
        """Execute an HTTP request with retry and exponential backoff."""
        last_exception = None

        for attempt in range(self.max_retries + 1):
            self._throttle()

            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_exception = exc
                if attempt < self.max_retries:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Network error on attempt %d/%d: %s — retrying in %ds",
                        attempt + 1, self.max_retries + 1, exc, wait,
                    )
                    time.sleep(wait)
                    continue
                raise

            if resp.status_code == 200:
                return resp.json()

            # Check for retryable status codes
            if resp.status_code in RETRYABLE_STATUS_CODES:
                api_err = self._parse_graph_error(resp)

                if attempt < self.max_retries:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Retryable error (HTTP %d) on attempt %d/%d: %s — retrying in %ds",
                        resp.status_code, attempt + 1, self.max_retries + 1, api_err, wait,
                    )
                    time.sleep(wait)
                    continue

                raise api_err

            # Check for rate-limit error codes in the body
            try:
                body = resp.json()
                error_code = body.get("error", {}).get("code")
                if error_code in RATE_LIMIT_ERROR_CODES and attempt < self.max_retries:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Rate limit (error code %d) on attempt %d/%d — retrying in %ds",
                        error_code, attempt + 1, self.max_retries + 1, wait,
                    )
                    time.sleep(wait)
                    continue
            except ValueError:
                pass

            # Non-retryable error
            raise self._parse_graph_error(resp)

        # Exhausted retries
        if last_exception:
            raise last_exception
        raise GraphAPIError("Exhausted retries without a successful response")

    def get(self, endpoint, params=None, access_token=None):
        """GET request to a Graph API endpoint."""
        params = dict(params or {})
        params["access_token"] = access_token or self.access_token
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._request("GET", url, params=params)

    def post(self, endpoint, data=None, access_token=None):
        """POST request to a Graph API endpoint."""
        data = dict(data or {})
        data["access_token"] = access_token or self.access_token
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._request("POST", url, data=data)

    def get_paginated(self, endpoint, params=None, access_token=None, max_items=None):
        """Yield items from a paginated GET endpoint.

        Follows paging.next links automatically. Stops when no more pages,
        or when max_items have been yielded.
        """
        params = dict(params or {})
        params["access_token"] = access_token or self.access_token
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        yielded = 0

        while url:
            self._throttle()
            result = self._request("GET", url, params=params)
            # After the first request, params are embedded in paging.next
            params = {}

            for item in result.get("data", []):
                yield item
                yielded += 1
                if max_items and yielded >= max_items:
                    return

            next_url = result.get("paging", {}).get("next")
            if next_url:
                url = next_url
            else:
                break

    # ---- High-level helpers ------------------------------------------------

    def get_pages(self):
        """Return list of pages the token can administer (for scope=all)."""
        pages = list(self.get_paginated(
            "me/accounts",
            params={"fields": "id,name,access_token", "limit": "100"},
        ))
        return pages

    def get_posts(self, page_id, access_token=None, limit=None):
        """Yield posts for a given page."""
        yield from self.get_paginated(
            f"{page_id}/posts",
            params={
                "fields": "id,created_time,permalink_url,message",
                "limit": "100",
            },
            access_token=access_token,
            max_items=limit,
        )

    def disable_comments(self, post_id, access_token=None):
        """Set is_comment_enabled=false on a post. Returns the API response."""
        return self.post(
            post_id,
            data={"is_comment_enabled": "false"},
            access_token=access_token,
        )
