#!/usr/bin/env python3
"""Apify Actor: Stake.com Esports Odds Scraper v6

CF bypass strategy:
- Use Apify's built-in ProxyConfiguration (RESIDENTIAL group) — rotates IPs automatically
- Oxylabs sonus_TbxLY kept as fallback via input proxyUrl
- Headless=True on Apify (xvfb provides virtual display so it's actually rendered)
- CF Turnstile with residential IP typically auto-solves within 5-15s
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

# Oxylabs fallback — used only if proxyUrl passed via input
OXYLABS_PROXY = {
    "server": "http://pr.oxylabs.io:7777",
    "username": "customer-sonus_TbxLY-cc-ca-city-edmonton",
    "password": "gX~dawV=8MzVzA",
}

STAKE_BASE = "https://stake.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

ESPORTS_GAME_SLUGS = [
    "dota-2", "counter-strike", "league-of-legends", "valorant",
    "mobile-legends", "starcraft-2", "overwatch", "rocket-league", "fifa",
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


async def wait_for_spa(page, timeout_s: int = 25) -> bool:
    """
    Poll until body has real content (SPA hydrated).
    Returns True when body.innerText > 500 chars with a real title.
    Compatible with wait_until='commit' navigation.
    """
    for i in range(timeout_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            body_len = await page.evaluate(
                "() => document.body ? document.body.innerText.length : 0"
            )
            logger.info(f"  SPA wait {i+1}s | title={title!r} | body={body_len}c")
            if body_len > 500 and title and title != "Just a moment...":
                return True
        except Exception:
            pass
    return False


async def handle_cf_challenge(page, max_wait_s: int = 90) -> bool:
    """Wait for CF to resolve. Requires non-empty title — empty title = blank page, not CF pass."""
    for i in range(max_wait_s):
        await asyncio.sleep(1)
        try:
            title = await page.title()
            cur_url = page.url
        except Exception:
            continue
        if i % 10 == 0 or i < 3:
            logger.info(f"CF wait {i+1}s | title={title!r}")
        # Must have a real title — empty string means page hasn't loaded yet
        if (
            title                                          # non-empty
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

    def parse_fixture(fixture: dict, t_name: str) -> List[Dict]:
        recs = []
        if not isinstance(fixture, dict):
            return recs
        name = fixture.get("name", "")
        start_time = fixture.get("startTime", "")
        slug = fixture.get("slug", "")
        match_url = f"{STAKE_BASE}/sports/esports/{slug}" if slug else ""
        vs = re.split(r"\s+(?:vs\.?|VS\.?|–|-)\s+", name, maxsplit=1)
        team1 = vs[0].strip() if len(vs) == 2 else ""
        team2 = vs[1].strip() if len(vs) == 2 else ""
        target = {"match winner", "match result", "winner", "1x2", "moneyline"}
        for market in fixture.get("markets", []):
            if not isinstance(market, dict):
                continue
            if (market.get("name") or "").lower() not in target:
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
                    "bookmaker": "stake", "game_raw": "", "tournament_name": t_name,
                    "team1": team1, "team2": team2, "match_start_time": start_time,
                    "match_url": match_url, "price_team1": p1, "price_team2": p2,
                    "price_draw": p_draw, "scraped_at": now,
                })
        return recs

    t = data.get("sportTournament")
    if t:
        for fx in t.get("fixtureList", []):
            records.extend(parse_fixture(fx, t.get("name", "")))
    for key in ("fixtureList", "fixtures", "events"):
        if key in data and isinstance(data[key], list):
            for fx in data[key]:
                records.extend(parse_fixture(fx, ""))
    return records


def parse_card_lines(lines: list) -> Optional[Dict]:
    """
    Parse Stake card text lines into structured odds record.
    Observed structure:
      line0: game name
      line1: status (Live / countdown / date)
      [optional] map: "5th Map"
      tournament name
      team1
      team2
      [score digits if live]
      "Match Winner - Twoway/Threeway"
      decimal odds (2 or 3 values)
    """
    odds_values = []
    for line in lines:
        if re.match(r"^(\d{1,3}\.\d{2,3})$", line.strip()):
            v = float(line.strip())
            if 1.01 <= v <= 500:
                odds_values.append(v)

    if len(odds_values) < 2:
        return None

    mw_idx = next(
        (i for i, l in enumerate(lines) if "match winner" in l.lower() or "moneyline" in l.lower()),
        None
    )

    team1 = team2 = tournament = ""

    if mw_idx and mw_idx >= 3:
        candidates = []
        i = mw_idx - 1
        while i >= 2 and len(candidates) < 4:
            line = lines[i].strip()
            if re.match(r"^\d{1,2}$", line):
                i -= 1
                continue
            candidates.insert(0, (i, line))
            i -= 1
        if len(candidates) >= 2:
            team1 = candidates[-2][1]
            team2 = candidates[-1][1]
            t_idx = candidates[-2][0] - 1
            if t_idx >= 2:
                pt = lines[t_idx].strip()
                if not re.match(r"^\d+(st|nd|rd|th)\s+Map", pt, re.I):
                    tournament = pt
        elif len(candidates) == 1:
            team2 = candidates[0][1]
    else:
        # Heuristic: find first odds line, take two non-numeric lines before it
        first_odds = next(
            (i for i, l in enumerate(lines) if re.match(r"^\d{1,3}\.\d{2,3}$", l.strip())),
            None
        )
        if first_odds and first_odds >= 2:
            non_num = [
                (i, l) for i, l in enumerate(lines[:first_odds])
                if not re.match(r"^[\d.]+$", l.strip()) and len(l.strip()) > 2
                and not re.match(r"^\d+(st|nd|rd|th)\s+Map", l.strip(), re.I)
            ]
            if len(non_num) >= 2:
                team1 = non_num[-2][1].strip()
                team2 = non_num[-1][1].strip()

    if not team1 or not team2:
        return None

    has_draw = any("threeway" in l.lower() or "draw" in l.lower() for l in lines)
    if len(odds_values) >= 3 and has_draw:
        p1, p_draw, p2 = odds_values[0], odds_values[1], odds_values[2]
    else:
        p1, p2, p_draw = odds_values[0], odds_values[1], None

    return {
        "game_raw": lines[0].strip() if lines else "",
        "tournament_name": tournament,
        "team1": team1, "team2": team2,
        "status": lines[1].strip() if len(lines) > 1 else "",
        "price_team1": p1, "price_team2": p2, "price_draw": p_draw,
    }


async def scrape_page_dom(page, now: str, page_url: str) -> List[Dict]:
    """Odds-density DOM scrape. No class-name assumptions."""
    records = []
    try:
        cards_data = await page.evaluate("""() => {
            const ODDS_RE = /\\b(\\d{1,3}\\.\\d{2,3})\\b/g;
            function extractOdds(text) {
                const out = []; let m; ODDS_RE.lastIndex = 0;
                while ((m = ODDS_RE.exec(text)) !== null) {
                    const v = parseFloat(m[1]);
                    if (v >= 1.01 && v <= 500) out.push(v);
                }
                return out;
            }
            const results = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('div,article,section,a')) {
                if (el.children.length > 25) continue;
                const text = el.innerText || '';
                if (text.length < 15 || text.length > 1000) continue;
                const odds = extractOdds(text);
                if (odds.length < 2 || odds.length > 8) continue;
                const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 2);
                const nonNum = lines.filter(l => !/^[\\d.]+$/.test(l) && !/^\\d{1,2}$/.test(l));
                if (nonNum.length < 2) continue;
                const key = text.substring(0, 80);
                if (seen.has(key)) continue;
                // Prefer smallest container — skip if a subset is already captured
                let skip = false;
                for (const r of results) {
                    if (r.text.length < text.length && text.includes(r.text.substring(0, 60))) {
                        skip = true; break;
                    }
                }
                if (skip) continue;
                seen.add(key);
                const a = el.tagName === 'A' ? el : el.querySelector('a[href*="/sports/"]');
                results.push({
                    text: text.substring(0, 700),
                    href: a ? a.href : '',
                    odds
                });
            }
            return results.slice(0, 150);
        }""")

        logger.info(f"DOM: {len(cards_data)} candidates on {page_url}")
        for card in cards_data:
            lines = [l.strip() for l in card["text"].split("\n") if l.strip()]
            parsed = parse_card_lines(lines)
            if not parsed:
                logger.debug(f"  unparsed: {card['text'][:100]!r}")
                continue
            records.append({
                "bookmaker": "stake",
                "game_raw": parsed["game_raw"],
                "tournament_name": parsed["tournament_name"],
                "team1": parsed["team1"],
                "team2": parsed["team2"],
                "match_start_time": parsed["status"],
                "match_url": card["href"] or page_url,
                "price_team1": parsed["price_team1"],
                "price_team2": parsed["price_team2"],
                "price_draw": parsed["price_draw"],
                "scraped_at": now,
            })
    except Exception as e:
        logger.error(f"DOM scrape error: {e}")
    return records


async def navigate_and_wait(page, url: str, label: str) -> bool:
    """
    Navigate with wait_until='commit' (fast, works through proxy),
    then poll until SPA content appears.
    Returns True if content loaded.
    """
    try:
        await page.goto(url, wait_until="commit", timeout=45000)
    except PWTimeout:
        logger.warning(f"{label}: commit timeout")
        return False
    except Exception as e:
        logger.warning(f"{label}: nav error: {e}")
        return False

    ok = await wait_for_spa(page, timeout_s=20)
    if not ok:
        logger.warning(f"{label}: SPA never hydrated — saving debug HTML")
        try:
            html = await page.content()
            key = re.sub(r"[^a-z0-9]", "_", label.lower())[:40]
            await Actor.set_value(f"debug_blank_{key}", html, content_type="text/html")
        except Exception:
            pass
    return ok


async def main() -> None:
    async with Actor() as actor:
        input_data = await actor.get_input() or {}
        proxy_url = input_data.get("proxyUrl") or os.environ.get("OXYLABS_PROXY")
        stake_base = input_data.get("stakeBaseUrl") or STAKE_BASE
        max_matches = input_data.get("maxMatches", 200)
        headless = input_data.get("headless", True)

        actor.log.info(
            f"Stake scraper v6 | headless={headless} "
            f"stealth={'v2' if STEALTH_AVAILABLE else 'MISSING'}"
        )

        now = datetime.now(timezone.utc).isoformat()
        intercepted: List[Dict] = []
        seen_keys: set = set()

        # Use Apify residential proxy group — rotates IPs, much better CF bypass
        # than a fixed Oxylabs endpoint which CF fingerprints after repeated hits
        proxy_config = await actor.create_proxy_configuration(
            groups=["RESIDENTIAL"],
            country_code="US",
        )
        proxy_url_str = proxy_config.new_url() if proxy_config else None
        actor.log.info(f"Apify proxy URL: {proxy_url_str[:40] if proxy_url_str else 'None'}...")

        launch_args = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-dev-shm-usage", "--disable-gpu",
                "--window-size=1920,1080", "--disable-automation",
            ]
        }

        # Proxy priority: Apify residential > input override > no proxy
        if proxy_url_str:
            launch_args["proxy"] = {"server": proxy_url_str}
            actor.log.info("Proxy: Apify RESIDENTIAL")
        elif proxy_url:
            launch_args["proxy"] = proxy_url if isinstance(proxy_url, dict) else {"server": proxy_url}
            actor.log.info("Proxy: input override")
        else:
            actor.log.info("Proxy: none (direct)")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-CA", timezone_id="America/Edmonton",
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
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )

            # GQL listener attached before any navigation
            async def on_response(response):
                try:
                    if "/_api/graphql" not in response.url or response.status != 200:
                        return
                    body = await response.json()
                    recs = extract_records_from_graphql(body, now)
                    if recs:
                        logger.info(f"GQL: {len(recs)} records intercepted")
                        intercepted.extend(recs)
                    else:
                        try:
                            req = await response.request.post_data_json()
                            op = req.get("operationName") if isinstance(req, dict) else None
                            keys = list((body.get("data") or {}).keys())
                            logger.info(f"GQL op={op} keys={keys}")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug(f"GQL handler: {e}")

            page.on("response", on_response)

            # ── Hub ──────────────────────────────────────────────────────
            hub_url = f"{stake_base}/sports/esports"
            actor.log.info(f"Hub: {hub_url}")

            try:
                await page.goto(hub_url, wait_until="commit", timeout=45000)
            except Exception as e:
                actor.log.error(f"Hub nav failed: {e}")
                await browser.close()
                await actor.push_data({"error": "nav_failed", "message": str(e)})
                return

            # CF check — waits for real non-empty title
            cf_ok = await handle_cf_challenge(page)
            if not cf_ok:
                actor.log.error("CF not solved")
                try:
                    html = await page.content()
                    await Actor.set_value("debug_cf_html", html, content_type="text/html")
                    shot = await page.screenshot(full_page=True, timeout=15000)
                    await Actor.set_value("debug_cf_shot", shot, content_type="image/png")
                except Exception:
                    pass
                await browser.close()
                await actor.push_data({"error": "cf_not_solved"})
                return

            # Now wait for SPA content (separate from CF check)
            spa_ok = await wait_for_spa(page, timeout_s=20)
            actor.log.info(f"SPA hydrated: {spa_ok}")

            # Extra settle for React render
            await asyncio.sleep(3)

            # Debug snapshot
            try:
                shot = await page.screenshot(full_page=True, timeout=15000)
                await Actor.set_value("debug_hub_shot", shot, content_type="image/png")
                html = await page.content()
                await Actor.set_value("debug_hub_html", html, content_type="text/html")
            except Exception:
                pass

            all_dom: List[Dict] = []

            hub_recs = await scrape_page_dom(page, now, hub_url)
            actor.log.info(f"Hub: {len(hub_recs)} records")
            all_dom.extend(hub_recs)

            # ── Game pages ───────────────────────────────────────────────
            for slug in ESPORTS_GAME_SLUGS:
                game_url = f"{stake_base}/sports/esports/{slug}"
                actor.log.info(f"→ {slug}")
                ok = await navigate_and_wait(page, game_url, slug)
                if ok:
                    await asyncio.sleep(2)
                    recs = await scrape_page_dom(page, now, game_url)
                    actor.log.info(f"  {slug}: {len(recs)} records")
                    all_dom.extend(recs)

            actor.log.info(f"GQL={len(intercepted)} DOM={len(all_dom)}")

            final: List[Dict] = []
            for rec in (intercepted + all_dom):
                t1 = (rec.get("team1") or "").strip().lower()
                t2 = (rec.get("team2") or "").strip().lower()
                if not t1 or not t2:
                    continue
                key = f"{t1}||{t2}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    final.append(rec)

            final = final[:max_matches]
            actor.log.info(f"Final: {len(final)} unique records")
            await browser.close()

        for rec in final:
            await actor.push_data(rec)

        await actor.push_data({
            "_meta": True, "bookmaker": "stake",
            "records_total": len(final),
            "gql_records": len(intercepted),
            "dom_records": len(all_dom),
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
