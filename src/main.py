#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper

ARCHITECTURE (2026-05-28):
Stake's GraphQL API at /_api/graphql requires authentication for odds data.
The path to odds is: sport → tournamentList → sportTournament.fixtureList →
markets.outcomes.{id,name,odds} — all gated behind 'Please log in'.

Strategy: Use Playwright to establish a real browser session (bypassing CF via
Oxylabs residential proxy), then intercept the GraphQL network requests the
browser makes natively as it loads the esports pages. The browser sends the
correct x-access-token / cf_clearance cookies automatically.

Flow:
1. Launch Chromium with Oxylabs residential proxy + stealth v2
2. Navigate to stake.com/sports/esports — let it load fully (CF + SPA hydrate)
3. Intercept all /_api/graphql POST responses while the page loads
4. Parse intercepted responses for SportFixture / SportTournament data with odds
5. If interception catches nothing, fall back to DOM scraping of rendered odds buttons

CF Note: Apify containers run headless on datacenter IPs. Oxylabs residential
proxy is the primary CF bypass mechanism — the browser fingerprint routes through
a real Canadian residential IP. Turnstile will still challenge but residential
proxies have a much higher solve rate than datacenter. We give it 90s.
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from apify import Actor
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Route, Request

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
    "password": "gX~dawV=8MzVzA",
}

STAKE_BASE = "https://stake.com"
GRAPHQL_URL = "https://stake.com/_api/graphql"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def parse_decimal_odds(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    t = str(val).strip().replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", t)
    return float(m.group(1)) if m else None


def safe_kv_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def handle_cf_challenge(page, max_wait_s: int = 90) -> bool:
    """Wait for CF Turnstile to resolve. 90s budget for residential proxy."""
    for i in range(max_wait_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            cur_url = page.url
        except Exception:
            continue
        if i % 10 == 0:
            logger.info(f"CF wait {i+1}s | title={title!r}")
        if (
            title != "Just a moment..."
            and "__cf_chl" not in cur_url
            and "challenge" not in cur_url.lower()
            and "cf-challenge" not in title.lower()
        ):
            logger.info(f"CF resolved at {i+1}s")
            await asyncio.sleep(3)  # SPA hydration buffer
            return True
    return False


def extract_records_from_graphql(payload: dict, now: str) -> List[Dict]:
    """Parse any intercepted GraphQL response for odds data."""
    records = []
    data = payload.get("data") or {}

    # Handle sportTournament.fixtureList path
    tournament = data.get("sportTournament")
    if tournament:
        t_name = tournament.get("name", "")
        for fixture in tournament.get("fixtureList", []):
            rec = parse_fixture(fixture, t_name, now)
            if rec:
                records.extend(rec)

    # Handle sportList path (returns sports, not events — skip)
    # Handle any top-level fixture list
    for key in ["fixtureList", "fixtures", "events"]:
        if key in data and isinstance(data[key], list):
            for fixture in data[key]:
                rec = parse_fixture(fixture, "", now)
                if rec:
                    records.extend(rec)

    return records


def parse_fixture(fixture: dict, tournament_name: str, now: str) -> Optional[List[Dict]]:
    """Parse a single fixture dict into odds records."""
    if not isinstance(fixture, dict):
        return None

    name = fixture.get("name", "")
    start_time = fixture.get("startTime", "")
    slug = fixture.get("slug", "")
    match_url = f"{STAKE_BASE}/sports/esports/{slug}" if slug else ""

    # Extract team names from fixture name (format: "Team A vs Team B" or "Team A - Team B")
    team1, team2 = "", ""
    vs_match = re.split(r"\s+(?:vs\.?|VS\.?|–|-)\s+", name, maxsplit=1)
    if len(vs_match) == 2:
        team1, team2 = vs_match[0].strip(), vs_match[1].strip()

    records = []
    target_markets = {"match winner", "match result", "winner", "1x2", "moneyline"}

    for market in fixture.get("markets", []):
        if not isinstance(market, dict):
            continue
        mkt_name = market.get("name", "").lower()
        # Accept match-winner markets OR take first market if we have no name (auth-gated name)
        if mkt_name and mkt_name not in target_markets:
            continue

        outcomes = market.get("outcomes", [])
        if len(outcomes) < 2:
            continue

        p1 = p2 = p_draw = None
        for o in outcomes:
            o_name = (o.get("name") or "").lower()
            odds = o.get("odds")
            if not odds:
                continue
            if team1 and team1.lower() in o_name:
                p1 = parse_decimal_odds(odds)
            elif team2 and team2.lower() in o_name:
                p2 = parse_decimal_odds(odds)
            elif any(x in o_name for x in {"draw", "tie", "x", "home", "1"}) and p1 is None:
                p1 = parse_decimal_odds(odds)
            elif any(x in o_name for x in {"away", "2"}) and p2 is None:
                p2 = parse_decimal_odds(odds)
            elif "draw" in o_name or "tie" in o_name:
                p_draw = parse_decimal_odds(odds)

        if p1 or p2:
            records.append({
                "bookmaker": "stake",
                "game_raw": fixture.get("sport", {}).get("name", "") if isinstance(fixture.get("sport"), dict) else "",
                "tournament_name": tournament_name,
                "team1": team1,
                "team2": team2,
                "match_start_time": start_time,
                "match_url": match_url,
                "price_team1": p1,
                "price_team2": p2,
                "price_draw": p_draw,
                "scraped_at": now,
            })

    return records or None


async def scrape_via_dom(page, now: str) -> List[Dict]:
    """
    Last-resort DOM scrape: find rendered odds buttons on the page.
    Stake renders odds as buttons with decimal values. Look for button clusters
    near team name text.
    """
    records = []
    try:
        dom_data = await page.evaluate("""() => {
            const results = [];
            // Find all elements with decimal odds values
            const allEls = document.querySelectorAll('*');
            const oddsEls = [];
            allEls.forEach(el => {
                if (el.children.length === 0) {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (/^\\d+\\.\\d{2,3}$/.test(text)) {
                        oddsEls.push(el);
                    }
                }
            });

            // Group nearby odds elements — look for clusters of 2-3 odds near team names
            const groups = [];
            for (let i = 0; i < oddsEls.length - 1; i++) {
                const rect1 = oddsEls[i].getBoundingClientRect();
                const rect2 = oddsEls[i+1].getBoundingClientRect();
                // Same row or close vertical proximity
                if (Math.abs(rect1.top - rect2.top) < 50) {
                    // Find containing match card
                    const card = oddsEls[i].closest('[class*="event"], [class*="match"], [class*="fixture"], article, [role="listitem"]');
                    const cardText = card ? (card.innerText || '').substring(0, 400) : '';
                    const odds = [];
                    let j = i;
                    while (j < oddsEls.length) {
                        const r = oddsEls[j].getBoundingClientRect();
                        if (Math.abs(r.top - rect1.top) < 50) {
                            odds.push(parseFloat((oddsEls[j].innerText || '').trim()));
                            j++;
                        } else break;
                    }
                    if (odds.length >= 2) {
                        groups.push({cardText: cardText, odds: odds});
                        i = j - 1; // skip consumed
                    }
                }
            }
            return groups.slice(0, 50);
        }""")

        logger.info(f"DOM scrape found {len(dom_data)} odds groups")
        for g in dom_data:
            logger.info(f"  Group: {g['cardText'][:100]} | odds={g['odds']}")
            # Try to extract team names from card text
            card_text = g["cardText"]
            vs_match = re.split(r"\s+(?:vs\.?|VS\.?|–|-)\s+", card_text, maxsplit=1)
            team1 = vs_match[0].strip()[:50] if len(vs_match) == 2 else ""
            team2 = vs_match[1].strip()[:50] if len(vs_match) == 2 else ""
            odds = g["odds"]
            if len(odds) >= 2:
                records.append({
                    "bookmaker": "stake",
                    "game_raw": "",
                    "tournament_name": "",
                    "team1": team1,
                    "team2": team2,
                    "match_start_time": "",
                    "match_url": page.url,
                    "price_team1": odds[0] if len(odds) > 0 else None,
                    "price_team2": odds[1] if len(odds) > 1 else None,
                    "price_draw": odds[2] if len(odds) > 2 else None,
                    "scraped_at": now,
                })
    except Exception as e:
        logger.error(f"DOM scrape failed: {e}")
    return records


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_url = input_data.get("proxyUrl") or os.environ.get("OXYLABS_PROXY") or DEFAULT_PROXY
        stake_base = input_data.get("stakeBaseUrl") or STAKE_BASE
        max_matches = input_data.get("maxMatches", 100)
        headless = input_data.get("headless", False)

        actor.log.info(
            f"Stake scraper | base={stake_base} headless={headless} "
            f"stealth={'v2' if STEALTH_AVAILABLE else 'MISSING'}"
        )

        # Intercepted GraphQL responses accumulate here
        intercepted_records: List[Dict] = []
        now = datetime.now(timezone.utc).isoformat()

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
            if isinstance(proxy_url, dict):
                launch_args["proxy"] = proxy_url
            else:
                launch_args["proxy"] = {"server": proxy_url}
            actor.log.info("Proxy: Oxylabs (residential CA)")

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
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)

            # ── NETWORK INTERCEPTION ──────────────────────────────────────
            # Intercept all /_api/graphql responses and parse them for odds
            async def handle_response(response):
                try:
                    url = response.url
                    if "/_api/graphql" not in url:
                        return
                    if response.status != 200:
                        return
                    try:
                        body = await response.json()
                    except Exception:
                        return
                    if not isinstance(body, dict):
                        return

                    recs = extract_records_from_graphql(body, now)
                    if recs:
                        logger.info(f"Intercepted {len(recs)} records from GraphQL response")
                        intercepted_records.extend(recs)
                    else:
                        # Log the operation name for debugging
                        req_post = None
                        try:
                            req_post = await response.request.post_data_json()
                        except Exception:
                            pass
                        op = req_post.get("operationName") if isinstance(req_post, dict) else None
                        if op:
                            logger.debug(f"GraphQL op={op} | data keys={list((body.get('data') or {}).keys())}")
                except Exception as e:
                    logger.debug(f"Response handler error: {e}")

            page.on("response", handle_response)
            # ─────────────────────────────────────────────────────────────

            # Navigate to the esports hub
            esports_url = f"{stake_base}/sports/esports"
            actor.log.info(f"Navigating to {esports_url}")

            try:
                await page.goto(esports_url, wait_until="commit", timeout=30000)
            except Exception as e:
                actor.log.error(f"Navigation failed: {e}")
                await browser.close()
                await actor.push_data({"error": "navigation_failed", "message": str(e)})
                return

            # Wait for CF
            cf_ok = await handle_cf_challenge(page)
            if not cf_ok:
                actor.log.error("CF challenge not solved — saving debug artifacts")
                try:
                    html = await page.content()
                    await Actor.set_value("debug_cf_blocked_html", html, content_type="text/html")
                    shot = await page.screenshot(full_page=True, timeout=15000)
                    await Actor.set_value("debug_cf_blocked_screenshot", shot, content_type="image/png")
                except Exception:
                    pass
                await browser.close()
                await actor.push_data({"error": "cf_not_solved", "url": esports_url})
                return

            actor.log.info("Past CF — waiting for SPA to load and fire GraphQL requests")

            # Wait for page to fully hydrate and fire all initial GraphQL queries
            # Stake SPA fires multiple queries on page load — give it time
            await asyncio.sleep(8)

            # Also wait for network idle
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # Save debug HTML/screenshot
            try:
                html = await page.content()
                await Actor.set_value("debug_page_html", html, content_type="text/html")
                shot = await page.screenshot(full_page=True, timeout=15000)
                await Actor.set_value("debug_page_screenshot", shot, content_type="image/png")
            except Exception as e:
                logger.warning(f"Debug save failed: {e}")

            actor.log.info(f"Intercepted {len(intercepted_records)} records from GraphQL so far")

            # If interception got nothing, try navigating to individual game pages
            # to trigger more GraphQL calls with authenticated session cookies
            if len(intercepted_records) < 5:
                actor.log.info("Interception sparse — drilling into game pages to trigger more GQL calls")

                # Get the list of game links from the loaded page
                try:
                    game_links = await page.evaluate("""() => {
                        const links = [];
                        document.querySelectorAll('a[href*="/sports/esports/"]').forEach(a => {
                            if (a.href && !links.find(l => l.href === a.href)) {
                                links.push({href: a.href, text: (a.innerText || '').trim().substring(0, 60)});
                            }
                        });
                        return links.slice(0, 8);
                    }""")
                    actor.log.info(f"Found {len(game_links)} game category links")

                    for link in game_links[:6]:
                        try:
                            actor.log.info(f"Navigating to game: {link['href']}")
                            await page.goto(link["href"], wait_until="commit", timeout=20000)
                            # No new CF challenge expected — cookies are set
                            await asyncio.sleep(5)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"Game page nav failed: {e}")
                            continue
                except Exception as e:
                    logger.error(f"Game link extraction failed: {e}")

            actor.log.info(f"After game page drilling: {len(intercepted_records)} intercepted records")

            # Fall back to DOM scrape if interception still empty
            records = intercepted_records[:max_matches]
            if not records:
                actor.log.info("No intercepted records — falling back to DOM scrape")
                await page.goto(esports_url, wait_until="commit", timeout=20000)
                await asyncio.sleep(6)
                dom_records = await scrape_via_dom(page, now)
                actor.log.info(f"DOM scrape found {len(dom_records)} records")
                records = dom_records[:max_matches]

            await browser.close()

        actor.log.info(f"Total records: {len(records)}")
        for rec in records:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True,
            "bookmaker": "stake",
            "records_total": len(records),
            "method": "graphql_intercept" if intercepted_records else "dom_fallback",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
