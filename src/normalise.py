import logging
import re
from rapidfuzz import process, fuzz

from typing import Optional


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CANONICAL_GAMES = [
    "League of Legends", "Dota 2", "CS2", "PUBG", "Valorant",
    "Apex Legends", "Call of Duty", "Fortnite", "Mobile Legends: Bang Bang",
    "Overwatch 2", "Rainbow Six Siege", "Rocket League", "Street Fighter 6", "Tekken 8",
    "StarCraft", "StarCraft II", "Arena of Valor", "Honor of Kings", "King of Glory",
    "CrossFire", "Wild Rift", "Deadlock",
    "FIFA", "FIFA Bots", "NBA2K", "eBasketball Bots", "eCricket", "eFootball", "eTouchdown",
    "Free Fire", "Madden", "Mir Tankov",
 "Teamfight Tactics",
 "Brawl Stars", "Chess", "eSim",
 "Quake Champions", "Quake Live",
 "Age of Empires", "Age of Empires II", "Age of Empires IV",
 "Warcraft 3",
 ]

KNOWN_ALIASES = {
    # Counter-Strike variants
    "Counter-Strike 2": "CS2",
    "Counter-Strike": "CS2",
    "Counter Strike": "CS2",
    "Counter Strike 2": "CS2",
    "CounterStrike": "CS2",
    "CounterStrike 2": "CS2",
    "CS:GO": "CS2",
    "CSGO": "CS2",
    "CS 2": "CS2",
    # MOBA / mobile
    "LoL": "League of Legends",
    "DOTA 2": "Dota 2",
    "MLBB": "Mobile Legends: Bang Bang",
    "Mobile Legends": "Mobile Legends: Bang Bang",
    "AOV": "Arena of Valor",
    "King Of Glory": "Honor of Kings",
    "King of Glory": "Honor of Kings",
    "Wild rift": "Wild Rift",
    # Shooters
    "PUBG: Battlegrounds": "PUBG",
    "VALORANT": "Valorant",
    "COD": "Call of Duty",
    "Warzone": "Call of Duty",
    "Black Ops 7": "Call of Duty",
    "Crossfire": "CrossFire",
    "Overwatch": "Overwatch 2",
    "OW2": "Overwatch 2",
    "Rainbow Six": "Rainbow Six Siege",
    "R6S": "Rainbow Six Siege",
    # Fighting / racing / sports sims
    "RL": "Rocket League",
    "SF6": "Street Fighter 6",
    "Street Fighter": "Street Fighter 6",
    "Tekken": "Tekken 8",
    "T8": "Tekken 8",
    "FIFA": "FIFA",
    "NBA2K": "NBA2K",
    "NBA 2K": "NBA2K",
    "Madden": "Madden",
    "eFootball": "eFootball",
    # RTS / arena / other
    "Starcraft II": "StarCraft II",
    "Starcraft 2": "StarCraft II",
    "StarCraft II": "StarCraft II",
    "WarCraft III": "Warcraft 3",
    "Warcraft III": "Warcraft 3",
    "Warcraft 3": "Warcraft 3",
    "Age Of Empires": "Age of Empires",
    "Age Of Empires II": "Age of Empires II",
    "Age Of Empires IV": "Age of Empires IV",
    "Age of Empires Iv": "Age of Empires IV",
    "Age of Empires II": "Age of Empires II",
    "Quake Champions": "Quake Champions",
    "Quake Live": "Quake Live",
    "Deadlock": "Deadlock",
    # TFT / auto-battler
    "Teamfight Tactics": "Teamfight Tactics",
    "TFT": "Teamfight Tactics",
    # Brawl / sports
    "Brawl Stars": "Brawl Stars",
    "Chess": "Chess",
    "eSim": "eSim",
    "Esports": "Esports",
}

# Tournament-name substrings that indicate a game when the game_raw is missing
# or generic. These are used by infer_game_from_tournament fallback.
TOURNAMENT_GAME_KEYWORDS = {
    "cs2": "CS2",
    "counter-strike": "CS2",
    "blast": "CS2",
    "iem": "CS2",
    "pgl": "CS2",
    "cct": "CS2",
    "nodwin clutch": "CS2",
    "counter strike": "CS2",
    "rainbow six": "Rainbow Six Siege",
    "rainbow 6": "Rainbow Six Siege",
    " r6 ": "Rainbow Six Siege",
    "six invitational": "Rainbow Six Siege",
    "south america league": "Rainbow Six Siege",
    "asia pacific league": "Rainbow Six Siege",
    "north america league": "Rainbow Six Siege",
    "brazil league": "Rainbow Six Siege",
    "latam league": "Rainbow Six Siege",
    "europe mena league": "Rainbow Six Siege",
    "teamfight tactics": "Teamfight Tactics",
    " tft": "Teamfight Tactics",
    "tacticians": "Teamfight Tactics",
    "league of legends": "League of Legends",
    " lol ": "League of Legends",
    "lcs": "League of Legends",
    "lec": "League of Legends",
    "lck": "League of Legends",
    "lpl": "League of Legends",
    "dota 2": "Dota 2",
    "the international": "Dota 2",
    "valorant": "Valorant",
    "vct": "Valorant",
    "champions tour": "Valorant",
    "starcraft": "StarCraft",
    "warcraft 3": "Warcraft 3",
    "warcraft iii": "Warcraft 3",
    "honor of kings": "Honor of Kings",
    "king pro league": "Honor of Kings",
    "mobile legends": "Mobile Legends: Bang Bang",
    "arena of valor": "Arena of Valor",
    "crossfire": "CrossFire",
    "fifa": "FIFA",
    "ea fc": "FIFA",
    "deadlock": "Deadlock",
    "rocket league": "Rocket League",
    "free fire": "Free Fire",
    "brawl stars": "Brawl Stars",
    "chess": "Chess",
    "pubg": "PUBG",
    "call of duty": "Call of Duty",
    "overwatch": "Overwatch 2",
    "mir tankov": "Mir Tankov",
    "nba2k": "NBA2K",
    "madden": "Madden",
    "valhalla league": "Dota 2",
    "lunar horse trophy": "Dota 2",
    "united21": "Dota 2",
    "european pro league": "CS2",
    "bb rush": "CS2",
    "esports world cup": "Esports",
}

# Normalised (game, tournament) -> canonical tournament name. Used to collapse
# dot-separated or abbreviated tournament names from different bookmakers.
TOURNAMENT_ALIASES = {
    "arena of valor": {
        "arena of valor. premier league": "Arena of Valor Premier League 2026",
        "arena of valor premier league": "Arena of Valor Premier League 2026",
        "premier league": "Arena of Valor Premier League 2026",
    },
}


def _strip_game_prefix(tournament: str, game: str) -> str:
    if not tournament or not game:
        return tournament
    patterns = {game.lower()}
    # Include known aliases of this game
    for alias, canonical in KNOWN_ALIASES.items():
        if canonical.lower() == game.lower():
            patterns.add(alias.lower())
    t = tournament.strip()
    for pat in patterns:
        t = re.sub(rf"^{re.escape(pat)}\s*[\.:\-]?\s*", "", t, flags=re.IGNORECASE).strip()
    return t


def infer_game_from_tournament(tournament_name: str) -> Optional[str]:
    """Return canonical game when tournament starts with a known game name/alias."""
    if not tournament_name:
        return None
    t = tournament_name.strip().lower()
    # Prefer longest/canonical matches first
    candidates = [(c, c.lower()) for c in CANONICAL_GAMES]
    aliases = {}
    for alias, canonical in KNOWN_ALIASES.items():
        aliases.setdefault(canonical.lower(), []).append(alias.lower())
    for canonical, canon_lower in sorted(candidates, key=lambda x: -len(x[1])):
        patterns = {canon_lower}
        patterns.update(aliases.get(canon_lower, []))
        for pat in patterns:
            if re.match(rf"^{re.escape(pat)}\s*[\.:\-]?\s*", t):
                return canonical

    # Fallback: scan for known tournament-name keywords anywhere in the string
    for kw, game in TOURNAMENT_GAME_KEYWORDS.items():
        if kw.strip() and kw in t:
            return game
    return None


def normalise_game(game_raw: str) -> Optional[str]:
    """
    Map game_raw to canonical game names using a hardcoded alias dict first,
    then rapidfuzz fallback.
    """
    if not game_raw:
        return None

    # Check direct aliases
    for alias, canonical in KNOWN_ALIASES.items():
        if alias.lower() == game_raw.lower():
            return canonical

    # Check exact canonical names
    for canonical in CANONICAL_GAMES:
        if canonical.lower() == game_raw.lower():
            return canonical

    # Fuzzy match fallback
    result = process.extractOne(game_raw, CANONICAL_GAMES, scorer=fuzz.WRatio)
    if result:
        match, score, _ = result
        if score > 80:
            return match

    logger.warning(f"No match found for game_raw: {game_raw} (confidence <= 80)")
    return None


def canonicalise_tournament(tournament_name: str, game: str) -> str:
    """Collapse tournament-name variants into a single canonical name."""
    if not tournament_name:
        return tournament_name
    game_canon = (game or "").lower()
    t_stripped = _strip_game_prefix(tournament_name, game_canon)
    aliases = TOURNAMENT_ALIASES.get(game_canon, {})
    return aliases.get(t_stripped.lower(), tournament_name.strip())
