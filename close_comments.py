#!/usr/bin/env python3
"""CLI tool to close comments on old Facebook page posts via Meta Graph API."""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

from meta_client import MetaClient, GraphAPIError

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logger = logging.getLogger("close_comments")


def setup_logging(verbose=False, log_json=False):
    level = logging.DEBUG if verbose else logging.INFO
    if log_json:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler])


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_iso_datetime(s):
    """Parse an ISO-8601 datetime string (Graph API format) to UTC datetime."""
    # Graph API returns e.g. "2024-06-15T10:30:00+0000"
    s = s.replace("+0000", "+00:00").replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def build_time_filter(args):
    """Return a function (created_time_str) -> bool that returns True if post matches."""
    now = datetime.now(timezone.utc)

    if args.since or args.until:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.since else None
        until_dt = (datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    + timedelta(days=1) - timedelta(seconds=1)) if args.until else None

        def filter_fn(created_time_str):
            dt = parse_iso_datetime(created_time_str)
            if since_dt and dt < since_dt:
                return False
            if until_dt and dt > until_dt:
                return False
            return True

        return filter_fn

    # Default: older-than-days
    cutoff = now - timedelta(days=args.older_than_days)

    def filter_fn(created_time_str):
        dt = parse_iso_datetime(created_time_str)
        return dt < cutoff

    return filter_fn


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def process_page(client, page_id, page_name, page_token, time_filter, args):
    """Process a single page: scan posts, filter, and optionally disable comments.

    Returns a list of result dicts for the report.
    """
    results = []
    scanned = 0
    matched = 0
    updated = 0
    skipped = 0
    errors = 0

    logger.info("Processing page: %s (id=%s)", page_name, page_id)

    try:
        posts = client.get_posts(page_id, access_token=page_token, limit=args.limit)
    except GraphAPIError as exc:
        logger.error("Failed to list posts for page %s: %s", page_id, exc)
        return results

    for post in posts:
        scanned += 1
        post_id = post.get("id", "unknown")
        created_time = post.get("created_time")

        if not created_time:
            logger.debug("Post %s has no created_time, skipping", post_id)
            continue

        if not time_filter(created_time):
            continue

        matched += 1

        record = {
            "page_id": page_id,
            "page_name": page_name,
            "post_id": post_id,
            "created_time": created_time,
            "permalink": post.get("permalink_url", ""),
            "message_preview": (post.get("message") or "")[:100],
            "status": "pending",
        }

        if args.dry_run:
            record["status"] = "dry_run"
            logger.info("[DRY RUN] Would disable comments on post %s (created %s)", post_id, created_time)
            results.append(record)
            if args.max_updates and matched >= args.max_updates:
                logger.info("Reached --max-updates %d, stopping for page %s", args.max_updates, page_id)
                break
            continue

        # Attempt to disable comments
        try:
            resp = client.disable_comments(post_id, access_token=page_token)
            if resp.get("success") or resp.get("id"):
                record["status"] = "updated"
                updated += 1
                logger.info("Disabled comments on post %s (created %s)", post_id, created_time)
            else:
                record["status"] = "unexpected_response"
                record["response"] = json.dumps(resp)
                errors += 1
                logger.warning("Unexpected response for post %s: %s", post_id, resp)
        except GraphAPIError as exc:
            record["status"] = "error"
            record["error"] = str(exc)
            errors += 1
            logger.error("Failed to disable comments on post %s: %s", post_id, exc)

        results.append(record)

        if args.max_updates and updated >= args.max_updates:
            logger.info("Reached --max-updates %d, stopping for page %s", args.max_updates, page_id)
            break

    logger.info(
        "Page %s summary: scanned=%d, matched=%d, updated=%d, skipped=%d, errors=%d",
        page_id, scanned, matched, updated, skipped, errors,
    )
    return results


def resolve_pages(client, args):
    """Return a list of (page_id, page_name, page_access_token) tuples."""
    if args.scope == "all":
        logger.info("Scope=all: fetching pages from /me/accounts")
        pages = client.get_pages()
        if not pages:
            logger.error("No pages found for this token. Check permissions (pages_manage_posts, pages_read_user_content).")
            return []
        result = []
        for p in pages:
            result.append((p["id"], p.get("name", "unknown"), p.get("access_token", client.access_token)))
        logger.info("Found %d page(s)", len(result))
        return result

    # scope == "page"
    page_ids = []
    if args.page_ids:
        page_ids = [pid.strip() for pid in args.page_ids.split(",") if pid.strip()]
    elif args.page_id:
        page_ids = [args.page_id]
    else:
        # Try DEFAULT_PAGE_ID from env
        default_id = os.getenv("DEFAULT_PAGE_ID")
        if default_id:
            page_ids = [default_id]
        else:
            logger.error("No page ID specified. Use --page-id, --page-ids, or set DEFAULT_PAGE_ID in .env")
            return []

    return [(pid, pid, client.access_token) for pid in page_ids]


def write_report(results, output_path, fmt="json"):
    """Write the report to a file."""
    path = Path(output_path)

    if fmt == "csv" or path.suffix == ".csv":
        if not results:
            fieldnames = ["page_id", "page_name", "post_id", "created_time", "permalink", "message_preview", "status"]
        else:
            fieldnames = list(results[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info("Report written to %s (%d records)", path, len(results))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Close comments on old Facebook page posts via Meta Graph API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Dry-run: list posts older than 365 days for a page
  python close_comments.py --scope page --page-id 123456 --older-than-days 365 --dry-run

  # Close comments on all pages the token can manage
  python close_comments.py --scope all --older-than-days 365

  # Close comments for posts within a date range
  python close_comments.py --scope page --page-id 123456 --since 2024-01-01 --until 2024-12-31

  # Save report as CSV
  python close_comments.py --scope page --page-id 123456 --older-than-days 180 --output report.csv
""",
    )

    # Scope
    parser.add_argument(
        "--scope", choices=["all", "page"], default="page",
        help="'all' = process all pages the token can manage; 'page' = process specific page(s) (default: page)",
    )
    parser.add_argument("--page-id", help="Facebook page ID (when --scope page)")
    parser.add_argument("--page-ids", help="Comma-separated list of page IDs (when --scope page)")

    # Time filters
    parser.add_argument(
        "--older-than-days", type=int, default=365,
        help="Close comments on posts older than N days (default: 365)",
    )
    parser.add_argument("--since", help="Start date filter YYYY-MM-DD (overrides --older-than-days)")
    parser.add_argument("--until", help="End date filter YYYY-MM-DD (overrides --older-than-days)")

    # Behaviour
    parser.add_argument("--dry-run", action="store_true", help="Only list posts that would be updated; make no changes")
    parser.add_argument("--limit", type=int, help="Max posts to read per page (pagination limit)")
    parser.add_argument("--max-updates", type=int, help="Max posts to actually update per page")
    parser.add_argument("--sleep-ms", type=int, default=200, help="Pause between API requests in ms (default: 200)")

    # Output
    parser.add_argument("--output", help="Path to write report file (JSON or CSV based on extension)")
    parser.add_argument("--log-json", action="store_true", help="Output log lines as JSON")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Load .env
    load_dotenv()

    setup_logging(verbose=args.verbose, log_json=args.log_json)

    # Resolve access token
    access_token = os.getenv("PAGE_ACCESS_TOKEN") or os.getenv("USER_ACCESS_TOKEN")
    if not access_token:
        logger.error("No access token found. Set PAGE_ACCESS_TOKEN or USER_ACCESS_TOKEN in .env")
        sys.exit(1)

    graph_version = os.getenv("META_GRAPH_VERSION", "v22.0")

    client = MetaClient(
        access_token=access_token,
        graph_version=graph_version,
        sleep_ms=args.sleep_ms,
    )

    # Resolve pages
    pages = resolve_pages(client, args)
    if not pages:
        sys.exit(1)

    # Build time filter
    time_filter = build_time_filter(args)

    # Process each page
    all_results = []
    for page_id, page_name, page_token in pages:
        results = process_page(client, page_id, page_name, page_token, time_filter, args)
        all_results.extend(results)

    # Summary
    total_matched = len(all_results)
    total_updated = sum(1 for r in all_results if r["status"] == "updated")
    total_errors = sum(1 for r in all_results if r["status"] == "error")
    total_dry = sum(1 for r in all_results if r["status"] == "dry_run")

    logger.info("=== Final Summary ===")
    logger.info("Total posts matched: %d", total_matched)
    if args.dry_run:
        logger.info("Dry-run posts: %d", total_dry)
    else:
        logger.info("Posts updated: %d", total_updated)
        logger.info("Errors: %d", total_errors)

    # Write report
    if args.output:
        write_report(all_results, args.output)
    elif all_results:
        # Default: write to timestamped JSON
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = f"report_{ts}.json"
        write_report(all_results, default_path)

    if total_errors > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
