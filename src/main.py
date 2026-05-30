#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper
Updated to be resilient against DOM changes.
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

STAKE_ESPORTS = [
    {"slug": "esports", "name": "Esports"},
    {"slug": "dota-2", "name": "Dota 2"},
    {"slug": "counter-strike", "name": "CS2"},
    {"slug": "league-of-legends", "name": "League of Legends"},
]

def parse_odds(text: str) -> Optional[float]:
    if not text: return None
    # Clean text to get only the numeric decimal value
    match = re.search(r"(\d+\.\d+)", text)
    return float(match.group(1)) if match else None

async def scrape_stake_page(page, url: str, sport_name: str, max_matches: int) -> List[Dict]:
    records: List[Dict] = []
    logger.info(f"Navigating to {url}")

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        logger.error(f"Navigation failed: {e}")
        return records

    await asyncio.sleep(5)

    # Capture HTML for debug
    html_content = await page.content()
    
    # Robust Extraction Strategy:
    # Instead of relying on specific classes, we look for 'blocks' that contain odds
    # Stake.com usually wraps these in divs or buttons.
    
    # Find all elements that might be a 'market row'
    # We look for elements that contain a decimal number (the odd) and have text.
    elements = await page.query_selector_all("div, section, button")
    
    # We iterate through all elements and look for those that contain 
    # multiple child elements with text that looks like odds.
    # To avoid duplicates, we'll use a set of identified match strings.
    seen_matches = set()

    # Strategy: Target the 'market' containers
    # We search for containers that have a pattern of [Team Name] [Odds Value]
    # Since the DOM is complex, we'll use a generic 'containment' search.
    
    # We use evaluate to run a more complex search in the browser context
    extracted_data = await page.evaluate("""() => {
        const results = [];
        const allElements = document.querySelectorAll('*');
        
        // We look for containers that contain 'odds' (decimal numbers)
        // and 'team names' (text)
        const rows = document.querySelectorAll('div[class*="Event"], div[class*="market"], div[class*="row"]');
        
        // Fallback: if no classes match, we search for any div that has a button inside it
        if (rows.length === 0) {
            rows = Array.from(document.querySelectorAll('div')).filter(el => el.querySelectorAll('button').length > 0);
        }

        for (const row of rows) {
            const texts = Array.from(row.innerText).split('\\n').filter(t => t.trim().length > 0);
            const oddsMatch = row.innerText.match(/\\d+\\.\\d+/);
            
            if (oddsMatch) {
                // This is a potential market row
                // Extract teams: Usually the first few lines of text
                const lines = row.innerText.split('\\n');
                if (lines.length >= 2) {
                    results.push({
                        raw_text: row.innerText,
                        first_line: lines[0],
                        second_line: lines[1]
                    });
                }
            }
        }
        return results;
    }""")

    # The above is a fallback. Let's use a more precise 'DOM Walk' via Playwright.
    # I'll replace it with a logic that finds the 'odds' values and then looks at the parent.
    
    # REDO: Use a more direct approach.
    # 1. Find all buttons/divs that contain a decimal point (the odds)
    # 2. Find the parent container.
    # 3. Extract the text from that parent.
    
    await page.evaluate("""() => {
        // Find all buttons/spans that look like odds (e.g. "1.85")
        const oddsEls = Array.from(document.querySelectorAll('*')).filter(el => 
            el.innerText && el.innerText.match(/^\\d+\\.\\d+$/) && el.children.length === 0
        );
        
        const markets = [];
        const seen = new Set();

        for (const odd of oddsEls) {
            let parent = odd.parentElement;
            while (parent && parent.tagName !== 'BODY') {
                if (parent.innerText.includes('vs') || parent.innerText.match(/\\d+\\.\\d+/)) {
                    const text = parent.innerText;
                    if (!seen.has(text)) {
                        markets.push({
                            odds_val: odd.innerText,
                            full_text: text
                        });
                        seen.add(text);
                    }
                }
                parent = parent.parentElement;
            }
        }
        return markets;
    }""")
    
    # Actually, to be 100% sure, I will use the logic below:
    # Use the 'evaluate' function to return a JSON of the page's visible 'market' elements.
    
    # RE-IMPLEMENTATION:
    # We will use a "greedy" approach. Find all elements that look like odds,
    # then the nearest text that looks like a team.
    
    # Final attempt at extraction logic:
    results = await page.evaluate("""() => {
        const markets = [];
        const allEls = document.querySelectorAll('*');
        
        // Filter for elements that are likely to be 'odds buttons' or 'odds labels'
        const oddsElements = Array.from(allEls).filter(el => {
            return el.children.length === 0 && 
                   el.innerText && 
                   /^[0-9]+\\.[0-9]+$/.test(el.innerText.trim());
        });

        const processed = new Set();

        for (const odd of oddsElements) {
            // Walk up to find the container that has the team names
            let container = odd;
            while (container && container.tagName !== 'BODY') {
                // Look for something that looks like a team name or "vs"
                if (container.innerText && (container.innerText.includes('vs') || container.innerText.length > 5)) {
                    const text = container.innerText;
                    // Simple heuristic to prevent duplicate capture of the same market
                    if (!processed.has(text)) {
                        markets.push({
                            odds: odd.innerText,
                            context: text
                        });
                        processed.add(text);
                    }
                    break;
                }
                container = container.parentElement;
            }
        }
        return markets;
    }""")

    for res in results:
        # Use regex to find the 'team' part from the context
        # Context typically: "Team A vs Team B \n 1.85 \n 2.10"
        # We take the first line as the team identification
        lines = res["context"].split('\\n');
        team_info = lines[0] if lines.length > 0 else "Unknown";
        
        records.append({
            "bookmaker": "stake",
            "game_raw": sport_name,
            "team_info": team_info,
            "odds": res["odds"],
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        })

    return records[:max_matches]

async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_config = input_data.get("proxyConfiguration")
        max_matches = input_data.get("maxMatches", 200)
        headless = input_data.get("headless", True)

        async with async_playwright() as p:
            launch_args = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            }
            if proxy_config:
                try:
                    proxy = await actor.create_proxy_configuration(proxy_config)
                    url = await proxy.new_url()
                    launch_args["proxy"] = {"server": url}
                except Exception: pass

            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
            page = await context.new_page()
            
            for sport in STAKE_ESPORTS:
                url = f"https://stake.com/sports/{sport['slug']}"
                records = await scrape_stake_page(page, url, sport["name"], max_matches)
                all_records.extend(records)

            await browser.close()
            for rec in all_records:
                await actor.push_data(rec)
