#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper v4

Key fixes from v3:
- wait_until="commit" gave blank page (title='') — SPA not hydrated
  Fix: wait_until="domcontentloaded" + explicit wait for body text content
- DOM selector was too class-name specific (Stake uses hashed CSS modules)
  Fix: pure odds-density approach — scan ALL divs, find clusters of decimal numbers
- GQL interception retained: listener attached before goto()
- Game page drilling retained but now verifies page has content before scraping
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from apify import Actor
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

try:
    from playwright_stealth import Stealth
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-scraper")

DEFAULT_PROXY = {
    "server": "http://pr.oxylabs.io:7777",
    "username": "customer-sonus_TbxLY-cc-ca-city-edmonton",
    "password": "***",
}

STAKE_BASE = "https://stake.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

ESPORTS_GAME_SLUGS = [
    "dota-2",
    "counter-strike",
    "league-of-legends",
    "valorant",
    "mobile-legends",
    "starcraft-2",
    "overwatch",
    "rocket-league",
    "fifa",
    "king-of-glory",
]


def parse_decimal_odds(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        return v if 1.01 <= v <= 500 else None
    t = str(val).strip().replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", t)
    if m:
        v = float(m.group(1))
        return v if 1.01 <= v <= 500 else None
    return None


async def wait_for_content(page, timeout_s: int = 20) -> bool:
    """
    Wait until the page has meaningful text content — i.e. the SPA has hydrated.
    Returns True if content appeared, False if still blank after timeout.
    """
    for i in range(timeout_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            body_len = await page.evaluate("() => document.body ? document.body.innerText.length : 0")
            if body_len > 500 and title and title != "Just a moment...":
                logger.info(f"Content ready at {i+1}s | title={title!r} | body_chars={body_len}")
                return True
            if i % 5 == 0:
                logger.info(f"Waiting for content {i+1}s | title={title!r} | body_chars={body_len}")
        except Exception:
            pass
    return False


async def handle_cf_challenge(page, max_wait_s: int = 90) -> bool:
    """Wait for CF Turnstile. Returns True when real page content is present."""
    for i in range(max_wait_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            cur_url = page.url
        except Exception:
            continue
        if i % 15 == 0 or i < 5:
            logger.info(f"CF wait {i+1}s | title={title!r}")
        if (
            title
            and title != "Just a moment..."
            and "__cf_chl" not in cur_url
            and "challenge" not in cur_url.lower()
        ):
            logger.info(f"CF resolved at {i+1}s | title={title!r}")
            return True
    return False


def extract_records_from_graphql(body: dict, now: str) -> List[Dict]:
    records = []
    data = body.get("data") or {}

    def parse_fixture(fixture: dict, t_name: str, game_name: str) -> List[Dict]:
        recs = []
        if not isinstance(fixture, dict):
            return recs
        name = fixture.get("name", "")
        start_time = fixture.get("startTime", "")
        slug = fixture.get("slug", "")
        match_url = f"{STAKE_BASE}/sports/esports/{slug}" if slug else ""

        vs_parts = re.split(r"\s+(?:vs\.?|VS\.?|–|-)\s+", name, maxsplit=1)
        team1 = vs_parts[0].strip() if len(vs_parts) == 2 else ""
        team2 = vs_parts[1].strip() if len(vs_parts) == 2 else ""

        target_markets = {"match winner", "match result", "winner", "1x2", "moneyline"}
        for market in fixture.get("markets", []):
            if not isinstance(market, dict):
                continue
            mkt_name = (market.get("name") or "").lower()
            if mkt_name and mkt_name not in target_markets:
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            p1 = p2 = p_draw = None
            for o in outcomes:
                o_name = (o.get("name") or "").lower()
                odds = parse_decimal_odds(o.get("odds"))
                if not odds:
                    continue
                if team1 and team1.lower() in o_name:
                    p1 = odds
                elif team2 and team2.lower() in o_name:
                    p2 = odds
                elif any(x in o_name for x in ("draw", "tie")):
                    p_draw = odds
                elif o_name in ("home", "1") and p1 is None:
                    p1 = odds
                elif o_name in ("away", "2") and p2 is None:
                    p2 = odds
            if p1 or p2:
                recs.append({
                    "bookmaker": "stake", "game_raw": game_name,
                    "tournament_name": t_name, "team1": team1, "team2": team2,
                    "match_start_time": start_time, "match_url": match_url,
                    "price_team1": p1, "price_team2": p2, "price_draw": p_draw,
                    "scraped_at": now,
                })
        return recs

    t = data.get("sportTournament")
    if t:
        g_name = ""
        for fx in t.get("fixtureList", []):
            records.extend(parse_fixture(fx, t.get("name", ""), g_name))

    for key in ("fixtureList", "fixtures", "events"):
        if key in data and isinstance(data[key], list):
            for fx in data[key]:
                records.extend(parse_fixture(fx, "", ""))

    return records


def parse_card_lines(lines: list) -> Optional[Dict]:
    """
    Parse Stake card innerText lines into structured record.

    Observed card line structure:
      [0] game name: "League of Legends" / "CS2" / "FIFA"
      [1] status: "Live" or "1m 6s" or date
      [optional] map indicator: "5th Map" / "1st Map"
      [N] tournament name
      [N+1] team1
      [N+2] team2
      [N+3..] score digits if live: "2", "2", "0", "1"
      "Match Winner - Twoway" or "Match Winner - Threeway"
      then decimal odds
    """
    # Extract all decimal odds
    odds_values = []
    for line in lines:
        m = re.match(r"^(\d{1,3}\.\d{2,3})$", line.strip())
        if m:
            v = float(m.group(1))
            if 1.01 <= v <= 500:
                odds_values.append(v)

    if len(odds_values) < 2:
        return None

    # Find "Match Winner" anchor
    mw_idx = None
    for i, line in enumerate(lines):
        if "match winner" in line.lower() or "moneyline" in line.lower():
            mw_idx = i
            break

    team1, team2, tournament = "", "", ""

    if mw_idx and mw_idx >= 3:
        candidates = []
        i = mw_idx - 1
        while i >= 2 and len(candidates) < 4:
            line = lines[i].strip()
            if re.match(r"^\d{1,2}$", line):  # skip score digits
                i -= 1
                continue
            candidates.insert(0, (i, line))
            i -= 1

        if len(candidates) >= 2:
            team1 = candidates[-2][1]
            team2 = candidates[-1][1]
            t_idx = candidates[-2][0] - 1
            if t_idx >= 2:
                potential_t = lines[t_idx].strip()
                if not re.match(r"^\d+(st|nd|rd|th)\s+Map", potential_t, re.I):
                    tournament = potential_t
        elif len(candidates) == 1:
            team2 = candidates[0][1]
    else:
        # No Match Winner label found — try heuristic: 2 non-numeric lines before the odds block
        first_odds_line = None
        for i, line in enumerate(lines):
            if re.match(r"^\d{1,3}\.\d{2,3}$", line.strip()):
                first_odds_line = i
                break
        if first_odds_line and first_odds_line >= 2:
            non_num = [(i, l) for i, l in enumerate(lines[:first_odds_line])
                       if not re.match(r"^\d", l.strip()) and len(l.strip()) > 2]
            if len(non_num) >= 2:
                team1 = non_num[-2][1].strip()
                team2 = non_num[-1][1].strip()

    if not team1 or not team2:
        return None

    game_raw = lines[0].strip() if lines else ""
    status_line = lines[1].strip() if len(lines) > 1 else ""

    has_draw = any("threeway" in l.lower() or "draw" in l.lower() for l in lines)
    if len(odds_values) >= 3 and has_draw:
        p1, p_draw, p2 = odds_values[0], odds_values[1], odds_values[2]
    else:
        p1, p2, p_draw = odds_values[0], odds_values[1], None

    return {
        "game_raw": game_raw, "tournament_name": tournament,
        "team1": team1, "team2": team2, "status": status_line,
        "price_team1": p1, "price_team2": p2, "price_draw": p_draw,
    }


async def scrape_page_dom(page, now: str, page_url: str) -> List[Dict]:
    """
    Pure odds-density DOM scrape. No class name assumptions.
    Finds all divs containing 2+ decimal numbers and extracts the smallest
    bounding containers that have a clean odds cluster.
    """
    records = []
    try:
        cards_data = await page.evaluate("""() => {
            const ODDS_RE = /\\b(\\d{1,3}\\.\\d{2,3})\\b/g;
            const MIN_ODDS = 1.01, MAX_ODDS = 500;

            function extractOdds(text) {
                const matches = [];
                let m;
                ODDS_RE.lastIndex = 0;
                while ((m = ODDS_RE.exec(text)) !== null) {
                    const v = parseFloat(m[1]);
                    if (v >= MIN_ODDS && v <= MAX_ODDS) matches.push(v);
                }
                return matches;
            }

            const results = [];
            const seen = new Set();

            // Walk all divs and find the smallest ones that have 2+ odds
            // while also containing team-name-like text (non-numeric lines > 3 chars)
            const allDivs = Array.from(document.querySelectorAll('div, article, section, a'));

            for (const el of allDivs) {
                // Skip containers that are too large (whole page sections)
                if (el.children.length > 25) continue;

                const text = el.innerText || '';
                if (text.length < 10 || text.length > 1000) continue;

                const odds = extractOdds(text);
                if (odds.length < 2 || odds.length > 8) continue;

                // Must have at least 2 non-numeric text lines (team names)
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 2);
                const nonNumLines = lines.filter(l => !/^[\\d.]+$/.test(l) && !/^\\d{1,2}$/.test(l));
                if (nonNumLines.length < 2) continue;

                const key = text.substring(0, 80);
                if (seen.has(key)) continue;

                // Skip if a parent already covers same text (prefer smallest container)
                let dominated = false;
                for (const prev of results) {
                    if (prev.text.includes(key) || key.includes(prev.text.substring(0, 60))) {
                        // If this element is smaller/more specific, replace
                        if (text.length < prev.text.length) {
                            results.splice(results.indexOf(prev), 1);
                            seen.delete(prev.text.substring(0, 80));
                        } else {
                            dominated = true;
                        }
                        break;
                    }
                }
                if (dominated) continue;

                seen.add(key);
                const href = el.tagName === 'A' ? el.href :
                             (el.querySelector('a[href*="/sports/"]') ?
                              el.querySelector('a[href*="/sports/"]').href : '');

                results.push({ text: text.substring(0, 700), href, odds });
            }

            return results.slice(0, 150);
        }""")

        logger.info(f"DOM: found {len(cards_data)} card candidates on {page_url}")

        for card in cards_data:
            raw_text = card.get("text", "")
            href = card.get("href", "") or page_url
            lines = [l.strip() for l in raw_text.split("\n") if l.strip()]

            parsed = parse_card_lines(lines)
            if not parsed:
                logger.debug(f"  Unparsed: {raw_text[:120]!r}")
                continue

            records.append({
                "bookmaker": "stake",
                "game_raw": parsed["game_raw"],
                "tournament_name": parsed["tournament_name"],
                "team1": parsed["team1"],
                "team2": parsed["team2"],
                "match_start_time": parsed.get("status", ""),
                "match_url": href,
                "price_team1": parsed["price_team1"],
                "price_team2": parsed["price_team2"],
                "price_draw": parsed["price_draw"],
                "scraped_at": now,
            })

    except Exception as e:
        logger.error(f"DOM scrape error: {e}")

    return records


async def navigate_and_scrape(page, url: str, now: str, label: str) -> List[Dict]:
    """Navigate to url, verify content loaded, scrape DOM."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        logger.warning(f"{label}: navigation timeout")
        return []
    except Exception as e:
        logger.warning(f"{label}: navigation error: {e}")
        return []

    # Wait for actual content — blank page guard
    content_ok = await wait_for_content(page, timeout_s=15)
    if not content_ok:
        logger.warning(f"{label}: page content never appeared")
        # Save debug artifact
        try:
            html = await page.content()
            await Actor.set_value(
                f"debug_blank_{label.replace('/', '_')}",
                html, content_type="text/html"
            )
        except Exception:
            pass
        return []

    # Extra wait for React to render match cards
    await asyncio.sleep(3)

    records = await scrape_page_dom(page, now, url)
    logger.info(f"{label}: {len(records)} records")
    return records


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_url = input_data.get("proxyUrl") or os.environ.get("OXYLABS_PROXY") or DEFAULT_PROXY
        stake_base = input_data.get("stakeBaseUrl") or STAKE_BASE
        max_matches = input_data.get("maxMatches", 200)
        headless = input_data.get("headless", False)

        actor.log.info(
            f"Stake scraper v4 | base={stake_base} headless={headless} "
            f"stealth={'v2' if STEALTH_AVAILABLE else 'MISSING'}"
        )

        now = datetime.now(timezone.utc).isoformat()
        intercepted_records: List[Dict] = []
        seen_keys: set = set()

        launch_args = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--disable-automation",
            ]
        }

        if proxy_url:
            launch_args["proxy"] = proxy_url if isinstance(proxy_url, dict) else {"server": proxy_url}
            actor.log.info("Proxy: Oxylabs residential CA")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-CA",
                timezone_id="America/Edmonton",
                java_script_enabled=True,
                extra_http_headers={
                    "Accept-Language": "en-CA,en;q=0.9",
                    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )

            if STEALTH_AVAILABLE:
                await Stealth().apply_stealth_async(context)
                actor.log.info("Stealth v2 applied ✓")

            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            # GQL interception — attached BEFORE any navigation
            async def on_response(response):
                try:
                    if "/_api/graphql" not in response.url:
                        return
                    if response.status != 200:
                        return
                    body = await response.json()
                    recs = extract_records_from_graphql(body, now)
                    if recs:
                        logger.info(f"GQL intercept: {len(recs)} records")
                        intercepted_records.extend(recs)
                    else:
                        try:
                            req_data = await response.request.post_data_json()
                            op = req_data.get("operationName") if isinstance(req_data, dict) else None
                            data_keys = list((body.get("data") or {}).keys())
                            logger.info(f"GQL op={op} keys={data_keys}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"Response handler: {e}")

            page.on("response", on_response)

            # ── Hub navigation with full content wait ────────────────────
            hub_url = f"{stake_base}/sports/esports"
            actor.log.info(f"Navigating hub: {hub_url}")

            try:
                await page.goto(hub_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                actor.log.error(f"Hub navigation failed: {e}")
                await browser.close()
                await actor.push_data({"error": "navigation_failed", "message": str(e)})
                return

            # CF check — wait for real title AND content
            cf_ok = await handle_cf_challenge(page)
            if not cf_ok:
                actor.log.error("CF not solved — saving debug")
                try:
                    html = await page.content()
                    await Actor.set_value("debug_cf_html", html, content_type="text/html")
                    shot = await page.screenshot(full_page=True, timeout=15000)
                    await Actor.set_value("debug_cf_screenshot", shot, content_type="image/png")
                except Exception:
                    pass
                await browser.close()
                await actor.push_data({"error": "cf_not_solved"})
                return

            # After CF passes, wait for actual SPA content
            content_ok = await wait_for_content(page, timeout_s=20)
            if not content_ok:
                actor.log.warning("Hub page content never appeared after CF — will still attempt scrape")

            # Extra settle time for React to render match cards
            await asyncio.sleep(4)

            # Save hub screenshot for debugging
            try:
                shot = await page.screenshot(full_page=True, timeout=15000)
                await Actor.set_value("debug_hub_screenshot", shot, content_type="image/png")
                html = await page.content()
                await Actor.set_value("debug_hub_html", html, content_type="text/html")
            except Exception:
                pass

            all_records: List[Dict] = []

            # Scrape hub
            hub_records = await scrape_page_dom(page, now, hub_url)
            actor.log.info(f"Hub DOM: {len(hub_records)} records")
            all_records.extend(hub_records)

            # Drill game pages
            actor.log.info(f"Drilling {len(ESPORTS_GAME_SLUGS)} game pages...")
            for slug in ESPORTS_GAME_SLUGS:
                game_url = f"{stake_base}/sports/esports/{slug}"
                game_records = await navigate_and_scrape(page, game_url, now, slug)
                all_records.extend(game_records)

            actor.log.info(f"GQL intercepted: {len(intercepted_records)} | DOM total: {len(all_records)}")

            # Merge + deduplicate
            final_records: List[Dict] = []
            for rec in (intercepted_records + all_records):
                t1 = (rec.get("team1") or "").strip().lower()
                t2 = (rec.get("team2") or "").strip().lower()
                if not t1 or not t2:
                    continue
                key = f"{t1}||{t2}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    final_records.append(rec)

            final_records = final_records[:max_matches]
            actor.log.info(f"Final unique records: {len(final_records)}")

            await browser.close()

        for rec in final_records:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True,
            "bookmaker": "stake",
            "records_total": len(final_records),
            "gql_records": len(intercepted_records),
            "dom_records": len(all_records),
            "method": "gql+dom",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
