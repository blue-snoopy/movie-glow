# movie-glow

**Free "Ambilight" for Plex + Philips Hue.** Your Hue lightstrip behind the TV follows the colours of the movie you're watching in Plex: no HDMI sync box, no TV app, no capture card.

## How it works

Most ambient-light setups capture the video signal. movie-glow takes a different route: it already has the movie file. Every ~1.5 seconds it:

1. asks Plex (or Home Assistant) **what's playing and where you are** in it,
2. reads **that exact frame from the file** with ffmpeg: a tiny 32×18 image, with HDR10 / Dolby Vision tone-mapped,
3. averages **5 zones** around the edge of the picture (left side, top-left, top, top-right, right side),
4. sends them to a **Hue gradient lightstrip** as 5 colour points, plus brightness from how bright the scene is, with a smooth fade.

Optionally, nearby colour lamps can follow the left or right side of the picture at a lower brightness, with an
**ambient light sensitivity** from 100 (same as the strip) down to 1 (the average colour of roughly the last 30 minutes),
so the room can carry the film's overall mood while the strip handles the action. When you pause or stop, the lights go to a white of your choice, back to how they were, or stay as they are.

A 1080p frame takes ~0.15 s and a 4K HDR frame ~0.5 s on a small Intel NUC.

## Limits

- **Plex only.** It needs the file, so Netflix, Disney+, discs etc. can't work.
- **About a second behind the picture.** Great for mood and scene colour, not for strobe-accurate explosions.
- The machine running movie-glow needs **read access to your media files** and must reach Plex and the Hue bridge.

## Requirements

- Plex Media Server, playing on any client
- A Philips Hue bridge (v2) and a colour light behind the TV. A **gradient** lightstrip (Play Gradient, Gradient Lightstrip) is ideal; any colour light works with a single colour.
- Docker (or Python 3.11+ and ffmpeg)

## Setup

```sh
git clone https://github.com/blue-snoopy/movie-glow.git
cd movie-glow
cp config.example.toml config.toml
cp .env.example .env
cp docker-compose.example.yml docker-compose.yml
docker compose build
```

1. **Hue key:** run `docker compose run --rm movie-glow --pair <bridge-ip>`, press the link button on the bridge, and put the printed `HUE_KEY=…` in `.env`.
2. **Plex token:** put it in `.env` as `PLEX_TOKEN` ([how to find it](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)).
3. **Pick your lights:** `docker compose run --rm movie-glow --lights` lists them; put the strip's name in `config.toml` under `[hue] strip`.
4. **Media path:** mount your media in `docker-compose.yml`, and if Plex sees the files under a different path, add a `path_map` entry.
5. **Test without touching the lights:**
   ```sh
   docker compose run --rm movie-glow --once "/media/movies/Some Movie (2020)/Some Movie.mkv" 1800
   ```
6. **Run it:** `docker compose up -d`, then play something in Plex. Logs: `docker logs -f movie-glow`.

If the colours appear mirrored (the left of the picture shows on the right of the strip), set `reverse = true`.

## Tested with

- Plex Media Server on Linux (Docker), playing on **Apple TV 4K** (Plex app)
- Hue Bridge v2 with **Play Gradient Lightstrip** (5 zones) and Hue colour bulbs as lamps or as a single-colour "strip"
- 1080p SDR and 4K HDR10 HEVC files, on an Intel NUC (1080p ≈ 0.15 s/frame, 4K HDR ≈ 0.5 s/frame)
- With and without Home Assistant. Plex-only timing stayed within ~1 s of the Apple TV's own position.
- `after` modes `fixed` and `restore`, including on `docker stop`

Not yet tested: other Plex clients (should work: movie-glow reads the file, not the client), `--pair`
on a fresh bridge, and low-power hardware like a Raspberry Pi (4K frames may be slow there; it still
works, just with more lag).

## Known limitations

- **Dolby Vision profile 5** files (some streaming-sourced DV) have no HDR10 base layer, so colours come out
  green/purple. Profile 7/8 (Blu-ray, most remuxes) are fine.
- **Plex-only timing** right after a seek can be a few seconds off until Plex reports the new position
  (Home Assistant's `media_player` position fixes this).
- If several sessions match `player`, the first one wins.
- Plex's session list needs a token from the **server owner's** account.

## Home Assistant (optional)

With `[home_assistant]` configured, you get:

- **`switch`**: an `input_boolean` that turns syncing on/off (e.g. from a "movie mode" script or a dashboard button). Without it, movie-glow syncs whenever the chosen Plex player is playing.
- **`media_player`**: a more precise playback position from your player (e.g. an Apple TV), which Plex only updates every few seconds.
- **`lamps_yield_on_pause_to`**: while paused, leave the lamps to another automation (e.g. a "pause lights" scene) when this entity is on.
- **`ambient_sensitivity_entity`**: an `input_number` (1–100) slider for ambient light sensitivity that you can change mid-movie.

## Tuning

All in `config.toml` under `[sync]`: `brightness_min` / `brightness_max`, `saturation`, `interval`, `fade_ms`, `lead`, `ambient_sensitivity`. Lamps take a per-lamp `factor` and `sensitivity`.

**Ambient light sensitivity** (lamps only) is a time-weighted average of each lamp's side of the picture.
It's logarithmic so the whole range is useful:

| sensitivity | lamps settle over |
|---|---|
| 100 | instantly (same as the strip) |
| 90 | ~1 s |
| 75 | ~6 s |
| 50 | ~45 s |
| 25 | ~5 min |
| 10 | ~15 min |
| 1 | ~30 min (the movie's overall mood) |

Brightness is averaged the same way. A new movie starts the average fresh.

## License

MIT
