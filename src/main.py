import asyncio
import json
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
]

async def scrape_stake_page(page, url: str, sport_name: str, max_matches: int) -> List[Dict]:
    records: List[Dict] = []
    logger.info(f"--- STARTING SCRAPE: {url} ({sport_name}) ---")
    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=60000)
        logger.info(f"Page loaded with status {resp.status}")
    except Exception as e:
        logger.error(f"Navigation failed: {e}")
        return records
    await asyncio.sleep(5)
    try:
        html = await page.content()
        logger.info(f"HTML content length: {len(html)}")
        extracted = await page.evaluate("""() => {
            const results = [];
            const allEls = document.querySelectorAll("*");
            for (const el of allEls) {
                if (el.innerText && el.innerText.includes("vs") && el.children.length === 0) {
                    results.push(el.innerText);
                }
            }
            return results;
        }""")
        logger.info(f"Extracted {len(extracted)} potential match strings")
        for s in extracted:
            records.append({"bookmaker": "stake", "game_raw": sport_name, "team_info": s, "odds": "TBD", "scraped_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
    return records[:max_matches]

async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_matches = input_data.get("maxMatches", 200)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            page = await context.new_page()
            all_records = []
            for sport in STAKE_ESPORTS:
                url = f"https://stake.com/sports/{sport['slug']}"
                logger.info(f"Processing {sport['name']}...")
                records = await scrape_stake_page(page, url, sport["name"], max_matches)
                all_records.extend(records)
            await browser.close()
            for rec in all_records:
                await actor.push_data(rec)
            logger.info("Scrape complete. Total records: " + str(len(all_records)))

if __name__ == "__main__":
    asyncio.run(main())
