import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from apify import Actor
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-scraper")

STAKE_ESPORTS = [
    {"slug": "esports", "name": "Esports"},
    {"slug": "dota-2", "name": "Dota 2"},
    {"slug": "counter-strike", "name": "CS2"},
    {"slug": "league-of-legends", "name": "League of Legends"},
    {"slug": "valorant", "name": "Valorant"},
    {"slug": "mobile-legends", "name": "Mobile Legends"},
    {"slug": "king-of-glory", "name": "King of Glory"},
    {"slug": "rocket-league", "name": "Rocket League"},
    {"slug": "overwatch", "name": "Overwatch"},
    {"slug": "fifa", "name": "FIFA"},
]


async def scrape_page(page, url: str, sport_name: str) -> List[Dict]:
    records = []
    logger.info(f"Scraping: {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        logger.error(f"Navigation failed for {url}: {e}")
        return records

    await asyncio.sleep(6)

    html = await page.content()
    logger.info(f"Page HTML length: {len(html)}")

    # Save debug snapshot
    await Actor.set_value(f"debug_html_{sport_name}", html, content_type="text/html")
    screenshot = await page.screenshot(full_page=True)
    await Actor.set_value(f"debug_screenshot_{sport_name}", screenshot, content_type="image/png")

    # Extract via JS — look for odds-like numbers near team name text
    extracted = await page.evaluate("""() => {
        const results = [];
        // Find all leaf text nodes containing decimal odds
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const oddsPattern = /^\\d+\\.\\d{2,3}$/;
        let node;
        while (node = walker.nextNode()) {
            const text = node.textContent.trim();
            if (oddsPattern.test(text)) {
                // Walk up to find a container with team info
                let el = node.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!el) break;
                    const inner = el.innerText || '';
                    const lines = inner.split('\\n').map(l => l.trim()).filter(l => l.length > 1);
                    // Look for containers with 2+ non-odds text lines (team names)
                    const teamLines = lines.filter(l => !oddsPattern.test(l) && l.length > 2 && l.length < 50);
                    const oddsLines = lines.filter(l => oddsPattern.test(l));
                    if (teamLines.length >= 2 && oddsLines.length >= 2) {
                        results.push({
                            teams: teamLines.slice(0, 4),
                            odds: oddsLines.slice(0, 3),
                            raw: inner.substring(0, 200)
                        });
                        break;
                    }
                    el = el.parentElement;
                }
            }
        }
        // Deduplicate by raw text
        const seen = new Set();
        return results.filter(r => {
            if (seen.has(r.raw)) return false;
            seen.add(r.raw);
            return true;
        });
    }""")

    logger.info(f"{sport_name}: extracted {len(extracted)} raw market blocks")

    for item in extracted:
        teams = item.get("teams", [])
        odds = item.get("odds", [])
        if len(teams) < 2:
            continue
        records.append({
            "bookmaker": "stake",
            "game": sport_name,
            "team1": teams[0],
            "team2": teams[1],
            "price_team1": float(odds[0]) if len(odds) > 0 else None,
            "price_team2": float(odds[1]) if len(odds) > 1 else None,
            "price_draw": float(odds[2]) if len(odds) > 2 else None,
            "match_url": url,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    return records


async def main():
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_matches = input_data.get("maxMatches", 500)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            page = await context.new_page()

            all_records = []

            for sport in STAKE_ESPORTS:
                url = f"https://stake.com/sports/esports/{sport['slug']}" if sport["slug"] != "esports" else "https://stake.com/sports/esports"
                try:
                    records = await scrape_page(page, url, sport["name"])
                    all_records.extend(records)
                    logger.info(f"{sport['name']}: {len(records)} records")
                except Exception as e:
                    logger.error(f"Failed {sport['name']}: {e}")

            await browser.close()

        logger.info(f"Total records: {len(all_records)}")
        for rec in all_records[:max_matches]:
            await actor.push_data(rec)

        await Actor.set_value("summary", {
            "total": len(all_records),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })
