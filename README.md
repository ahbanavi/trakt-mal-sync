# trakt-mal-sync

One-way sync of a [Trakt.tv](https://trakt.tv) watch history to a
[MyAnimeList](https://myanimelist.net) anime list. Trakt is the source and stays untouched; MAL
follows it. Run it from cron or a systemd timer.

- **Trakt is read only by construction.** The script sends a client ID and GET requests, nothing
  else. It never holds a Trakt OAuth token, so it cannot change your Trakt data even by mistake.
  The flip side: your Trakt profile must be public.
- **MAL is written through the official API v2**, with a token from a one-time PKCE login. The
  token refreshes itself on every run.
- **Dry run by default.** `sync` prints what would change; `sync --apply` writes it.
- Single file, Python 3.9+ and `requests`. No database.

## What it syncs

| Trakt | MAL |
|---|---|
| Watched episodes (full history) | status `watching` / `completed`, episode count |
| Watched movies | status `completed` |
| First and last play | start and finish date, only where MAL has none |
| Ratings (season, then show; movie) | score, only where MAL has none (`SCORES=fill`, the default) |
| Watchlist (shows, seasons, movies) | `plan_to_watch`, only for anime not on your MAL list yet |

Rules that keep it from damaging a list you already maintain:

- It never deletes a MAL entry, and never touches one that Trakt has no data for.
- It never lowers a status or an episode count (`ALLOW_DOWNGRADE=0`). MAL often counts one more
  episode than TMDB, which files it as a special, so lowering would mark finished shows unfinished.
  Held-back changes are listed in the log.
- MAL's `on_hold` and `dropped` are kept when Trakt only says "watching": Trakt's public API has
  no dropped state.
- Plays from before your Trakt account existed (+1 day) count as watched, but give no dates. They
  are "watched on release date" marks and the bulk import of a new account.

## How anime are matched

Trakt numbers seasons the way TMDB does, while MAL gives every season, cour and special its own
entry. Each episode on Trakt is placed on a MAL entry and episode by the first of three sources
that knows it:

1. **[anibridge-mappings](https://github.com/anibridge/anibridge-mappings)**: explicit TMDB episode
   ranges to MAL episode ranges, rebuilt daily. For example, TMDB season 1, episodes 26-38 of
   Re:Zero are episodes 1-13 of MAL's "2nd Season".
2. **Air dates**: the episode belongs to the one MAL entry of the show whose airing window holds
   its air date. Its rank among that season's episodes in the window is its MAL episode number.
   This catches what the mappings miss or get wrong, such as a new cour that TMDB appended to
   season 1, or an OVA released on a given day.
3. **[Fribb/anime-lists](https://github.com/Fribb/anime-lists)**: TMDB season + episode offset. When
   episodes run past a season's last entry and Trakt has no separate later season, the later
   seasons' entries are chained on in order.

The mappings are downloaded once a week and cached. Each run also reads the full episode list of
every anime show you watched, for the air dates.

A finished MAL entry counts as completed when you have watched every Trakt episode that maps to
it, even if MAL counts one or two more. MAL often lists a bonus episode that TMDB files as a
special, as with K-On!: 12 episodes on TMDB, 13 on MAL.

Anything still unresolved is reported as *Not mapped*. Fix those with an overrides file (see
[overrides.example.json](overrides.example.json)):

```json
{
  "tv": [{"tmdb": 97860, "season": 2, "offset": 13, "mal_id": 59989}],
  "movies": [{"tmdb": 128, "mal_id": 164}],
  "skip_mal": [12345]
}
```

`tv` entries replace both mappings for that TMDB season; `offset` is the number of episodes
before the MAL entry starts. `skip_mal` lists MAL ids the sync must never touch.

## Setup

```bash
git clone https://github.com/ahbanavi/trakt-mal-sync && cd trakt-mal-sync
pip install requests   # if your Python lacks it
```

1. **Trakt app.** Create one at <https://trakt.tv/oauth/applications> and copy its client ID.
   Any redirect URI will do; OAuth is never used. Trakt currently asks for VIP to create apps.
   Make your profile public (Settings ▸ Privacy).
2. **MAL client.** Create one at <https://myanimelist.net/apiconfig> with app type *other* (no
   secret) and a redirect URL such as `http://localhost/callback`. Nothing needs to listen there.
3. **Config.** Copy [config.example.env](config.example.env) to a private file (`chmod 600`) and
   fill it in.
4. **Log in to MAL.** The PKCE verifier stays on the machine that runs the sync; only the code from
   the redirect is pasted back:

   ```bash
   ./trakt_mal_sync.py --config sync.env login-url
   # open the URL, allow, copy the http://localhost/callback?code=... URL from the address bar
   ./trakt_mal_sync.py --config sync.env login-code 'http://localhost/callback?code=...&state=...'
   ```

5. **Dry run, read, apply.**

   ```bash
   ./trakt_mal_sync.py --config sync.env sync                 # what would change
   ./trakt_mal_sync.py --config sync.env sync --apply --only 5114   # one entry first
   ./trakt_mal_sync.py --config sync.env sync --apply
   ```

6. **Schedule it.** [systemd/](systemd/) holds a oneshot service and a weekly timer. They expect
   the script in `/opt/trakt-mal-sync/` and the config in `/etc/trakt-mal-sync.env`:

   ```bash
   install -Dm755 trakt_mal_sync.py /opt/trakt-mal-sync/trakt_mal_sync.py
   install -m644 systemd/trakt-mal-sync.{service,timer} /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now trakt-mal-sync.timer
   ```

The MAL refresh token is rotated on every run. If runs stop for over a month it can expire; then
repeat step 4.

## Configuration

| Variable | Default | |
|---|---|---|
| `TRAKT_CLIENT_ID`, `TRAKT_USER` | required | Trakt app client ID and the profile slug |
| `MAL_CLIENT_ID` | required | MAL client ID |
| `MAL_CLIENT_SECRET` | empty | only for MAL clients of type *web* |
| `MAL_REDIRECT_URI` | `http://localhost/callback` | must match the MAL client |
| `STATE_DIR` | `$STATE_DIRECTORY` or `~/.local/state/trakt-mal-sync` | tokens, caches, `last-run.txt` |
| `TIMEZONE` | `UTC` | for the dates written to MAL |
| `OVERRIDES_FILE` | `$STATE_DIR/overrides.json` | mapping fixes |
| `SCORES` | `fill` | `fill`, `overwrite` (Trakt always wins) or `off` |
| `SYNC_SPECIALS`, `SYNC_WATCHLIST`, `SYNC_DATES` | `1` | turn parts off |
| `ALLOW_DOWNGRADE` | `0` | `1` mirrors Trakt even when MAL is ahead |
| `KEEP_MAL_STATUSES` | `on_hold,dropped` | MAL statuses that survive a Trakt "watching" |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | report after an applied run that changed something, failed, or found new unmapped anime |

Process environment variables win over the `--config` file.

## Files in the state directory

| File | |
|---|---|
| `mal_token.json` | MAL access and refresh token (`0600`) |
| `anime-list-full.json`, `anibridge-mappings.json` | cached mappings, refreshed after 6 days |
| `mal_anime.json` | cached MAL episode counts, types, airing status and dates |
| `last-run.txt` | the last run's full log |
| `unmapped-seen.json` | unmapped items already reported, so Telegram only shows new ones |
| `overrides.json` | optional mapping fixes |

## Limitations

- Dropped and on-hold shows on Trakt are not visible without OAuth, so they are not synced.
- Rewatch counts are not synced.
- Specials (season 0) map through anibridge or their release date; movies are matched by their
  TMDB movie id.
- An episode with no air date on TMDB can only be placed by the two mappings.
- A Trakt show without a TMDB id cannot be matched.

## Credits

Anime mappings by [anibridge/anibridge-mappings](https://github.com/anibridge/anibridge-mappings)
(MIT) and [Fribb/anime-lists](https://github.com/Fribb/anime-lists), both built on
[Anime-Lists/anime-lists](https://github.com/Anime-Lists/anime-lists) and
[manami-project/anime-offline-database](https://github.com/manami-project/anime-offline-database).
Not affiliated with Trakt or MyAnimeList.

### Related projects

Other Trakt-to-MAL work looked at while building this. No code was taken from any of them, but
they shaped its design:

- [aniTrakt](https://anitrakt.huere.net/): the original Trakt-to-MAL sync and its Trakt ↔ MAL
  table, now unmaintained. The table lives on in
  [rensetsu/db.trakt.anitrakt](https://github.com/rensetsu/db.trakt.anitrakt) (archived), and
  [rensetsu/BetterAnimeTraktMapper](https://github.com/rensetsu/BetterAnimeTraktMapper) (archived)
  tackled its episode-order gaps.
- [alexborovkov/anime-sync](https://github.com/alexborovkov/anime-sync): a browser-based,
  two-way Trakt ↔ MAL sync. This project went the other way: one-way, unattended, with Trakt
  left read only.
- [s-afrid/trakt-sync-engine](https://github.com/s-afrid/trakt-sync-engine): Trakt to MAL and
  Letterboxd, which also resolves ids through Fribb's mapping.
- [eliasbenb/PlexAniBridge-Mappings](https://github.com/eliasbenb/PlexAniBridge-Mappings)
  (archived): the per-season TMDB episode mappings that led to anibridge-mappings.
- [TheXEM](https://thexem.info/): the scene ↔ TVDB numbering map that Sonarr uses. It was
  considered, but it is keyed on TVDB while Trakt numbers by TMDB.

## License

[MIT](LICENSE)
