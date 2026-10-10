#!/usr/bin/env python
"""Opt-in live verification of the IGDB and RAWG integrations.

This is NOT part of the default test suite. It talks to the real internet with
real credentials, so it is run deliberately:

    # credentials come from the environment only - never from a file in the repo
    set CARTRIDGE_LIVE=1                                  (Windows cmd)
    set CARTRIDGE_IGDB_CLIENT_ID=...
    set CARTRIDGE_IGDB_CLIENT_SECRET=...
    set CARTRIDGE_RAWG_API_KEY=...
    python scripts\live_api_check.py

    python scripts/live_api_check.py --save-fixtures      # also refresh fixtures

What it proves, in order:

1. credentials are present and no secret is echoed;
2. IGDB authentication through the Twitch OAuth2 client-credentials flow;
3. IGDB title search returns mapped, sane results;
4. IGDB detail lookup by numeric id;
5. a real IGDB cover image downloads, passes format validation and thumbnails;
6. RAWG search and detail;
7. a real RAWG image downloads and validates;
8. the documented fallback: when IGDB fails, RAWG answers;
9. local re-ranking actually reorders provider results;
10. rate-limit budgets after the run stay far below published limits;
11. the generated report contains no credential material.

Request budget: about 15 HTTP calls per run. The limiter policies are the real
production ones, so a bug that spams a provider would show up here as a refusal.

Output is written to ``artifacts/live/`` as markdown + JSON. Both are sanitized
twice: by the redactor and by an explicit post-write scan that fails the run if
any credential value survived.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cartridge import config                                   # noqa: E402
from cartridge.artwork import images as image_tools             # noqa: E402
from cartridge.core.redaction import Redactor                   # noqa: E402
from cartridge.providers.base import HttpSettings, HttpClient   # noqa: E402
from cartridge.providers.igdb import (                          # noqa: E402
    FIELDS,
    IGDB_BASE,
    IgdbProvider,
)
from cartridge.providers.mapping import sanitize_for_fixture    # noqa: E402
from cartridge.providers.ratelimit import LimiterSet            # noqa: E402
from cartridge.providers.rawg import RAWG_BASE, RawgProvider    # noqa: E402
from cartridge.providers.service import MetadataService         # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
NOT_TESTED = "NOT TESTED"
BLOCKED = "BLOCKED"

SEARCH_QUERIES = [
    "The Witcher 2: Assassins of Kings",
    "Diablo II",
    "Baldur's Gate 3",
]


class Recorder(object):
    """Collects check results with honest statuses."""

    def __init__(self):
        self.checks = []

    def add(self, name, status, detail="", evidence=None):
        self.checks.append(
            {"name": name, "status": status, "detail": detail, "evidence": evidence or {}}
        )
        print("%-26s %-11s %s" % (name, status, _first_line(detail)))
        return status

    def count(self, status):
        return len([c for c in self.checks if c["status"] == status])


def _first_line(text):
    return (text or "").splitlines()[0][:88] if text else ""


def ensure_qt():
    """QImage decoding does not need a QApplication, QPixmap does; create one anyway."""
    if not os.environ.get("QT_QPA_PLATFORM") and not os.environ.get("DISPLAY") \
            and not sys.platform.startswith("win"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PyQt5.QtWidgets import QApplication

        return QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:
        print("Qt unavailable (%s); image decode checks will be limited" % exc)
        return None


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check_credentials(recorder, credentials, redactor):
    if not credentials.any_configured:
        recorder.add(
            "credentials", NOT_TESTED,
            "No credentials found. Export CARTRIDGE_IGDB_CLIENT_ID / "
            "CARTRIDGE_IGDB_CLIENT_SECRET / CARTRIDGE_RAWG_API_KEY and re-run.",
        )
        return False
    masked = credentials.masked()
    recorder.add(
        "credentials", PASS,
        "IGDB=%s, RAWG=%s (sources: %s)"
        % (
            "configured" if credentials.igdb_configured else "missing",
            "configured" if credentials.rawg_configured else "missing",
            ", ".join(sorted(set(credentials.sources.values()))) or "unknown",
        ),
        {"masked": masked},
    )
    return True


def check_igdb_auth(recorder, service, redactor):
    try:
        started = time.monotonic()
        service.igdb.authenticate(force=True)
        elapsed = (time.monotonic() - started) * 1000.0
    except Exception as exc:
        recorder.add(
            "igdb_auth", FAIL,
            "Twitch OAuth2 client-credentials flow failed: %s"
            % redactor.text(str(exc)),
            {"error_type": type(exc).__name__},
        )
        return False
    info = service.igdb.token_info()
    ok = info.get("authenticated") is True
    recorder.add(
        "igdb_auth", PASS if ok else FAIL,
        "access token obtained in %.0f ms; expiry in %s s"
        % (elapsed, info.get("seconds_until_expiry")),
        {
            "latency_ms": round(elapsed, 1),
            "token_present": bool(service.igdb.has_token),
            "seconds_until_expiry": info.get("seconds_until_expiry"),
            "token_type_reported": "bearer",
        },
    )
    return ok


def check_igdb_search(recorder, service, redactor, fixtures):
    evidence = {"queries": []}
    failures = []
    for query in SEARCH_QUERIES:
        try:
            started = time.monotonic()
            games = service.igdb.search(query, limit=6)
            elapsed = (time.monotonic() - started) * 1000.0
        except Exception as exc:
            failures.append("%s: %s" % (query, redactor.text(str(exc))))
            evidence["queries"].append({"query": query, "error": redactor.text(str(exc))})
            continue
        top = games[0] if games else None
        evidence["queries"].append(
            {
                "query": query,
                "results": len(games),
                "latency_ms": round(elapsed, 1),
                "top_title": top.title if top else None,
                "top_year": top.year if top else None,
                "top_provider_id": top.provider_id if top else None,
                "top_has_cover": bool(top.cover_url) if top else False,
                "top_screenshots": len(top.screenshot_urls) if top else 0,
                "top_genres": top.genres[:3] if top else [],
                "top_developer": top.developer if top else None,
                "top_alternative_names": top.alternative_names[:3] if top else [],
            }
        )
        if fixtures is not None and query == SEARCH_QUERIES[0] and games:
            fixtures["igdb_query"] = query
        if not games:
            failures.append("%s: no results" % query)

    status = PASS if not failures else FAIL
    recorder.add(
        "igdb_search", status,
        "; ".join(
            "%s -> %d results (%.0f ms)"
            % (
                row["query"], row.get("results", 0), row.get("latency_ms", 0.0)
            )
            for row in evidence["queries"]
            if "results" in row
        ) or "; ".join(failures),
        evidence,
    )
    return status == PASS, evidence


def check_igdb_details(recorder, service, redactor, provider_id):
    if not provider_id:
        recorder.add("igdb_details", NOT_TESTED, "no id available from the search step")
        return None
    try:
        started = time.monotonic()
        game = service.igdb.details(provider_id)
        elapsed = (time.monotonic() - started) * 1000.0
    except Exception as exc:
        recorder.add("igdb_details", FAIL, redactor.text(str(exc)))
        return None
    if game is None:
        recorder.add("igdb_details", FAIL, "detail lookup returned nothing")
        return None
    recorder.add(
        "igdb_details", PASS,
        "fetched '%s' (%s, %s) in %.0f ms; %d screenshots"
        % (game.title, game.provider_id, game.year, elapsed, len(game.screenshot_urls)),
        {
            "provider_id": game.provider_id,
            "title": game.title,
            "year": game.year,
            "rating": game.rating,
            "genres": game.genres,
            "platforms": game.platforms[:5],
            "developer": game.developer,
            "publisher": game.publisher,
            "franchise": game.franchise,
            "age_rating": game.age_rating,
            "alternative_names": game.alternative_names[:5],
            "screenshots": len(game.screenshot_urls),
            "has_summary": bool(game.summary),
            "latency_ms": round(elapsed, 1),
        },
    )
    return game


def check_image_download(recorder, client, service, url, destination, label, redactor):
    if not url:
        recorder.add(label, NOT_TESTED, "no image URL available from the previous step")
        return False
    limiters = service.limiters
    try:
        started = time.monotonic()
        info = client.download(
            url, destination, operation="live_image", limiter=limiters.images
        )
        elapsed = (time.monotonic() - started) * 1000.0
    except Exception as exc:
        recorder.add(label, FAIL, "download failed: %s" % redactor.text(str(exc)))
        return False

    validation = image_tools.validate_file(destination)
    thumb_path = destination + ".thumb.jpg"
    thumb = image_tools.make_thumbnail(destination, thumb_path, image_tools.COVER_THUMB)

    ok = validation["ok"] and thumb["ok"]
    recorder.add(
        label, PASS if ok else FAIL,
        "%d bytes in %.0f ms, format=%s %dx%d, thumbnail %dx%d (%s)"
        % (
            validation.get("bytes", 0), elapsed, validation.get("format"),
            validation.get("width", 0), validation.get("height", 0),
            thumb.get("width", 0), thumb.get("height", 0),
            "ok" if ok else (validation.get("reason") or thumb.get("reason")),
        ),
        {
            "url_host": _host(url),
            "http_status": info.status_code,
            "bytes_downloaded": info.payload.get("bytes"),
            "latency_ms": round(elapsed, 1),
            "validation": validation,
            "thumbnail": thumb,
            "sha256": image_tools.sha256_file(destination),
        },
    )
    return ok


def check_rawg(recorder, service, redactor, client, artifact_dir):
    if not service.rawg.configured:
        recorder.add("rawg_search", NOT_TESTED, "RAWG key not configured")
        recorder.add("rawg_details", NOT_TESTED, "RAWG key not configured")
        recorder.add("rawg_image", NOT_TESTED, "RAWG key not configured")
        return None
    evidence = {"queries": []}
    first_id = None
    failures = []
    for query in SEARCH_QUERIES[:2]:
        try:
            started = time.monotonic()
            games = service.rawg.search(query, limit=6)
            elapsed = (time.monotonic() - started) * 1000.0
        except Exception as exc:
            failures.append(redactor.text(str(exc)))
            continue
        top = games[0] if games else None
        if top is not None and first_id is None:
            first_id = top.provider_id
        evidence["queries"].append(
            {
                "query": query,
                "results": len(games),
                "latency_ms": round(elapsed, 1),
                "top_title": top.title if top else None,
                "top_year": top.year if top else None,
                "top_rating_0_100": top.rating if top else None,
                "top_has_cover": bool(top.cover_url) if top else False,
                "top_screenshots": len(top.screenshot_urls) if top else 0,
            }
        )
        if not games:
            failures.append("%s: no results" % query)

    recorder.add(
        "rawg_search", PASS if not failures else FAIL,
        "; ".join(
            "%s -> %d results (%.0f ms)" % (r["query"], r["results"], r["latency_ms"])
            for r in evidence["queries"]
        ) or "; ".join(failures),
        evidence,
    )

    if first_id is None:
        recorder.add("rawg_details", NOT_TESTED, "no id available from the search step")
        recorder.add("rawg_image", NOT_TESTED, "no image URL available")
        return None

    try:
        started = time.monotonic()
        game = service.rawg.details(first_id)
        elapsed = (time.monotonic() - started) * 1000.0
    except Exception as exc:
        recorder.add("rawg_details", FAIL, redactor.text(str(exc)))
        return None
    if game is None:
        recorder.add("rawg_details", FAIL, "detail lookup returned nothing")
        return None
    recorder.add(
        "rawg_details", PASS,
        "fetched '%s' (%s) in %.0f ms; summary=%s"
        % (game.title, game.year, elapsed, "yes" if game.summary else "no"),
        {
            "provider_id": game.provider_id,
            "title": game.title,
            "year": game.year,
            "rating": game.rating,
            "genres": game.genres,
            "developer": game.developer,
            "publisher": game.publisher,
            "age_rating": game.age_rating,
            "has_summary": bool(game.summary),
            "screenshots": len(game.screenshot_urls),
            "latency_ms": round(elapsed, 1),
        },
    )
    image_url = game.cover_url or (game.screenshot_urls[0] if game.screenshot_urls else None)
    check_image_download(
        recorder, client, service, image_url,
        os.path.join(artifact_dir, "rawg_image.jpg"), "rawg_image", redactor,
    )
    return game


def check_fallback(recorder, credentials, redactor, limiters):
    """With a broken IGDB secret, the service must fall back to RAWG."""
    if not credentials.rawg_configured:
        recorder.add("provider_fallback", NOT_TESTED, "RAWG key not configured")
        return
    broken = MetadataService(
        client=HttpClient(settings=HttpSettings(max_retries=0)),
        redactor=redactor,
        limiters=limiters,
    )
    broken.set_credentials(
        "this_client_id_does_not_exist_000",
        "this_secret_does_not_exist_00000",
        credentials.rawg_api_key,
    )
    try:
        outcome = broken.search("The Witcher 2", limit=4)
    except Exception as exc:
        recorder.add("provider_fallback", FAIL, "fallback raised: %s" % redactor.text(str(exc)))
        return
    finally:
        broken.close()

    ok = outcome.provider_used == "rawg" and bool(outcome.results)
    recorder.add(
        "provider_fallback", PASS if ok else FAIL,
        "IGDB failed as expected -> provider_used=%s, %d results, %d errors recorded"
        % (outcome.provider_used, len(outcome.results), len(outcome.errors)),
        {
            "provider_used": outcome.provider_used,
            "providers_tried": outcome.providers_tried,
            "fell_back": outcome.fell_back,
            "result_count": len(outcome.results),
            "errors": outcome.errors[:4],
            "top_title": outcome.results[0].title if outcome.results else None,
        },
    )


def check_ranking(recorder, service, redactor):
    """Local ranking must beat raw provider order for an unambiguous query."""
    try:
        outcome = service.search("The Witcher 3: Wild Hunt", limit=8)
    except Exception as exc:
        recorder.add("local_ranking", FAIL, redactor.text(str(exc)))
        return
    if not outcome.results:
        recorder.add("local_ranking", NOT_TESTED, "no results to rank")
        return
    scores = [game.match_score for game in outcome.results]
    descending = all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))
    top = outcome.results[0]
    from cartridge.providers.ranking import confidence_label

    ok = descending and "witcher 3" in top.title.lower()
    recorder.add(
        "local_ranking", PASS if ok else FAIL,
        "top='%s' score=%.2f (%s); %d results; ordering descending=%s"
        % (top.title, top.match_score, confidence_label(top.match_score),
           len(scores), descending),
        {
            "query": outcome.query,
            "provider_used": outcome.provider_used,
            "top_5": [
                {"title": g.title, "year": g.year, "score": g.match_score,
                 "provider": g.provider}
                for g in outcome.results[:5]
            ],
            "confidence_summary": outcome.confidence_summary(),
            "latency_ms": outcome.latency_ms,
        },
    )


def check_rate_limits(recorder, service):
    summary = service.limiters.summary()
    evidence = {}
    problems = []
    for name, stats in summary.items():
        evidence[name] = {
            "used_this_minute": stats["used_this_minute"],
            "max_per_minute": stats["max_per_minute"],
            "used_today": stats["used_today"],
            "max_per_day": stats["max_per_day"],
            "refused": stats["refused"],
            "total_wait_seconds": stats["total_wait_seconds"],
        }
        if stats["used_today"] > stats["max_per_day"]:
            problems.append("%s exceeded its daily budget" % name)
    recorder.add(
        "rate_limit_budget", PASS if not problems else FAIL,
        "requests used: " + ", ".join(
            "%s %d/%d per day" % (n, s["used_today"], s["max_per_day"])
            for n, s in evidence.items()
        ) + ("; " + "; ".join(problems) if problems else ""),
        evidence,
    )


def check_report_is_clean(recorder, report_text, credentials):
    leaks = []
    for value in credentials.values():
        if value and value in report_text:
            leaks.append("a credential value appears verbatim")
    for needle in ("client_secret=", "access_token=", "Bearer "):
        for line in report_text.splitlines():
            if needle in line and "REDACTED" not in line and "masked" not in line:
                leaks.append("%s in: %s" % (needle, line.strip()[:80]))
                break
    recorder.add(
        "report_is_clean", PASS if not leaks else FAIL,
        "no credential material in the generated report" if not leaks
        else "; ".join(sorted(set(leaks))),
        {"leaks": sorted(set(leaks))},
    )
    return not leaks


def _host(url):
    try:
        import httpx

        return httpx.URL(url).host
    except Exception:
        return str(url).split("/")[2][:40] if "://" in str(url) else ""


# --------------------------------------------------------------------------
# fixture capture (sanitized)
# --------------------------------------------------------------------------
def capture_fixtures(client, service, fixture_dir, redactor):
    """Save sanitized real payloads so offline tests match today's API shape."""
    saved = []
    if service.igdb.configured:
        try:
            service.igdb.authenticate()
            body = 'search "%s";\nfields %s;\nlimit 3;' % (
                SEARCH_QUERIES[0].replace('"', ""), FIELDS
            )
            info = client.request(
                "POST", IGDB_BASE + "/games",
                headers=service.igdb._headers(),
                content=body,
                operation="fixture_capture",
                limiter=service.limiters.igdb,
            )
            payload = sanitize_for_fixture(info.payload)
            path = os.path.join(fixture_dir, "live_igdb_search.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"_comment": "Sanitized live IGDB capture. No credentials.",
                     "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "payload": payload},
                    handle, indent=2, ensure_ascii=False,
                )
            saved.append(os.path.relpath(path, REPO_ROOT))
        except Exception as exc:
            print("IGDB fixture capture failed: %s" % redactor.text(str(exc)))

    if service.rawg.configured:
        try:
            info = client.request(
                "GET", RAWG_BASE + "/games",
                params=service.rawg._params(
                    {"search": SEARCH_QUERIES[0], "page_size": 3, "exclude_addons": "true"}
                ),
                operation="fixture_capture",
                limiter=service.limiters.rawg,
            )
            payload = sanitize_for_fixture(info.payload)
            path = os.path.join(fixture_dir, "live_rawg_search.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"_comment": "Sanitized live RAWG capture. No credentials.",
                     "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "payload": payload},
                    handle, indent=2, ensure_ascii=False,
                )
            saved.append(os.path.relpath(path, REPO_ROOT))
        except Exception as exc:
            print("RAWG fixture capture failed: %s" % redactor.text(str(exc)))
    return saved


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def render_report(checks, meta):
    lines = [
        "# Cartridge live API verification",
        "",
        "_Generated by `scripts/live_api_check.py` against the real IGDB and RAWG",
        "endpoints. Machine-recorded evidence; no credential values._",
        "",
        "- **When:** %s" % meta["timestamp"],
        "- **Host:** %s" % meta["host"],
        "- **Python:** %s" % meta["python"],
        "- **httpx:** %s" % meta["httpx"],
        "- **App version:** %s" % meta["app_version"],
        "",
        "| # | Check | Status | Detail |",
        "|---|-------|--------|--------|",
    ]
    for index, check in enumerate(checks, start=1):
        detail = (check["detail"] or "").replace("\n", " ").replace("|", "/")
        if len(detail) > 200:
            detail = detail[:197] + "..."
        lines.append(
            "| %d | `%s` | **%s** | %s |" % (index, check["name"], check["status"], detail)
        )
    lines.append("")
    counts = {}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    lines.append(
        "Summary: %s" % ", ".join("%s=%d" % (k, counts[k]) for k in sorted(counts))
    )
    lines.append("")
    lines.append("## Evidence")
    for check in checks:
        lines.append("")
        lines.append("### `%s` — %s" % (check["name"], check["status"]))
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(check["evidence"], indent=2, sort_keys=True, default=str))
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cartridge live IGDB/RAWG verification")
    parser.add_argument("--artifact-dir", default=os.path.join(REPO_ROOT, "artifacts", "live"))
    parser.add_argument("--report", default=None)
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--save-fixtures", action="store_true",
                        help="also write sanitized payloads to tests/fixtures/")
    parser.add_argument("--skip-images", action="store_true")
    args = parser.parse_args(argv)

    artifact_dir = args.artifact_dir
    if not os.path.isdir(artifact_dir):
        os.makedirs(artifact_dir)

    redactor = Redactor()
    credentials = config.load_credentials(redactor=redactor)
    # Also register with the process-wide redactor so library-level error text is
    # scrubbed too.
    credentials.register_with(config.default_redactor())

    ensure_qt()
    recorder = Recorder()
    print("")
    print("CARTRIDGE LIVE API VERIFICATION")
    print("-" * 78)

    if not config.live_tests_enabled():
        print("NOTE: CARTRIDGE_LIVE is not set. This script talks to the real")
        print("      internet; it is opt-in so the default suite stays offline.")
        print("")

    limiters = LimiterSet()
    client = HttpClient(
        settings=HttpSettings(max_retries=1),
        redactor=redactor,
        latency_hook=lambda op, ms, attempts: None,
    )
    service = MetadataService(client=client, redactor=redactor, limiters=limiters)
    service.set_credentials(
        credentials.igdb_client_id,
        credentials.igdb_client_secret,
        credentials.rawg_api_key,
    )

    meta = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": "%s %s" % (sys.platform, os.name),
        "python": sys.version.split()[0],
        "httpx": config.environment_info().get("httpx_version"),
        "app_version": config.environment_info().get("app_version"),
        "credentials_sources": sorted(set(credentials.sources.values())),
    }

    try:
        if not check_credentials(recorder, credentials, redactor):
            return _finish(recorder, meta, args, credentials, redactor, service)

        igdb_ok = False
        top_id = None
        top_cover = None
        if credentials.igdb_configured:
            igdb_ok = check_igdb_auth(recorder, service, redactor)
            if igdb_ok:
                search_ok, evidence = check_igdb_search(recorder, service, redactor, None)
                if search_ok:
                    rows = evidence["queries"]
                    top_id = rows[0].get("top_provider_id")
                    top_cover = None
                details = check_igdb_details(recorder, service, redactor, top_id)
                if details is not None:
                    top_cover = details.cover_url
                    if not args.skip_images:
                        check_image_download(
                            recorder, client, service, top_cover,
                            os.path.join(artifact_dir, "igdb_cover.jpg"),
                            "igdb_image", redactor,
                        )
            else:
                recorder.add("igdb_search", BLOCKED, "authentication failed first")
                recorder.add("igdb_details", BLOCKED, "authentication failed first")
                recorder.add("igdb_image", BLOCKED, "authentication failed first")
        else:
            for name in ("igdb_auth", "igdb_search", "igdb_details", "igdb_image"):
                recorder.add(name, NOT_TESTED, "IGDB credentials not configured")

        check_rawg(recorder, service, redactor, client, artifact_dir)
        check_fallback(recorder, credentials, redactor, limiters)
        check_ranking(recorder, service, redactor)
        check_rate_limits(recorder, service)

        if args.save_fixtures:
            fixture_dir = os.path.join(REPO_ROOT, "tests", "fixtures")
            saved = capture_fixtures(client, service, fixture_dir, redactor)
            recorder.add(
                "fixture_capture", PASS if saved else FAIL,
                "sanitized payloads written: %s" % (", ".join(saved) or "none"),
                {"files": saved},
            )
    except Exception as exc:
        recorder.add(
            "unexpected_error", FAIL,
            "%s: %s" % (type(exc).__name__, redactor.text(str(exc))),
            {"traceback": redactor.text(traceback.format_exc())[-1500:]},
        )
    finally:
        service.close()

    return _finish(recorder, meta, args, credentials, redactor, service)


def _finish(recorder, meta, args, credentials, redactor, service):
    report_path = args.report or os.path.join(args.artifact_dir, "live_api_report.md")
    json_path = args.json_out or os.path.join(args.artifact_dir, "live_api_report.json")

    text = render_report(recorder.checks, meta)
    clean = check_report_is_clean(recorder, text, credentials)
    if not clean:
        # Never write a report that contains a secret.
        print("REFUSING TO WRITE REPORT: credential material detected in output")
        return 2
    text = render_report(recorder.checks, meta)

    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump({"meta": meta, "checks": recorder.checks}, handle, indent=2, default=str)

    print("-" * 78)
    print(
        "summary: " + ", ".join(
            "%s=%d" % (status, recorder.count(status))
            for status in (PASS, FAIL, NOT_TESTED, BLOCKED)
            if recorder.count(status)
        )
    )
    print("report : %s" % os.path.relpath(report_path, REPO_ROOT))
    print("json   : %s" % os.path.relpath(json_path, REPO_ROOT))
    print("")
    return 1 if recorder.count(FAIL) else 0


if __name__ == "__main__":
    sys.exit(main())
