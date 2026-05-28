#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper

Navigates Stake with Playwright + playwright-stealth v2, handles Cloudflare
Managed Challenge, drills into match pages, and extracts timestamped odds.

STEALTH FIX (2026-05-27):
- playwright-stealth v2 dropped stealth_async() function entirely
- Correct v2 API: Stealth().apply_stealth_async(context) on BrowserContext
- Applied at context level (not page level) so all new pages inherit evasions
- headless=False default (CF bot detection trivially beats headless Chromium)
- Extended CF wait from 25s to 60s — Turnstile can take 30-40s on first solve
- Added proper navigator.webdriver=false via CDP in addition to stealth
- Removed AutomationControlled flag (still present in args but stealth masks it)
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

# Oxylabs proxy config (separate fields for Chromium compat)
DEFAULT_PROXY = {
    "server": "http://pr.oxylabs.io:7777",
    "username": "customer-sonus_TbxLY-cc-ca-city-edmonton",
    "password": "gX~dawV=8MzVzA",
}

STAKE_BASE = "https://stake.com"

# Realistic Chrome 126 UA — must match the sec-ch-ua headers below
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def parse_decimal_odds(text: str) -> Optional[float]:
    if not text:
        return None
    t = text.strip().replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", t)
    return float(m.group(1)) if m else None


def safe_kv_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


async def handle_cf_challenge(page, url: str, max_wait_s: int = 60) -> bool:
    """Wait for Cloudflare Turnstile/Managed Challenge to resolve.
    Returns True once we're past CF and onto the real page.
    Turnstile can take 30-40s on cold solve — 60s budget is realistic.
    """
    for i in range(max_wait_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            cur_url = page.url
        except Exception:
            continue
        logger.info(f"CF wait {i+1}s | title={title!r} | url={cur_url[:80]}")
        if (
            title != "Just a moment..."
            and "__cf_chl" not in cur_url
            and "challenge" not in cur_url
            and "cf-challenge" not in title.lower()
        ):
            # Extra delay for Stake SPA React hydration after CF resolves
            await asyncio.sleep(4)
            return True
    return False


async def save_debug(page, label: str):
    safe = safe_kv_key(label)
    try:
        html = await page.content()
        await Actor.set_value(f"debug_{safe}_html", html, content_type="text/html")
    except Exception as e:
        logger.warning(f"debug html save failed: {e}")
    try:
        screenshot = await page.screenshot(full_page=True, timeout=15000)
        await Actor.set_value(f"debug_{safe}_screenshot", screenshot, content_type="image/png")
    except Exception as e:
        logger.warning(f"debug screenshot save failed: {e}")


async def extract_matches_from_listing(page) -> List[Dict]:
    """Try to find match cards on the current listing page."""
    records: List[Dict] = []

    # Probe window state (React/Redux/Apollo)
    try:
        state_data = await page.evaluate("""() => {
            for (const key of Object.keys(window)) {
                if (
                    key.toLowerCase().includes('initial') ||
                    key.toLowerCase().includes('redux') ||
                    key.toLowerCase().includes('apollo') ||
                    key.toLowerCase().includes('relay')
                ) {
                    try {
                        const val = window[key];
                        if (val && typeof val === 'object') {
                            const str = JSON.stringify(val);
                            if (str.includes('odds') || str.includes('match') || str.includes('event')) {
                                return {source: key, preview: str.substring(0, 2000)};
                            }
                        }
                    } catch(e) {}
                }
            }
            return null;
        }""")
        if state_data:
            logger.info(f"Found window state: {state_data['source']}")
            await Actor.set_value("debug_window_state", state_data["preview"], content_type="text/plain")
    except Exception as e:
        logger.debug(f"State probe failed: {e}")

    # DOM mining for match cards
    try:
        dom_matches = await page.evaluate("""() => {
            const results = [];

            // Strategy 1: data-attribute elements
            document.querySelectorAll('[data-testid], [data-event], [data-match]').forEach(el => {
                const text = el.innerText || '';
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                const oddsLines = lines.filter(l => /^\\d+\\.\\d+$/.test(l));
                if (oddsLines.length >= 1) {
                    results.push({strategy: 'data-attr', text: text.substring(0, 500), odds: oddsLines});
                }
            });

            // Strategy 2: odds buttons
            document.querySelectorAll('button, [role="button"]').forEach(btn => {
                const text = (btn.innerText || '').trim();
                if (/^\\d+\\.\\d+$/.test(text)) {
                    const parent = btn.closest('div, article, section');
                    const parentText = parent ? (parent.innerText || '').substring(0, 300) : '';
                    results.push({strategy: 'odds-button', odds: text, context: parentText});
                }
            });

            // Strategy 3: team vs pattern with odds
            document.querySelectorAll('div, article, section').forEach(container => {
                const text = container.innerText || '';
                const teamPattern = /([A-Z][A-Za-z0-9\\s]{2,25})\\s+(?:vs?\\.?|–|-)\\s+([A-Z][A-Za-z0-9\\s]{2,25})/;
                const match = text.match(teamPattern);
                if (match) {
                    const odds = text.match(/\\d+\\.\\d+/g);
                    if (odds && odds.length >= 2) {
                        results.push({
                            strategy: 'team-vs-pattern',
                            team1: match[1].trim(),
                            team2: match[2].trim(),
                            odds: odds.slice(0, 3)
                        });
                    }
                }
            });

            return results.slice(0, 20);
        }""")
        logger.info(f"DOM mining found {len(dom_matches)} candidates")
        for dm in dom_matches:
            logger.info(f"  DOM: {json.dumps(dm, default=str)[:200]}")
    except Exception as e:
        logger.debug(f"DOM mining failed: {e}")

    # Try common Stake selectors
    card_selectors = [
        '[data-testid*="event"]',
        '[data-testid*="match"]',
        '[data-testid*="game"]',
        '[class*="EventCard"]',
        '[class*="event-card"]',
        '[class*="MatchCard"]',
        '[class*="match-card"]',
        'article',
        '[role="listitem"]',
    ]
    for sel in card_selectors:
        cards = await page.query_selector_all(sel)
        if cards:
            logger.info(f"Selector '{sel}' found {len(cards)} cards")
            for card in cards[:5]:
                try:
                    text = await card.inner_text()
                    if len(text) > 10:
                        logger.info(f"  Card text: {text[:200]}")
                except Exception:
                    pass
            break

    return records


async def scrape_sport(page, url: str, sport_name: str, max_matches: int) -> List[Dict]:
    records: List[Dict] = []
    logger.info(f"Navigating to {url}")

    try:
        resp = await page.goto(url, wait_until="commit", timeout=30000)
        logger.info(f"Commit reached | status={resp.status if resp else 'unknown'}")
    except PWTimeout:
        logger.error("Navigation commit timeout")
        return records
    except Exception as e:
        logger.error(f"Navigation error: {e}")
        return records

    cf_ok = await handle_cf_challenge(page, url)
    if not cf_ok:
        logger.error("Cloudflare challenge not solved within budget")
        await save_debug(page, f"cf_blocked_{safe_kv_key(sport_name)}")
        return records

    logger.info("Past Cloudflare — page loaded")
    await save_debug(page, sport_name)

    listing_records = await extract_matches_from_listing(page)
    records.extend(listing_records)

    match_links = await page.evaluate("""(maxMatches) => {
        const links = [];
        document.querySelectorAll(
            'a[href*="/sports/esports/"], a[href*="/event/"], a[href*="/match/"]'
        ).forEach(a => {
            links.push({href: a.href, text: (a.innerText || '').trim().substring(0, 100)});
        });
        return links.slice(0, maxMatches);
    }""", max_matches)
    logger.info(f"Found {len(match_links)} match links")

    for idx, link in enumerate(match_links[:max_matches]):
        try:
            logger.info(f"Drilling into match {idx+1}/{len(match_links)}: {link['href']}")
            match_page = await page.context.new_page()
            await match_page.goto(link["href"], wait_until="commit", timeout=30000)
            cf_ok = await handle_cf_challenge(match_page, link["href"], max_wait_s=30)

            if not cf_ok:
                await match_page.close()
                continue

            await save_debug(match_page, f"match_{idx}_{safe_kv_key(sport_name)}")

            match_data = await match_page.evaluate("""() => {
                const data = {title: document.title, url: window.location.href, teams: [], odds: [], markets: []};
                const allText = document.body.innerText;
                const vsMatches = allText.match(/([A-Z][A-Za-z0-9\\s&]{2,30})\\s+(?:vs\\.?|VS\\.?|–|-)\\s+([A-Z][A-Za-z0-9\\s&]{2,30})/g);
                if (vsMatches) {
                    vsMatches.forEach(m => {
                        const parts = m.split(/\\s+(?:vs\\.?|VS\\.?|–|-)\\s+/);
                        if (parts.length === 2) data.teams.push({team1: parts[0].trim(), team2: parts[1].trim()});
                    });
                }
                const oddsMatches = allText.match(/\\b\\d+\\.\\d{2,3}\\b/g);
                if (oddsMatches) data.odds = oddsMatches.slice(0, 10);
                document.querySelectorAll('h1,h2,h3,h4,[class*="market"],[class*="Market"]').forEach(el => {
                    const text = (el.innerText || '').trim();
                    if (text && text.length < 100) data.markets.push(text);
                });
                return data;
            }""")

            logger.info(f"Match {idx+1}: {json.dumps(match_data, default=str)[:300]}")

            if match_data.get("teams"):
                team_pair = match_data["teams"][0]
                odds = match_data.get("odds", [])
                records.append({
                    "bookmaker": "stake",
                    "game_raw": sport_name,
                    "tournament_name": "",
                    "team1": team_pair.get("team1", ""),
                    "team2": team_pair.get("team2", ""),
                    "match_start_time": "",
                    "match_url": link["href"],
                    "price_team1": parse_decimal_odds(odds[0]) if len(odds) > 0 else None,
                    "price_team2": parse_decimal_odds(odds[1]) if len(odds) > 1 else None,
                    "price_draw": parse_decimal_odds(odds[2]) if len(odds) > 2 else None,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                })

            await match_page.close()

        except Exception as e:
            logger.error(f"Match drill error: {e}")
            continue

    return records[:max_matches]


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_url = input_data.get("proxyUrl") or os.environ.get("OXYLABS_PROXY") or DEFAULT_PROXY
        stake_base = input_data.get("stakeBaseUrl") or STAKE_BASE
        max_matches = input_data.get("maxMatches", 50)
        # CF absolutely requires headless=False — Turnstile detects headless Chromium trivially
        headless = input_data.get("headless", False)

        actor.log.info(
            f"Stake scraper | base={stake_base} proxy={'set' if proxy_url else 'none'} "
            f"headless={headless} stealth={'v2' if STEALTH_AVAILABLE else 'MISSING'}"
        )

        if not STEALTH_AVAILABLE:
            actor.log.warning(
                "playwright-stealth not importable — CF bypass will likely fail. "
                "Ensure requirements.txt includes playwright-stealth>=2.0.0"
            )

        launch_args = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                # Additional evasion flags
                "--window-size=1920,1080",
                "--start-maximized",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-automation",
                "--excludeSwitches=enable-automation",
                "--useAutomationExtension=false",
            ]
        }

        if proxy_url:
            if isinstance(proxy_url, dict):
                launch_args["proxy"] = proxy_url
                actor.log.info("Proxy: Oxylabs Edmonton (dict format)")
            else:
                launch_args["proxy"] = {"server": proxy_url}
                actor.log.info("Proxy: configured (URL format)")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_args)

            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-CA",
                timezone_id="America/Edmonton",
                java_script_enabled=True,
                # Realistic browser hints matching the UA above
                extra_http_headers={
                    "Accept-Language": "en-CA,en;q=0.9",
                    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )

            # ── STEALTH v2 FIX ──────────────────────────────────────────────
            # Old code (BROKEN): from playwright_stealth import stealth_async
            #   → stealth_async does not exist in v2; ImportError silently
            #     caught, stealth never applied, CF blocks immediately.
            #
            # Correct v2 pattern: Stealth().apply_stealth_async(context)
            #   → applies to BrowserContext so ALL pages (including new tabs
            #     opened for match drilldown) inherit all evasions.
            # ────────────────────────────────────────────────────────────────
            if STEALTH_AVAILABLE:
                stealth = Stealth(
                    navigator_webdriver=True,   # patches navigator.webdriver → false
                    chrome_runtime=True,        # injects fake chrome.runtime
                    navigator_plugins=True,     # adds fake plugin list
                    navigator_languages=True,
                    navigator_platform=True,
                    webgl_vendor=True,
                    hairline=True,
                    media_codecs=True,
                )
                await stealth.apply_stealth_async(context)
                actor.log.info("Stealth v2 applied to BrowserContext ✓")
            else:
                actor.log.warning("Stealth NOT applied — CF bypass unlikely to work")

            page = await context.new_page()

            # Additionally override navigator.webdriver via CDP (belt-and-suspenders)
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-CA', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            """)

            # Scrape esports hub
            url = f"{stake_base}/sports/esports"
            records = await scrape_sport(page, url, "Esports", max_matches)

            await browser.close()

        actor.log.info(f"Total records: {len(records)}")
        for rec in records:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True,
            "bookmaker": "stake",
            "records_total": len(records),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })


if __name__ == "__main__":
    asyncio.run(main())
