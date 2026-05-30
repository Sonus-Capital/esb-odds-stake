#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper v4
Single-page scrape of /sports/esports with load-more clicks + robust DOM extraction.
"""

import asyncio
import logging
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

BASE_URL = "https://stake.com/sports/esports"

OXYLABS_USER = "customer-sonus_TbxLY-cc-ca-city-edmonton"
OXYLABS_PASS = "gX~dawV=8MzVzA"
OXYLABS_HOST = "pr.oxylabs.io:7777"


async def wait_for_cf(page, timeout: int = 15) -> bool:
    """Wait for Cloudflare challenge to resolve."""
    for i in range(timeout):
        title = await page.title()
        logger.info(f"CF wait {i+1}s | title='{title}'")
        if "Just a moment" not in title and "Cloudflare" not in title:
            logger.info(f"CF resolved at {i+1}s")
            return True
        await asyncio.sleep(1)
    logger.warning("CF did not resolve within timeout")
    return False


async def click_load_more(page, max_clicks: int = 20) -> int:
    clicked = 0
    for _ in range(max_clicks):
        try:
            btn = await page.query_selector(
                "button:has-text('Load More'), button:has-text('load more'), "
                "button:has-text('Show more'), button:has-text('Show More'), "
                "[data-testid='load-more']"
            )
            if not btn:
                break
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await asyncio.sleep(2)
            clicked += 1
            logger.info(f"Clicked 'Load More' ({clicked})")
        except Exception as e:
            logger.info(f"Load More exhausted: {e}")
            break
    return clicked


async def extract_markets(page) -> List[Dict]:
    return await page.evaluate("""() => {
        const results = [];
        const oddsPattern = /^\\d+\\.\\d{2,3}$/;

        const allLeaf = Array.from(document.querySelectorAll('*')).filter(el =>
            el.children.length === 0 &&
            el.innerText &&
            oddsPattern.test(el.innerText.trim())
        );

        const processed = new Set();

        for (const oddsEl of allLeaf) {
            let container = oddsEl.parentElement;
            for (let depth = 0; depth < 10; depth++) {
                if (!container || container.tagName === 'BODY') break;
                const innerText = container.innerText || '';
                const lines = innerText.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                const oddsLines = lines.filter(l => oddsPattern.test(l));
                const textLines = lines.filter(l => !oddsPattern.test(l) && l.length > 1 && l.length < 60);
                if (oddsLines.length >= 2 && textLines.length >= 2) {
                    const key = innerText.substring(0, 100);
                    if (!processed.has(key)) {
                        processed.add(key);
                        results.push({
                            team1: textLines[0],
                            team2: textLines[1],
                            odds: oddsLines.slice(0, 3),
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


async def amain() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_config = input_data.get("proxyConfiguration")
        max_matches = input_data.get("maxMatches", 500)
        max_clicks = input_data.get("maxLoadMoreClicks", 20)
        headless = input_data.get("headless", True)

        actor.log.info(f"Stake scraper v4 | base={BASE_URL} headless={headless}")

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

        # Proxy: use proxyConfiguration from input, default to Apify residential, fallback to Oxylabs
        if not proxy_config:
            proxy_config = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}
            actor.log.info("Proxy: defaulting to Apify residential proxy")

        try:
            proxy = await actor.create_proxy_configuration(proxy_config)
            proxy_url = await proxy.new_url()
            launch_args["proxy"] = {"server": proxy_url}
            actor.log.info(f"Proxy resolved: {proxy_url[:50]}...")
        except Exception as e:
            actor.log.warning(f"Apify proxy failed ({e}), falling back to Oxylabs")
            proxy_url = f"http://{OXYLABS_USER}:{OXYLABS_PASS}@{OXYLABS_HOST}"
            launch_args["proxy"] = {"server": proxy_url}
            actor.log.info("Proxy: Oxylabs residential CA (fallback)")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )

            page = await context.new_page()

            if stealth_async:
                await stealth_async(page)
                actor.log.info("Stealth v2 applied ✓")

            # Warm up on homepage
            try:
                actor.log.info("Warming up with stake.com homepage")
                await page.goto("https://stake.com", wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
            except Exception as e:
                actor.log.warning(f"Warm-up failed: {e}")

            # Navigate to esports hub
            actor.log.info(f"Navigating hub: {BASE_URL}")
            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                actor.log.error(f"Navigation failed: {e}")
                await browser.close()
                return

            # Wait for CF challenge
            await wait_for_cf(page)
            actor.log.info("CF solved — waiting for SPA hydration")
            await asyncio.sleep(8)

            # Debug snapshot
            html = await page.content()
            actor.log.info(f"Initial HTML length: {len(html)}")
            await Actor.set_value("debug_html_initial", html, content_type="text/html")
            screenshot = await page.screenshot(full_page=True)
            await Actor.set_value("debug_screenshot_initial", screenshot, content_type="image/png")

            # Click load more
            clicks = await click_load_more(page, max_clicks)
            actor.log.info(f"Load More clicked {clicks} times")

            # Final snapshot
            html_final = await page.content()
            actor.log.info(f"Final HTML length: {len(html_final)}")
            await Actor.set_value("debug_html_final", html_final, content_type="text/html")
            screenshot_final = await page.screenshot(full_page=True)
            await Actor.set_value("debug_screenshot_final", screenshot_final, content_type="image/png")

            raw_markets = await extract_markets(page)
            actor.log.info(f"DOM: found {len(raw_markets)} market blocks")

            await browser.close()

        all_records = []
        for item in raw_markets:
            odds = item.get("odds", [])
            all_records.append({
                "bookmaker": "stake",
                "game": "esports",
                "team1": item.get("team1", ""),
                "team2": item.get("team2", ""),
                "price_team1": float(odds[0]) if len(odds) > 0 else None,
                "price_team2": float(odds[1]) if len(odds) > 1 else None,
                "price_draw": float(odds[2]) if len(odds) > 2 else None,
                "extra_text": item.get("extra_text", []),
                "raw": item.get("raw", ""),
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        actor.log.info(f"Final unique records: {len(all_records)}")
        for rec in all_records[:max_matches]:
            await actor.push_data(rec)

        await Actor.set_value("summary", {
            "total": len(all_records),
            "load_more_clicks": clicks,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })


def main():
    asyncio.run(amain())
