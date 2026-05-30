import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict

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
        # domcontentloaded is enough — networkidle never fires on Stake's SPA
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.error(f"Navigation failed for {url}: {e}")
        return records

    # Wait for JS to hydrate the SPA
    await asyncio.sleep(8)

    html = await page.content()
    logger.info(f"Page HTML length: {len(html)}")

    await Actor.set_value(f"debug_html_{sport_name}", html, content_type="text/html")
    screenshot = await page.screenshot(full_page=True)
    await Actor.set_value(f"debug_screenshot_{sport_name}", screenshot, content_type="image/png")

    extracted = await page.evaluate("""() => {
        const results = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const oddsPattern = /^\\d+\\.\\d{2,3}$/;
        let node;
        while (node = walker.nextNode()) {
            const text = node.textContent.trim();
            if (oddsPattern.test(text)) {
                let el = node.parentElement;
                for (let i = 0; i < 8; i++) {
                    if (!el) break;
                    const inner = el.innerText || '';
                    const lines = inner.split('\\n').map(l => l.trim()).filter(l => l.length > 1);
                    const teamLines = lines.filter(l => !oddsPattern.test(l) && l.length > 2 && l.length < 50);
                    const oddsLines = lines.filter(l => oddsPattern.test(l));
                    if (teamLines.length >= 2 && oddsLines.length >= 2) {
                        results.push({ teams: teamLines.slice(0, 4), odds: oddsLines.slice(0, 3), raw: inner.substring(0, 200) });
                        break;
                    }
                    el = el.parentElement;
                }
            }
        }
        const seen = new Set();
        return results.filter(r => { if (seen.has(r.raw)) return false; seen.add(r.raw); return true; });
    }""")

    logger.info(f"{sport_name}: {len(extracted)} market blocks found")

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


async def amain():
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_matches = input_data.get("maxMatches", 500)
        proxy_config = input_data.get("proxyConfiguration")

        proxy_url = None
        if proxy_config:
            try:
                proxy = await actor.create_proxy_configuration(proxy_config)
                proxy_url = await proxy.new_url()
                logger.info(f"Using proxy: {proxy_url[:30]}...")
            except Exception as e:
                logger.warning(f"Proxy setup failed: {e}")

        # Fallback to Oxylabs if no Apify proxy configured
        if not proxy_url:
            proxy_url = "http://customer-sonus_TbxLY-cc-ca-city-edmonton:gX~dawV=8MzVzA@pr.oxylabs.io:7777"
            logger.info("Using Oxylabs proxy")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                proxy={"server": proxy_url},
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
                url = "https://stake.com/sports/esports" if sport["slug"] == "esports" else f"https://stake.com/sports/esports/{sport['slug']}"
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

        await Actor.set_value("summary", {"total": len(all_records), "scraped_at": datetime.now(timezone.utc).isoformat()})


def main():
    asyncio.run(amain())
