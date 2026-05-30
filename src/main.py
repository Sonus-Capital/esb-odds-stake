import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Dict

from apify import Actor
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stake-scraper")

BASE_URL = "https://stake.com/sports/esports"


async def click_load_more(page, max_clicks: int = 20) -> int:
    """Click 'Load More' button until exhausted or max_clicks reached."""
    clicked = 0
    for _ in range(max_clicks):
        try:
            # Stake uses various button texts — try common ones
            btn = await page.query_selector("button:has-text('Load More'), button:has-text('load more'), button:has-text('Show more'), [data-testid='load-more']")
            if not btn:
                break
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await asyncio.sleep(2)
            clicked += 1
            logger.info(f"Clicked 'Load More' ({clicked})")
        except Exception as e:
            logger.info(f"No more 'Load More' buttons: {e}")
            break
    return clicked


async def extract_markets(page) -> List[Dict]:
    """Extract all match/odds data from the current DOM state."""
    return await page.evaluate("""() => {
        const results = [];
        const oddsPattern = /^\\d+\\.\\d{2,3}$/;

        // Walk all text nodes looking for odds values
        // Then climb to find the enclosing match block
        const allText = Array.from(document.querySelectorAll('*')).filter(el => {
            return el.children.length === 0 &&
                   el.innerText &&
                   oddsPattern.test(el.innerText.trim());
        });

        const processed = new Set();

        for (const oddsEl of allText) {
            // Climb up to find a block with 2+ team names and 2+ odds
            let container = oddsEl.parentElement;
            for (let depth = 0; depth < 10; depth++) {
                if (!container || container.tagName === 'BODY') break;

                const innerText = container.innerText || '';
                const lines = innerText.split('\\n')
                    .map(l => l.trim())
                    .filter(l => l.length > 0);

                const oddsLines = lines.filter(l => oddsPattern.test(l));
                const textLines = lines.filter(l => !oddsPattern.test(l) && l.length > 1 && l.length < 60);

                // A valid match block has at least 2 odds and 2 text lines
                if (oddsLines.length >= 2 && textLines.length >= 2) {
                    const key = innerText.substring(0, 100);
                    if (!processed.has(key)) {
                        processed.add(key);

                        // Try to find game type from class names or nearby badge
                        const gameEl = container.querySelector('[class*="sport"], [class*="game"], [class*="category"]');
                        const game = gameEl ? gameEl.innerText.trim() : '';

                        results.push({
                            team1: textLines[0],
                            team2: textLines[1],
                            odds: oddsLines.slice(0, 3),
                            game: game,
                            extra_text: textLines.slice(0, 6),
                            raw: innerText.substring(0, 300),
                        });
                    }
                    break;
                }
                container = container.parentElement;
            }
        }
        return results;
    }""")


async def amain():
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        max_clicks = input_data.get("maxLoadMoreClicks", 20)
        max_matches = input_data.get("maxMatches", 500)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )

            # Mask automation signals
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            """)

            page = await context.new_page()

            logger.info(f"Navigating to {BASE_URL}")
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.error(f"Navigation failed: {e}")
                await browser.close()
                return

            # Wait for initial SPA hydration
            logger.info("Waiting for SPA hydration...")
            await asyncio.sleep(8)

            # Save initial debug state
            html = await page.content()
            logger.info(f"Initial HTML length: {len(html)}")
            await Actor.set_value("debug_html_initial", html, content_type="text/html")
            screenshot = await page.screenshot(full_page=True)
            await Actor.set_value("debug_screenshot_initial", screenshot, content_type="image/png")

            # Click load more until exhausted
            clicks = await click_load_more(page, max_clicks)
            logger.info(f"Clicked load more {clicks} times")

            # Final debug snapshot after loading all
            html_final = await page.content()
            logger.info(f"Final HTML length: {len(html_final)}")
            await Actor.set_value("debug_html_final", html_final, content_type="text/html")
            screenshot_final = await page.screenshot(full_page=True)
            await Actor.set_value("debug_screenshot_final", screenshot_final, content_type="image/png")

            # Extract all markets
            raw_markets = await extract_markets(page)
            logger.info(f"Extracted {len(raw_markets)} raw market blocks")

            await browser.close()

        # Build output records
        all_records = []
        for item in raw_markets:
            odds = item.get("odds", [])
            all_records.append({
                "bookmaker": "stake",
                "game": item.get("game", "esports"),
                "team1": item.get("team1", ""),
                "team2": item.get("team2", ""),
                "price_team1": float(odds[0]) if len(odds) > 0 else None,
                "price_team2": float(odds[1]) if len(odds) > 1 else None,
                "price_draw": float(odds[2]) if len(odds) > 2 else None,
                "extra_text": item.get("extra_text", []),
                "raw": item.get("raw", ""),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        logger.info(f"Total records: {len(all_records)}")
        for rec in all_records[:max_matches]:
            await actor.push_data(rec)

        await Actor.set_value("summary", {
            "total": len(all_records),
            "load_more_clicks": clicks,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })


def main():
    asyncio.run(amain())
