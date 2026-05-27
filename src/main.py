#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper

Uses Playwright + stealth to navigate Stake's esports pages and extract
match odds from the DOM. Handles Cloudflare challenges via stealth + proxy.

Environment:
  APIFY_TOKEN   – Apify API token (required)
  OXYLABS_USER  – Oxylabs username (fallback to proxyConfiguration)
  OXYLABS_PASS  – Oxylabs password (fallback to proxyConfiguration)
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from apify import Actor
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-scraper")

# Stake esports sport slugs (from live GraphQL probe + manual inspection)
STAKE_ESPORTS = [
    {"slug": "esports", "name": "Esports"},
    {"slug": "dota-2", "name": "Dota 2"},
    {"slug": "counter-strike", "name": "CS2"},
    {"slug": "league-of-legends", "name": "League of Legends"},
]


def parse_odds(text: str) -> Optional[float]:
    """Extract decimal odds from text like '1.85' or '-185'."""
    if not text:
        return None
    text = text.strip().replace(",", ".")
    m = re.search(r"(\d+\.?\d*)", text)
    return float(m.group(1)) if m else None


async def scrape_stake_page(page, url: str, sport_name: str, max_matches: int) -> List[Dict]:
    """Scrape a single Stake sport page."""
    records: List[Dict] = []
    logger.info(f"Navigating to {url}")

    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=60000)
        logger.info(f"Page loaded: status={resp.status if resp else 'unknown'}")
    except Exception as e:
        logger.error(f"Navigation failed: {e}")
        return records

    # Wait for dynamic content
    await asyncio.sleep(5)

    # Save debug artifacts
    try:
        html = await page.content()
        await Actor.set_value(f"debug_{sport_name}_html", html, content_type="text/html")
        screenshot = await page.screenshot(full_page=True)
        await Actor.set_value(f"debug_{sport_name}_screenshot", screenshot, content_type="image/png")
        logger.info(f"Saved debug artifacts for {sport_name}")
    except Exception as e:
        logger.warning(f"Failed to save debug artifacts: {e}")

    # ── Strategy 1: Look for structured data in <script type="application/ld+json"> ──
    ld_jsons = await page.query_selector_all('script[type="application/ld+json"]')
    for ld in ld_jsons:
        try:
            raw = await ld.inner_text()
            data = json.loads(raw)
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "SportsEvent":
                team1 = data.get("homeTeam", {}).get("name", "")
                team2 = data.get("awayTeam", {}).get("name", "")
                if team1 and team2:
                    records.append({
                        "bookmaker": "stake",
                        "game_raw": sport_name,
                        "tournament_name": data.get("description", ""),
                        "team1": team1,
                        "team2": team2,
                        "match_start_time": data.get("startDate", ""),
                        "match_url": url,
                        "price_team1": None,
                        "price_team2": None,
                        "price_draw": None,
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                    })
        except Exception:
            continue

    if records:
        logger.info(f"Found {len(records)} events via ld+json")
        return records[:max_matches]

    # ── Strategy 2: CSS selector extraction with multiple attempts ──
    selector_groups = [
        # Group A: data-testid based (common on stake)
        {
            "card": '[data-testid="sport-event"]',
            "team":  '[data-testid*="competitor"] span, [data-testid*="team"] span',
            "odds":  '[data-testid*="outcome"] .chakra-text, [data-testid*="odds"]',
            "league": '[data-testid="event-league"]',
        },
        # Group B: class-based
        {
            "card": '[class*="EventCard"], [class*="event-card"]',
            "team":  '[class*="TeamName"], [class*="competitor-name"]',
            "odds":  '[class*="Odds"], [class*="outcome-odds"]',
            "league": '[class*="League"], [class*="tournament"]',
        },
        # Group C: Generic HTML structure
        {
            "card": 'div > div > div',  # fallback – scan all nested divs
            "team":  'span',
            "odds":  'button span',
            "league": 'h3, h4',
        },
    ]

    for grp_idx, sel in enumerate(selector_groups):
        cards = await page.query_selector_all(sel["card"])
        logger.info(f"Selector group {grp_idx}: found {len(cards)} cards")

        for card in cards:
            try:
                # Get team names
                team_els = await card.query_selector_all(sel["team"])
                teams = [await t.inner_text() for t in team_els if await t.inner_text()]
                teams = [t.strip() for t in teams if len(t.strip()) > 1]
                if len(teams) < 2:
                    continue

                # Get odds
                odds_els = await card.query_selector_all(sel["odds"])
                odds = [parse_odds(await o.inner_text()) for o in odds_els]
                odds = [o for o in odds if o is not None]

                # Get league/tournament
                league_el = await card.query_selector(sel["league"])
                league = (await league_el.inner_text()).strip() if league_el else ""

                records.append({
                    "bookmaker": "stake",
                    "game_raw": sport_name,
                    "tournament_name": league,
                    "team1": teams[0],
                    "team2": teams[1],
                    "match_start_time": "",
                    "match_url": url,
                    "price_team1": odds[0] if len(odds) > 0 else None,
                    "price_team2": odds[1] if len(odds) > 1 else None,
                    "price_draw": None,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                })

                if len(records) >= max_matches:
                    break
            except Exception as e:
                logger.debug(f"Card extraction error: {e}")
                continue

        if records:
            logger.info(f"Extracted {len(records)} records with group {grp_idx}")
            break

    # ── Strategy 3: Brute-force JS data mining ──
    if not records:
        try:
            js_records = await page.evaluate("""() => {
                const data = [];
                const texts = [];
                // Walk all elements looking for pairs of team names and numbers
                document.querySelectorAll('*').forEach(el => {
                    const text = el.innerText || '';
                    if (text.length > 3 && text.length < 200) texts.push(text);
                });
                // Simple heuristic: look for lines with "v", "vs", "-" between team-like words
                for (const text of texts) {
                    const vsMatch = text.match(/([A-Za-z0-9][A-Za-z0-9\s]{2,30})\s+[vV\-]\s+([A-Za-z0-9][A-Za-z0-9\s]{2,30})/);
                    if (vsMatch) {
                        data.push({team1: vsMatch[1].trim(), team2: vsMatch[2].trim(), raw: text});
                    }
                }
                return data.slice(0, 50);
            }""")
            logger.info(f"JS mining found {len(js_records)} candidates")
            for r in js_records[:max_matches]:
                records.append({
                    "bookmaker": "stake",
                    "game_raw": sport_name,
                    "tournament_name": "",
                    "team1": r["team1"],
                    "team2": r["team2"],
                    "match_start_time": "",
                    "match_url": url,
                    "price_team1": None,
                    "price_team2": None,
                    "price_draw": None,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.warning(f"JS mining failed: {e}")

    return records


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_config = input_data.get("proxyConfiguration")
        max_matches = input_data.get("maxMatches", 200)
        include_live = input_data.get("includeLive", True)
        headless = input_data.get("headless", True)

        actor.log.info(f"Starting Stake scraper | proxy={proxy_config is not None} max={max_matches} headless={headless}")

        async with async_playwright() as p:
            launch_args = {
                "headless": headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            }

            # Proxy setup
            if proxy_config:
                try:
                    proxy = await actor.create_proxy_configuration(proxy_config)
                    url = await proxy.new_url()
                    launch_args["proxy"] = {"server": url}
                    actor.log.info(f"Using proxy: {url[:50]}...")
                except Exception as e:
                    actor.log.warning(f"Proxy setup failed: {e}")

            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )

            if stealth_async:
                page = await context.new_page()
                await stealth_async(page)
                actor.log.info("Stealth applied")
            else:
                page = await context.new_page()

            # Warm up with main page
            try:
                actor.log.info("Warming up with stake.com homepage")
                await page.goto("https://stake.com", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
            except Exception as e:
                actor.log.warning(f"Warm-up failed: {e}")

            all_records: List[Dict] = []

            for sport in STAKE_ESPORTS:
                url = f"https://stake.com/sports/{sport['slug']}"
                records = await scrape_stake_page(page, url, sport["name"], max_matches)
                all_records.extend(records)
                actor.log.info(f"{sport['name']}: {len(records)} records")

                # Click "Live" tab if available and requested
                if include_live and records:
                    try:
                        live_btn = await page.query_selector('text="Live", [class*="live"], [data-testid*="live"]')
                        if live_btn:
                            await live_btn.click()
                            await asyncio.sleep(3)
                            live_records = await scrape_stake_page(page, page.url, sport["name"], max_matches)
                            all_records.extend(live_records)
                            actor.log.info(f"{sport['name']} live: {len(live_records)} records")
                    except Exception:
                        pass

                if len(all_records) >= max_matches * len(STAKE_ESPORTS):
                    break

            await browser.close()

            # Push results
            actor.log.info(f"Total records: {len(all_records)}")
            for rec in all_records:
                await actor.push_data(rec)

            # Push metadata
            await actor.push_data({
                "_meta": True,
                "bookmaker": "stake",
                "records_total": len(all_records),
                "sports_scraped": [s["name"] for s in STAKE_ESPORTS],
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })


if __name__ == "__main__":
    asyncio.run(main())
