#!/usr/bin/env python3
"""trakt-mal-sync: one-way sync of a Trakt.tv watch history to a MyAnimeList anime list.

Trakt is read only. The script talks to Trakt with a client ID alone and calls nothing but GET,
so it never holds a Trakt OAuth token and cannot change anything there. It needs a public Trakt
profile for that reason.

MAL is written through the official API v2 with an OAuth token from a PKCE login.

Anime are matched through Fribb's anime-lists mapping, which links TMDB show + season (+ episode
offset) to MAL ids. Trakt numbers seasons the TMDB way, so the two line up.

Usage:
  trakt_mal_sync.py login-url              # print the MAL authorize URL
  trakt_mal_sync.py login-code <url|code>  # finish the MAL login
  trakt_mal_sync.py sync                   # dry run: show what would change on MAL
  trakt_mal_sync.py sync --apply           # write the changes to MAL
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import secrets
import sys
import time
import traceback
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

__version__ = "1.0.0"

TRAKT_API = "https://api.trakt.tv"
MAL_API = "https://api.myanimelist.net/v2"
MAL_OAUTH = "https://myanimelist.net/v1/oauth2"
FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
USER_AGENT = f"trakt-mal-sync/{__version__} (+https://github.com/ahbanavi/trakt-mal-sync)"

MAPPING_MAX_AGE = 6 * 86400  # re-download the mapping when older than this
ANIME_INFO_MAX_AGE = 6 * 86400  # re-fetch MAL details of unfinished anime after this
MAL_DELAY = 0.5  # seconds between MAL calls
TYPE_RANK = {"TV": 0, "ONA": 1, "OVA": 2, "SPECIAL": 3, "MOVIE": 4}
MAL_LIST_FIELDS = (
    "list_status{status,score,num_episodes_watched,is_rewatching,start_date,finish_date,updated_at},"
    "num_episodes,status,media_type"
)
TELEGRAM_LIMIT = 4000


class SyncError(Exception):
    pass


# --- config -------------------------------------------------------------------------------------


@dataclass
class Config:
    trakt_client_id: str
    trakt_user: str
    mal_client_id: str
    mal_client_secret: str
    mal_redirect_uri: str
    state_dir: Path
    timezone: ZoneInfo
    overrides_file: Path | None
    telegram_token: str
    telegram_chat: str
    sync_specials: bool
    sync_watchlist: bool
    scores: str  # overwrite | fill | off
    sync_dates: bool
    allow_downgrade: bool
    keep_statuses: set[str]

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ

        def need(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise SyncError(f"{key} is not set")
            return value

        def flag(key: str, default: bool) -> bool:
            value = env.get(key, "").strip().lower()
            return default if not value else value in ("1", "true", "yes", "on")

        if env.get("SCORES", "").strip().lower() not in ("", "overwrite", "fill", "off"):
            raise SyncError("SCORES must be overwrite, fill or off")
        state_dir = Path(
            env.get("STATE_DIR")
            or env.get("STATE_DIRECTORY")  # set by systemd's StateDirectory=
            or Path.home() / ".local/state/trakt-mal-sync"
        )
        overrides = env.get("OVERRIDES_FILE", "").strip()
        return cls(
            trakt_client_id=need("TRAKT_CLIENT_ID"),
            trakt_user=need("TRAKT_USER"),
            mal_client_id=need("MAL_CLIENT_ID"),
            mal_client_secret=env.get("MAL_CLIENT_SECRET", "").strip(),
            mal_redirect_uri=env.get("MAL_REDIRECT_URI", "").strip() or "http://localhost/callback",
            state_dir=state_dir,
            timezone=ZoneInfo(env.get("TIMEZONE", "").strip() or "UTC"),
            overrides_file=Path(overrides) if overrides else state_dir / "overrides.json",
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat=env.get("TELEGRAM_CHAT_ID", "").strip(),
            sync_specials=flag("SYNC_SPECIALS", True),
            sync_watchlist=flag("SYNC_WATCHLIST", True),
            scores=env.get("SCORES", "").strip().lower() or "fill",
            sync_dates=flag("SYNC_DATES", True),
            allow_downgrade=flag("ALLOW_DOWNGRADE", False),
            keep_statuses={
                s.strip()
                for s in (env.get("KEEP_MAL_STATUSES") or "on_hold,dropped").split(",")
                if s.strip()
            },
        )


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set."""
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


def write_json(path: Path, data, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


# --- HTTP ---------------------------------------------------------------------------------------


def http(session: requests.Session, method: str, url: str, retries: int = 5, **kw):
    """One request with retries on network errors, 429 and 5xx. Other statuses are returned."""
    last = ""
    for attempt in range(retries):
        wait = min(60, 2 ** attempt * 2)
        try:
            r = session.request(method, url, timeout=60, **kw)
        except requests.RequestException as e:
            last = str(e)
        else:
            if r.status_code != 429 and r.status_code < 500:
                return r
            last = f"HTTP {r.status_code}"
            retry_after = r.headers.get("Retry-After", "")
            if retry_after.isdigit():
                wait = max(wait, int(retry_after))
        if attempt < retries - 1:
            time.sleep(wait)
    raise SyncError(f"{method} {url.split('?')[0]} failed: {last}")


class Trakt:
    """Read-only Trakt client: a client ID and GET requests, nothing else."""

    def __init__(self, client_id: str, user: str):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Content-Type": "application/json",
                "trakt-api-version": "2",
                "trakt-api-key": client_id,
                "User-Agent": USER_AGENT,
            }
        )
        self.user = f"users/{urllib.parse.quote(user, safe='')}"

    def get(self, path: str, **params) -> requests.Response:
        r = http(self.s, "GET", f"{TRAKT_API}/{path}", params=params)
        if r.status_code == 401 or r.status_code == 403:
            raise SyncError(f"Trakt {path}: HTTP {r.status_code}; is the profile public?")
        if r.status_code != 200:
            raise SyncError(f"Trakt {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def profile(self) -> dict:
        return self.get(self.user, extended="full").json()

    def show_seasons(self, trakt_id: int) -> set[int]:
        """Season numbers Trakt lists for a show, ignoring empty ones."""
        seasons = self.get(f"shows/{trakt_id}/seasons", extended="full").json()
        return {s["number"] for s in seasons if s.get("episode_count")}

    def pages(self, path: str, **params) -> list:
        """All pages of a list under the user, e.g. pages("history/episodes")."""
        items, page = [], 1
        while True:
            r = self.get(f"{self.user}/{path}", page=page, limit=250, **params)
            items.extend(r.json())
            if page >= int(r.headers.get("X-Pagination-Page-Count") or 1):
                return items
            page += 1


class Mal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.token_file = cfg.state_dir / "mal_token.json"
        self.last_call = 0.0

    # OAuth (PKCE with the "plain" method, the only one MAL supports)

    def login_url(self) -> str:
        verifier = secrets.token_urlsafe(96)[:128]
        state = secrets.token_urlsafe(16)
        write_json(self.cfg.state_dir / "mal_pkce.json", {"verifier": verifier, "state": state})
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.cfg.mal_client_id,
                "code_challenge": verifier,
                "code_challenge_method": "plain",
                "redirect_uri": self.cfg.mal_redirect_uri,
                "state": state,
            }
        )
        return f"{MAL_OAUTH}/authorize?{query}"

    def login_code(self, code_or_url: str) -> None:
        pkce = read_json(self.cfg.state_dir / "mal_pkce.json")
        if not pkce:
            raise SyncError("no pending login; run login-url first")
        code = code_or_url.strip()
        if "?" in code:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(code).query)
            if query.get("state", [pkce["state"]])[0] != pkce["state"]:
                raise SyncError("state mismatch; run login-url again")
            code = query["code"][0]
        self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": pkce["verifier"],
                "redirect_uri": self.cfg.mal_redirect_uri,
            }
        )
        (self.cfg.state_dir / "mal_pkce.json").unlink()

    def refresh(self) -> None:
        token = read_json(self.token_file)
        if not token:
            raise SyncError("not logged in to MAL; run login-url and login-code")
        self._token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})

    def _token_request(self, data: dict) -> None:
        data["client_id"] = self.cfg.mal_client_id
        if self.cfg.mal_client_secret:
            data["client_secret"] = self.cfg.mal_client_secret
        r = http(self.s, "POST", f"{MAL_OAUTH}/token", data=data)
        if r.status_code != 200:
            raise SyncError(f"MAL token request failed: HTTP {r.status_code} {r.text[:300]}")
        body = r.json()
        write_json(
            self.token_file,
            {
                "access_token": body["access_token"],
                "refresh_token": body["refresh_token"],
                "expires_at": int(time.time()) + int(body.get("expires_in", 0)),
            },
        )

    # API

    def _call(self, method: str, url: str, **kw) -> requests.Response:
        token = read_json(self.token_file)
        if not token:
            raise SyncError("not logged in to MAL; run login-url and login-code")
        pause = MAL_DELAY - (time.monotonic() - self.last_call)
        if pause > 0:
            time.sleep(pause)
        self.last_call = time.monotonic()
        headers = {"Authorization": f"Bearer {token['access_token']}"}
        return http(self.s, method, url, headers=headers, **kw)

    def animelist(self) -> dict[int, dict]:
        """The user's whole anime list as {mal_id: node}, node carrying its list_status."""
        out: dict[int, dict] = {}
        url = f"{MAL_API}/users/@me/animelist"
        params = {"fields": MAL_LIST_FIELDS, "limit": 1000, "nsfw": "true"}
        while url:
            r = self._call("GET", url, params=params)
            if r.status_code != 200:
                raise SyncError(f"MAL animelist: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            for item in body["data"]:
                node = item["node"]
                node["list_status"] = item.get("list_status") or node.get("list_status")
                out[node["id"]] = node
            url, params = body.get("paging", {}).get("next"), None
        return out

    def anime(self, mal_id: int) -> dict | None:
        r = self._call(
            "GET", f"{MAL_API}/anime/{mal_id}", params={"fields": "num_episodes,status,media_type"}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise SyncError(f"MAL anime {mal_id}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def update(self, mal_id: int, fields: dict) -> None:
        r = self._call("PATCH", f"{MAL_API}/anime/{mal_id}/my_list_status", data=fields)
        if r.status_code != 200:
            raise SyncError(f"HTTP {r.status_code} {r.text[:200]}")


# --- mapping ------------------------------------------------------------------------------------


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


@dataclass
class Mapping:
    # (tmdb show, tmdb season) -> [(episode offset, mal id, type)]
    tv: dict[tuple[int, int], list[tuple[int, int, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    movies: dict[int, int] = field(default_factory=dict)  # tmdb movie -> mal id
    shows: set[int] = field(default_factory=set)  # tmdb shows known to be anime
    seasons: dict[int, list[int]] = field(default_factory=dict)  # tmdb show -> mapped seasons
    skip: set[int] = field(default_factory=set)  # mal ids never touched


def load_mapping(cfg: Config, log) -> Mapping:
    path = cfg.state_dir / "anime-list-full.json"
    fresh = path.exists() and time.time() - path.stat().st_mtime < MAPPING_MAX_AGE
    if not fresh:
        try:
            r = requests.get(FRIBB_URL, timeout=120, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except (requests.RequestException, ValueError) as e:
            if not path.exists():
                raise SyncError(f"cannot download the anime mapping: {e}")
            log(f"warning: mapping download failed ({e}); using the cached copy")
    m = Mapping()
    multi_movie: dict[int, set[int]] = defaultdict(set)
    for e in json.loads(path.read_text()):
        mal_id = e.get("mal_id")
        tmdb = e.get("themoviedb_id")
        if not mal_id or not isinstance(tmdb, dict):
            continue
        season = (e.get("season") or {}).get("tmdb")
        offset = (e.get("episode_offset") or {}).get("tmdb") or 0
        for show in as_list(tmdb.get("tv")):
            m.shows.add(show)
            if isinstance(season, int):
                m.tv[(show, season)].append((offset, mal_id, e.get("type") or ""))
        for movie in as_list(tmdb.get("movie")):
            multi_movie[movie].add(mal_id)
    # a TMDB movie that maps to several MAL entries is ambiguous, so it is left out
    m.movies = {k: next(iter(v)) for k, v in multi_movie.items() if len(v) == 1}

    overrides = read_json(cfg.overrides_file, {}) if cfg.overrides_file else {}
    replaced = set()
    for o in overrides.get("tv", []):
        key = (int(o["tmdb"]), int(o["season"]))
        if key not in replaced:
            m.tv[key] = []
            replaced.add(key)
        m.tv[key].append((int(o.get("offset", 0)), int(o["mal_id"]), "OVERRIDE"))
        m.shows.add(key[0])
    for o in overrides.get("movies", []):
        m.movies[int(o["tmdb"])] = int(o["mal_id"])
    m.skip = {int(x) for x in overrides.get("skip_mal", [])}
    for show, season in m.tv:
        m.seasons.setdefault(show, []).append(season)
    return m


def watchlist_keys(d: TraktData, m: Mapping) -> list[tuple[int, int]]:
    """TMDB seasons on the Trakt watchlist; a whole show stands for all its regular seasons."""
    keys = [(t, s) for t in d.watchlist_shows for s in sorted(m.seasons.get(t, [])) if s > 0]
    return keys + sorted(d.watchlist_seasons)


def segments(m: Mapping, key: tuple[int, int]) -> tuple[list[tuple[int, int]], list[str]]:
    """Resolve a TMDB season to [(offset, mal_id)] sorted by offset, plus ambiguity notes.

    Several MAL entries at one offset mean the mapping does not know which episodes each one
    covers. On a regular season the most series-like type wins (TV > ONA > OVA > ...); a tie, or
    any clash in season 0 (specials), drops that offset.
    """
    by_offset: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for offset, mal_id, typ in m.tv.get(key, []):
        if mal_id not in m.skip:
            by_offset[offset].append((mal_id, typ))
    out, notes = [], []
    for offset in sorted(by_offset):
        cands = sorted(set(by_offset[offset]), key=lambda c: TYPE_RANK.get(c[1], 9))
        if key[1] == 0:
            if len(cands) == 1 and cands[0][1] != "MOVIE":
                out.append((offset, cands[0][0]))
            continue
        if len(cands) > 1 and TYPE_RANK.get(cands[0][1], 9) == TYPE_RANK.get(cands[1][1], 9):
            notes.append(f"offset {offset}: ambiguous between MAL {[c[0] for c in cands]}")
            continue
        out.append((offset, cands[0][0]))
    return out, notes


# --- Trakt data ---------------------------------------------------------------------------------


@dataclass
class TraktData:
    # (tmdb show, season) -> {episode number: [watched_at, ...]}
    episodes: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    movies: dict = field(default_factory=lambda: defaultdict(list))  # tmdb movie -> [watched_at]
    titles: dict = field(default_factory=dict)  # ("show"|"movie", tmdb) -> title
    show_ratings: dict = field(default_factory=dict)
    season_ratings: dict = field(default_factory=dict)
    movie_ratings: dict = field(default_factory=dict)
    watchlist_shows: set = field(default_factory=set)
    watchlist_seasons: set = field(default_factory=set)
    watchlist_movies: set = field(default_factory=set)
    anime_genre: dict = field(default_factory=dict)  # tmdb show -> title, for "anime" genre
    no_tmdb: list = field(default_factory=list)  # titles of anime-genre shows lacking a TMDB id
    trakt_ids: dict = field(default_factory=dict)  # tmdb show -> trakt show id
    # Plays before this are not real watch dates: "watched on release date" marks and the bulk
    # import done when the account was new. They still count as watched.
    date_cutoff: str = ""


def load_trakt(cfg: Config, t: Trakt, log) -> TraktData:
    d = TraktData()
    joined = t.profile().get("joined_at")
    if joined:
        cutoff = dt.datetime.fromisoformat(joined.replace("Z", "+00:00")) + dt.timedelta(days=1)
        d.date_cutoff = cutoff.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    for h in t.pages("history/episodes"):
        show, ep = h["show"], h["episode"]
        tmdb = show["ids"].get("tmdb")
        if not tmdb:
            continue
        d.episodes[(tmdb, ep["season"])][ep["number"]].append(h["watched_at"])
        d.titles[("show", tmdb)] = show["title"]
        d.trakt_ids[tmdb] = show["ids"]["trakt"]
    for h in t.pages("history/movies"):
        tmdb = h["movie"]["ids"].get("tmdb")
        if tmdb:
            d.movies[tmdb].append(h["watched_at"])
            d.titles[("movie", tmdb)] = h["movie"]["title"]
    for r in t.pages("ratings/shows"):
        if tmdb := r["show"]["ids"].get("tmdb"):
            d.show_ratings[tmdb] = r["rating"]
            d.titles.setdefault(("show", tmdb), r["show"]["title"])
    for r in t.pages("ratings/seasons"):
        if tmdb := r["show"]["ids"].get("tmdb"):
            d.season_ratings[(tmdb, r["season"]["number"])] = r["rating"]
    for r in t.pages("ratings/movies"):
        if tmdb := r["movie"]["ids"].get("tmdb"):
            d.movie_ratings[tmdb] = r["rating"]
            d.titles.setdefault(("movie", tmdb), r["movie"]["title"])
    if cfg.sync_watchlist:
        for w in t.pages("watchlist/shows"):
            if tmdb := w["show"]["ids"].get("tmdb"):
                d.watchlist_shows.add(tmdb)
                d.titles.setdefault(("show", tmdb), w["show"]["title"])
        for w in t.pages("watchlist/seasons"):
            if tmdb := w["show"]["ids"].get("tmdb"):
                d.watchlist_seasons.add((tmdb, w["season"]["number"]))
                d.titles.setdefault(("show", tmdb), w["show"]["title"])
        for w in t.pages("watchlist/movies"):
            if tmdb := w["movie"]["ids"].get("tmdb"):
                d.watchlist_movies.add(tmdb)
                d.titles.setdefault(("movie", tmdb), w["movie"]["title"])
    for w in t.pages("watched/shows", extended="full"):
        show = w["show"]
        if "anime" in (show.get("genres") or []):
            if show["ids"].get("tmdb"):
                d.anime_genre[show["ids"]["tmdb"]] = show["title"]
            else:
                d.no_tmdb.append(show["title"])
    log(
        f"Trakt: {sum(len(v) for v in d.episodes.values())} episodes in {len(d.episodes)} seasons, "
        f"{len(d.movies)} movies, {len(d.show_ratings) + len(d.season_ratings) + len(d.movie_ratings)}"
        f" ratings, {len(d.watchlist_shows) + len(d.watchlist_seasons) + len(d.watchlist_movies)}"
        " watchlist items"
    )
    return d


# --- planning -----------------------------------------------------------------------------------


@dataclass
class Want:
    mal_id: int
    sources: list = field(default_factory=list)
    episodes: dict = field(default_factory=lambda: defaultdict(list))  # MAL ep index -> [ts]
    movie_plays: list = field(default_factory=list)
    score: int | None = None
    score_rank: int = 9  # lower wins: 0 season or movie rating, 1 show rating
    watchlist: bool = False

    def rate(self, score: int | None, rank: int) -> None:
        if score and rank < self.score_rank:
            self.score, self.score_rank = score, rank


@dataclass
class Report:
    changes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # downgrades held back
    unmapped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def to_date(ts: str, tz: ZoneInfo) -> str:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date().isoformat()


def locate(segs: list[tuple[int, int]], number: int, n_eps) -> tuple[int, int] | None:
    """(mal_id, MAL episode index) for a TMDB episode number, or None if no segment covers it."""
    for i, (offset, mal_id) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        if offset < number and (nxt is None or number <= nxt):
            idx = number - offset
            if n_eps(mal_id) and idx > n_eps(mal_id):
                return None
            return mal_id, idx
    return None


def fold_later_seasons(segs: list[tuple[int, int]], pool: list[int], n_eps) -> list[tuple[int, int]]:
    """Chain MAL entries of later TMDB seasons into the gaps after each segment.

    TMDB, and so Trakt, often keeps a show's later cours inside one long season while the mapping
    still files them under season 2, 3, ... The pool's entries are placed, in order, wherever a
    segment ends before the next one starts (or after the last one).
    """
    taken = {mal_id for _, mal_id in segs}
    pool = [p for p in pool if p not in taken]
    out = []
    for i, (offset, mal_id) in enumerate(segs):
        out.append((offset, mal_id))
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        end = offset + n_eps(mal_id) if n_eps(mal_id) else None
        while pool and end is not None and (nxt is None or end < nxt):
            cand = pool.pop(0)
            out.append((end, cand))
            end = end + n_eps(cand) if n_eps(cand) else None
    return out


def collect_wants(
    cfg: Config, d: TraktData, m: Mapping, info: dict, trakt_seasons, report: Report
) -> dict:
    wants: dict[int, Want] = {}

    def want(mal_id: int) -> Want:
        return wants.setdefault(mal_id, Want(mal_id))

    def n_eps(mal_id: int) -> int:
        return (info.get(mal_id) or {}).get("num_episodes") or 0

    for key, eps in sorted(d.episodes.items()):
        tmdb, season = key
        if season == 0 and not cfg.sync_specials:
            continue
        title = d.titles.get(("show", tmdb), str(tmdb))
        segs, notes = segments(m, key)
        for note in notes:
            report.unmapped.append(f"{title} S{season}: {note}")
        if not segs:
            if season > 0 and (tmdb in m.shows or tmdb in d.anime_genre):
                report.unmapped.append(f"{title} S{season}: no MAL mapping (tmdb {tmdb})")
            continue
        later = [s for s in sorted(m.seasons.get(tmdb, [])) if s > season]
        if season > 0 and later and any(locate(segs, n, n_eps) is None for n in eps):
            on_trakt = trakt_seasons(tmdb)
            pool = [
                mal_id
                for s in later
                if s not in on_trakt
                for _, mal_id in segments(m, (tmdb, s))[0]
                if (info.get(mal_id) or {}).get("status") != "not_yet_aired"
            ]
            segs = fold_later_seasons(segs, pool, n_eps)
        uncovered = []
        for number, plays in sorted(eps.items()):
            found = locate(segs, number, n_eps)
            if found is None:
                uncovered.append(number)
                continue
            mal_id, idx = found
            w = want(mal_id)
            w.episodes[idx].extend(plays)
            src = f"{title} S{season}"
            if src not in w.sources:
                w.sources.append(src)
                w.rate(d.season_ratings.get(key), 0)
                if season > 0:
                    w.rate(d.show_ratings.get(tmdb), 1)
        if uncovered and season > 0:
            report.unmapped.append(f"{title} S{season}: episodes {compact(uncovered)} not mapped")

    for tmdb, plays in d.movies.items():
        mal_id = m.movies.get(tmdb)
        if not mal_id or mal_id in m.skip:
            continue
        w = want(mal_id)
        w.movie_plays.extend(plays)
        w.sources.append(d.titles.get(("movie", tmdb), str(tmdb)))
        w.rate(d.movie_ratings.get(tmdb), 0)

    if cfg.sync_watchlist:
        for tmdb, season in watchlist_keys(d, m):
            for _, mal_id in segments(m, (tmdb, season))[0]:
                w = want(mal_id)
                w.watchlist = True
                w.sources.append(f"{d.titles.get(('show', tmdb), tmdb)} S{season} (watchlist)")
        for tmdb in d.watchlist_movies:
            if (mal_id := m.movies.get(tmdb)) and mal_id not in m.skip:
                w = want(mal_id)
                w.watchlist = True
                w.sources.append(f"{d.titles.get(('movie', tmdb), tmdb)} (watchlist)")
    return wants


def compact(numbers: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(set(numbers)):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)


STATUS_RANK = {"plan_to_watch": 0, "watching": 1, "on_hold": 1, "dropped": 1, "completed": 2}


def target(cfg: Config, w: Want, node: dict, cutoff: str) -> dict | None:
    """The list status Trakt implies for one MAL entry, or None when Trakt has nothing to say."""
    total = node.get("num_episodes") or 0
    finished = node.get("status") == "finished_airing"
    first_plays = {idx: min(plays) for idx, plays in w.episodes.items()}
    if w.movie_plays and not first_plays:
        # a MAL entry reached through a Trakt movie: one play covers every episode
        first = min(w.movie_plays)
        first_plays = {i: first for i in range(1, max(total, 1) + 1)}
    first_plays = {i: ts for i, ts in first_plays.items() if not total or i <= total}
    watched = list(first_plays)
    if watched:
        complete = bool(total) and len(watched) >= total and finished
        t = {"status": "completed" if complete else "watching"}
        if complete:
            t["num_watched_episodes"] = total
        else:
            # a skipped episode keeps a finished show short of its last episode, so MAL does not
            # read the entry as complete
            top = max(watched)
            short = total and finished and len(watched) < total
            t["num_watched_episodes"] = min(top, total - 1) if short else top
        # a date is only written when the play behind it is a real one (see date_cutoff)
        start, finish = min(first_plays.values()), max(first_plays.values())
        if start >= cutoff:
            t["start_date"] = to_date(start, cfg.timezone)
        if complete and finish >= cutoff:
            t["finish_date"] = to_date(finish, cfg.timezone)
    elif w.watchlist:
        t = {"status": "plan_to_watch"}
    else:
        return None
    if cfg.scores != "off" and w.score:
        t["score"] = w.score
    return t


def diff(cfg: Config, w: Want, node: dict, cutoff: str, report: Report) -> dict:
    """Fields to send to MAL for one entry; empty when MAL already matches."""
    want = target(cfg, w, node, cutoff)
    if want is None:
        return {}
    cur = node.get("list_status") or {}
    if cur and want["status"] == "plan_to_watch":
        return {}  # a watchlist entry never overrides an entry that already exists on MAL
    out = {}
    status = want["status"]
    if cur.get("status") in cfg.keep_statuses and status == "watching":
        status = cur["status"]  # Trakt's public API has no dropped/on-hold state, keep MAL's
    if status != cur.get("status"):
        out["status"] = status
    eps = want.get("num_watched_episodes")
    if eps is not None and eps != cur.get("num_episodes_watched", 0):
        out["num_watched_episodes"] = eps
    if "score" in want and want["score"] != cur.get("score", 0):
        if cfg.scores == "overwrite" or not cur.get("score"):
            out["score"] = want["score"]
    if cfg.sync_dates:
        for k in ("start_date", "finish_date"):
            if k in want and not cur.get(k):
                out[k] = want[k]
    if cur and not cfg.allow_downgrade:
        lower_status = STATUS_RANK.get(out.get("status", cur["status"]), 0) < STATUS_RANK.get(
            cur["status"], 0
        )
        fewer_eps = out.get("num_watched_episodes", 1 << 30) < cur.get("num_episodes_watched", 0)
        if lower_status or fewer_eps:
            held = {k: out.pop(k) for k in ("status", "num_watched_episodes") if k in out}
            report.skipped.append((w, node, held))
    return out


def describe(node: dict, fields: dict) -> str:
    cur = node.get("list_status") or {}
    names = {
        "status": "status",
        "num_watched_episodes": "eps",
        "score": "score",
        "start_date": "start",
        "finish_date": "finish",
    }
    before_key = {"num_watched_episodes": "num_episodes_watched"}
    parts = []
    for k, v in fields.items():
        before = cur.get(before_key.get(k, k))
        parts.append(f"{names[k]} {before if before not in (None, '', 0) else '-'}→{v}")
    title = node.get("title", "?")
    return f"{title} [{node['id']}]: {'new, ' if not cur else ''}{', '.join(parts)}"


# --- run ----------------------------------------------------------------------------------------


def anime_info(mal: Mal, cfg: Config, ids: set[int], mylist: dict, log) -> dict:
    """num_episodes/status/title per MAL id, from the list itself or a cached details call."""
    cache_file = cfg.state_dir / "mal_anime.json"
    cache = {int(k): v for k, v in (read_json(cache_file, {}) or {}).items()}
    now = int(time.time())
    for mal_id, node in mylist.items():
        cache[mal_id] = {
            k: node.get(k) for k in ("title", "num_episodes", "status", "media_type")
        } | {"fetched": now}
    missing = [
        i
        for i in sorted(ids)
        if i not in cache
        or (
            cache[i].get("status") != "finished_airing"
            and now - cache[i].get("fetched", 0) > ANIME_INFO_MAX_AGE
        )
    ]
    if missing:
        log(f"MAL: fetching details of {len(missing)} anime")
    for mal_id in missing:
        a = mal.anime(mal_id)
        if a is None:
            cache[mal_id] = {"title": None, "missing": True, "fetched": now}
            continue
        cache[mal_id] = {
            k: a.get(k) for k in ("title", "num_episodes", "status", "media_type")
        } | {"fetched": now}
    write_json(cache_file, {str(k): v for k, v in cache.items()}, mode=0o644)
    return cache


def sync(cfg: Config, apply: bool, only: set[int] | None = None) -> int:
    lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    report = Report()
    mal = Mal(cfg)
    mal.refresh()  # every run: keeps the refresh token chain young
    mapping = load_mapping(cfg, log)
    trakt = Trakt(cfg.trakt_client_id, cfg.trakt_user)
    data = load_trakt(cfg, trakt, log)
    mylist = mal.animelist()
    log(f"MAL: {len(mylist)} entries on the list")

    # candidate MAL ids first, so episode counts are known before episodes are assigned
    ids = set()
    for tmdb, season in data.episodes:
        # later seasons too: they may be folded into this one (fold_later_seasons)
        for s in mapping.seasons.get(tmdb, []) if season > 0 else [season]:
            if s >= season:
                ids.update(mal_id for _, mal_id in segments(mapping, (tmdb, s))[0])
    ids.update(mapping.movies[t] for t in data.movies if t in mapping.movies)
    if cfg.sync_watchlist:
        for key in watchlist_keys(data, mapping):
            ids.update(mal_id for _, mal_id in segments(mapping, key)[0])
        ids.update(mapping.movies[t] for t in data.watchlist_movies if t in mapping.movies)
    info = anime_info(mal, cfg, ids, mylist, log)

    seasons_cache: dict[int, set[int]] = {}

    def trakt_seasons(tmdb: int) -> set[int]:
        if tmdb not in seasons_cache:
            seasons_cache[tmdb] = trakt.show_seasons(data.trakt_ids[tmdb])
        return seasons_cache[tmdb]

    wants = collect_wants(cfg, data, mapping, info, trakt_seasons, report)
    planned = []
    for mal_id, w in sorted(wants.items()):
        node = mylist.get(mal_id) or {"id": mal_id, **(info.get(mal_id) or {})}
        node["id"] = mal_id
        if (info.get(mal_id) or {}).get("missing"):
            report.unmapped.append(f"MAL {mal_id} (from {', '.join(w.sources)}) no longer exists")
            continue
        fields = diff(cfg, w, node, data.date_cutoff, report)
        if fields and (not only or mal_id in only):
            planned.append((node, fields))

    unmapped_anime = [
        f"{title}: anime on Trakt with no MAL mapping (tmdb {tmdb})"
        for tmdb, title in sorted(data.anime_genre.items(), key=lambda x: x[1])
        if tmdb not in mapping.shows
    ] + [f"{title}: anime on Trakt without a TMDB id" for title in data.no_tmdb]
    report.unmapped.extend(unmapped_anime)

    log(f"{'Applying' if apply else 'Dry run:'} {len(planned)} changes")
    for node, fields in planned:
        line = describe(node, fields)
        if apply:
            try:
                mal.update(node["id"], fields)
            except SyncError as e:
                report.errors.append(f"{line}: {e}")
                log(f"  ERROR {line}: {e}")
                continue
        report.changes.append(line)
        log(f"  {line}")
    if report.skipped:
        log(f"Held back {len(report.skipped)} downgrades (ALLOW_DOWNGRADE=0):")
        for w, node, held in report.skipped:
            log(f"  {describe(node, held)}  (from {', '.join(w.sources)})")
    if report.unmapped:
        log(f"Not mapped ({len(report.unmapped)}):")
        for u in report.unmapped:
            log(f"  {u}")

    (cfg.state_dir / "last-run.txt").write_text("\n".join(lines) + "\n")
    if apply:
        notify(cfg, report)
    return 1 if report.errors else 0


def notify(cfg: Config, report: Report, error: str = "") -> None:
    if not (cfg.telegram_token and cfg.telegram_chat):
        return
    seen_file = cfg.state_dir / "unmapped-seen.json"
    seen = set(read_json(seen_file, []) or [])
    new_unmapped = [u for u in report.unmapped if u not in seen]
    if not (error or report.changes or report.errors or new_unmapped):
        return
    parts = ["Trakt → MAL sync"]
    if error:
        parts.append(f"FAILED: {error}")
    if report.changes:
        parts.append(f"Updated {len(report.changes)}:\n" + "\n".join(f"• {c}" for c in report.changes))
    if report.errors:
        parts.append(f"Errors {len(report.errors)}:\n" + "\n".join(f"• {e}" for e in report.errors))
    if new_unmapped:
        parts.append(f"Newly unmapped {len(new_unmapped)}:\n" + "\n".join(f"• {u}" for u in new_unmapped))
    text = "\n\n".join(parts)
    if len(text) > TELEGRAM_LIMIT:
        text = text[: TELEGRAM_LIMIT - 20] + "\n… (see last-run.txt)"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        r.raise_for_status()
        write_json(seen_file, sorted(seen | set(report.unmapped)), mode=0o644)
    except requests.RequestException as e:
        print(f"warning: Telegram notify failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="One-way sync of a Trakt.tv history to MyAnimeList.")
    ap.add_argument("--config", help="KEY=VALUE env file to load (process env wins)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login-url", help="print the MAL authorize URL")
    lc = sub.add_parser("login-code", help="finish the MAL login with the redirect URL or code")
    lc.add_argument("code")
    sp = sub.add_parser("sync", help="sync Trakt to MAL (dry run unless --apply)")
    sp.add_argument("--apply", action="store_true", help="write the changes to MAL")
    sp.add_argument("--only", type=int, action="append", metavar="MAL_ID",
                    help="limit changes to this MAL id (repeatable)")
    args = ap.parse_args()

    if args.config:
        load_env_file(args.config)
    cfg = None
    try:
        cfg = Config.from_env()
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = open(cfg.state_dir / ".lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("another run holds the lock")
        mal = Mal(cfg)
        if args.cmd == "login-url":
            print(mal.login_url())
            return 0
        if args.cmd == "login-code":
            mal.login_code(args.code)
            print(f"MAL login saved to {mal.token_file}")
            return 0
        return sync(cfg, args.apply, set(args.only or []))
    except Exception as e:  # an unattended run must still report, whatever broke
        if not isinstance(e, SyncError):
            traceback.print_exc()
        print(f"error: {e}", file=sys.stderr)
        if cfg and args.cmd == "sync" and args.apply:
            notify(cfg, Report(), error=f"{type(e).__name__}: {e}"[:500])
        return 2


if __name__ == "__main__":
    sys.exit(main())
