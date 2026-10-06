# Short Video Music

Drop royalty-free audio files (`.mp3`, `.wav`, `.ogg`, `.m4a`, `.aac`, `.flac`)
into this directory. `composite_short_video` picks one at random per generated
short. If the directory is empty, the compositor falls back to a synthesized
C-major chord.

## Recommended sources (free for commercial use, no attribution required)

- **Pixabay Music** — https://pixabay.com/music/ (Pixabay license / CC0)
- **Mixkit** — https://mixkit.co/free-stock-music/ (Mixkit License)
- **Uppbeat** — https://uppbeat.io/ (free tier requires account; no attribution
  on paid; check track-level license)
- **Free Music Archive** — https://freemusicarchive.org/ (filter for CC0/CC-BY)

## Recommended track style for healthcare ads

- 30 sec or longer (will be looped/trimmed to 4 s)
- Calm / hopeful / cinematic / ambient
- No vocals
- Avoid harsh transients near 0:00 — the first second plays through a 0.25 s
  fade-in but a clean attack still sounds best

## How a track gets used

1. ffmpeg loads the file with `-stream_loop -1` so anything shorter than 4 s
   gets repeated to fill the clip
2. Volume is reduced to 55%, then a 0.25 s fade-in and 0.4 s fade-out are
   applied so the loop seam is inaudible
3. Output is AAC 128 kbps stereo, AAC-in-MP4

## Picking different music per ad

The selection is `random.choice` per call, so dropping in 3–5 tracks gives
each generated short a different vibe.
