#!/usr/bin/env python3
"""
Stake.com Esports Odds Scraper — v4.1 (2026-05-30)

Pure GQL HTTP, no browser. Uses Oxylabs residential proxy to bypass CF on cloud IPs.
groups { templates { markets { outcomes { odds } } } } = unauthenticated odds path.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

import aiohttp
from apify import Actor

logging.basicConfig(level=logging.INFO)

GQL_URL = "https://stake.com/_api/graphql"

BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Origin": "https://stake.com",
    "Referer": "https://stake.com/sports/esports",
    "x-language": "en",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Google Chrome";v="135", "Not-A.Brand";v="8"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

ESPORTS_SLUGS = {
    "dota-2", "counter-strike", "league-of-legends", "valorant",
    "mobile-legends", "starcraft-2", "king-of-glory", "overwatch",
    "rocket-league", "rainbow-six", "warcraft-3",
}

MATCH_WINNER_RE = re.compile(
    r"match winner|moneyline|1x2|match result|twoway|threeway",
    re.IGNORECASE,
)

# Proxy rotation list — working residential exits confirmed 2026-05-30
PROXY_LIST = [
    "http://customer-sonus_TbxLY-cc-ca-city-edmonton:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://customer-sonus_TbxLY-cc-gb:gX~dawV=8MzVzA@pr.oxylabs.io:7777",
    "http://numbnuts_9kOSG:~SWmnT7Qe~n7Fi@pr.oxylabs.io:7777",
]

Q_SPORT_LIST = "{ sportList { id name slug type } }"
Q_TOURNAMENTS = "query T($id:String!){ sport(sportId:$id){ tournamentList{ id name } } }"
Q_FIXTURES = """query F($tid:String!){ sportTournament(tournamentId:$tid){ name fixtureList{
  id name slug status
  data{...on SportFixtureDataMatch{startTime competitors{name}}}
  groups{name templates{name markets{id name status outcomes{id name odds active}}}}
}}}"""


async def gql(session: aiohttp.ClientSession, query: str, variables: dict = None, proxy: str = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    kwargs = {"json": payload}
    if proxy:
        kwargs["proxy"] = proxy
    async with session.post(GQL_URL, **kwargs) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"HTTP {resp.status}: {text[:200]}")
        data = await resp.json()
        if "errors" in data:
            raise Exception(f"GQL errors: {data['errors']}")
        return data.get("data", {})


def pick_proxy(idx: int) -> str:
    return PROXY_LIST[idx % len(PROXY_LIST)]


def extract_match_winner(fixture: dict, t_name: str, sport_slug: str, now: str) -> Optional[Dict]:
    name = fixture.get("name", "")
    slug = fixture.get("slug", "")
    fdata = fixture.get("data") or {}
    comps = [c.get("name", "") for c in fdata.get("competitors", [])]
    if not comps:
        parts = re.split(r"\s+(?:vs\.?|-)\s+", name, maxsplit=1)
        comps = [p.strip() for p in parts] if len(parts) == 2 else []
    if len(comps) < 2:
        return None
    team1, team2 = comps[0], comps[1]
    start_time = fdata.get("startTime", "")
    match_url = f"https://stake.com/sports/esports/{slug}" if slug else ""

    best_market = None
    best_score = -1
    for group in fixture.get("groups", []):
        gn = (group.get("name") or "").lower()
        gp = 3 if gn == "main" else 2 if gn in ("winner", "threeway") else 1
        for tmpl in group.get("templates", []):
            for mkt in tmpl.get("markets", []):
                if mkt.get("status") != "active":
                    continue
                if not MATCH_WINNER_RE.search(mkt.get("name", "")):
                    continue
                outs = [o for o in mkt.get("outcomes", []) if o.get("active") and o.get("odds")]
                if len(outs) < 2:
                    continue
                score = gp * 10 - len(outs)
                if score > best_score:
                    best_score = score
                    best_market = (mkt["name"], outs)

    if not best_market:
        return None
    mkt_name, outcomes = best_market
    p1 = p2 = p_draw = None
    for o in outcomes:
        on = (o.get("name") or "").lower()
        odds = float(o.get("odds", 0))
        if not (1.01 <= odds <= 500):
            continue
        if team1.lower() in on or on in ("home", "1"):
            p1 = odds
        elif team2.lower() in on or on in ("away", "2"):
            p2 = odds
        elif any(x in on for x in ("draw", "tie")):
            p_draw = odds
    if p1 is None and p2 is None:
        valid = [float(o["odds"]) for o in outcomes if 1.01 <= float(o.get("odds", 0)) <= 500]
        if len(valid) >= 2:
            p1 = valid[0]
            if len(valid) == 3:
                p_draw, p2 = valid[1], valid[2]
            else:
                p2 = valid[1]
    if p1 is None or p2 is None:
        return None
    return {
        "bookmaker": "stake",
        "game_raw": sport_slug,
        "tournament_name": t_name,
        "team1": team1,
        "team2": team2,
        "match_start_time": start_time,
        "match_url": match_url,
        "market_name": mkt_name,
        "price_team1": p1,
        "price_team2": p2,
        "price_draw": p_draw,
        "scraped_at": now,
    }


async def main() -> None:
    async with Actor() as actor:
        inp = await actor.get_input() or {}
        max_matches = inp.get("maxMatches", 300)
        esports_slugs = set(inp.get("esportsSlugs", list(ESPORTS_SLUGS)))

        actor.log.info(f"Stake GQL v4.1 | max={max_matches} | sports={len(esports_slugs)}")
        now = datetime.now(timezone.utc).isoformat()
        records: List[Dict] = []
        seen: set = set()
        proxy_idx = 0

        conn = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=BASE_HEADERS, connector=conn, timeout=timeout) as session:

            # Sports
            proxy = pick_proxy(proxy_idx)
            actor.log.info(f"Fetching sportList via proxy #{proxy_idx % len(PROXY_LIST)}...")
            sport_data = await gql(session, Q_SPORT_LIST, proxy=proxy)
            esports = [s for s in sport_data.get("sportList", []) if s.get("slug") in esports_slugs]
            actor.log.info(f"Esports matched: {[s['slug'] for s in esports]}")

            for sport in esports:
                proxy_idx += 1
                proxy = pick_proxy(proxy_idx)
                try:
                    td = await gql(session, Q_TOURNAMENTS, {"id": sport["id"]}, proxy=proxy)
                    tournaments = (td.get("sport") or {}).get("tournamentList", [])
                except Exception as e:
                    actor.log.warning(f"tournamentList failed for {sport['slug']}: {e}")
                    continue

                actor.log.info(f"{sport['name']}: {len(tournaments)} tournaments")

                for t in tournaments:
                    proxy_idx += 1
                    proxy = pick_proxy(proxy_idx)
                    try:
                        fd = await gql(session, Q_FIXTURES, {"tid": t["id"]}, proxy=proxy)
                        fixtures = (fd.get("sportTournament") or {}).get("fixtureList", [])
                    except Exception as e:
                        actor.log.warning(f"  {t['name']}: {e}")
                        continue

                    t_recs = 0
                    for fx in fixtures:
                        if fx.get("status") not in ("active", "live"):
                            continue
                        rec = extract_match_winner(fx, t["name"], sport["slug"], now)
                        if not rec:
                            continue
                        key = f"{rec['team1'].lower()}||{rec['team2'].lower()}"
                        if key in seen:
                            continue
                        seen.add(key)
                        records.append(rec)
                        t_recs += 1
                        if len(records) >= max_matches:
                            break

                    if t_recs:
                        actor.log.info(f"  {t['name']}: {t_recs} records")
                    if len(records) >= max_matches:
                        break
                    await asyncio.sleep(0.2)

                if len(records) >= max_matches:
                    break
                await asyncio.sleep(0.3)

        actor.log.info(f"Total: {len(records)} records")
        for rec in records:
            await actor.push_data(rec)
        await actor.push_data({
            "_meta": True, "bookmaker": "stake",
            "records_total": len(records),
            "method": "gql_unauthenticated_groups_oxylabs",
            "scraped_at": now,
        })


if __name__ == "__main__":
    asyncio.run(main())
