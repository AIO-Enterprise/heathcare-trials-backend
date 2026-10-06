"""
Video Compositor — Ken Burns MP4 from composited still PNG

Pipeline:
  1. PNG bytes in  →  temp file
  2. ffmpeg: zoompan (slow 3% zoom-in) + lavfi ambient chord audio
  3. H.264/AAC MP4 bytes out  →  saved by caller

Audio: ffmpeg lavfi aevalsrc generates a soft C-major open-fifth pad
  (C3 + G3 + E4 + G4) at very low amplitude — no external file needed,
  zero licensing, deterministic.
"""

import logging
import os
import random
import subprocess
import tempfile

log = logging.getLogger(__name__)

_MUSIC_DIR = os.path.join(os.path.dirname(__file__), "music")
_MUSIC_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")


def _pick_music_file() -> str | None:
    """Return absolute path to a random music file in _MUSIC_DIR, or None if empty."""
    try:
        files = [os.path.join(_MUSIC_DIR, f) for f in os.listdir(_MUSIC_DIR) if f.lower().endswith(_MUSIC_EXTS)]
        return random.choice(files) if files else None
    except FileNotFoundError:
        return None


_DURATION = 10  # seconds
_FPS = 25
_FRAMES = _DURATION * _FPS  # 250

# Soft C-major chord: C3=130.81, G3=196.00, E4=329.63, G4=392.00 Hz
# Amplitude kept very low (0.05–0.08) so it sits far below any voiceover.
_AEVALSRC = (
    "aevalsrc="
    "0.08*sin(2*PI*130.81*t)"
    "+0.06*sin(2*PI*196.00*t)"
    "+0.05*sin(2*PI*261.63*t)"
    "+0.04*sin(2*PI*329.63*t)"
    ":s=44100:c=stereo"
)

# Audio filter chain: remove harsh overtones, gentle reverb, fade-out last 1.5s
_AF = f"lowpass=f=550,aecho=0.5:0.3:30:0.25,volume=0.45,afade=t=out:st={_DURATION - 1.5}:d=1.5"

# Ken Burns: linear zoom 1.00 → 1.03 over the full clip, perfectly centred
_VF = (
    f"zoompan=z='1+0.03*(on/{_FRAMES})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={_FRAMES}:s={{w}}x{{h}}:fps={_FPS}"
)


def composite_video(png_bytes: bytes, canvas_w: int, canvas_h: int) -> bytes | None:
    """
    Wrap a composited still PNG as a 10-second Ken Burns MP4 with ambient audio.
    Returns MP4 bytes, or None if ffmpeg is unavailable or fails.
    Runs synchronously — call via asyncio.to_thread from async contexts.
    """
    if not _ffmpeg_available():
        log.warning("ffmpeg not found — video creative skipped")
        return None

    vf = _VF.format(w=canvas_w, h=canvas_h)

    png_tmp = out_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_bytes)
            png_tmp = f.name

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out_tmp = f.name

        cmd = [
            "ffmpeg",
            "-y",
            "-r",
            str(_FPS),
            "-loop",
            "1",
            "-i",
            png_tmp,
            "-f",
            "lavfi",
            "-i",
            _AEVALSRC,
            "-vf",
            vf,
            "-af",
            _AF,
            "-t",
            str(_DURATION),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            "-movflags",
            "+faststart",
            out_tmp,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            log.error("ffmpeg failed (rc=%d): %s", result.returncode, result.stderr.decode()[-600:])
            return None

        with open(out_tmp, "rb") as f:
            return f.read()

    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out generating video")
        return None
    except Exception as exc:
        log.error("video_compositor error: %s", exc)
        return None
    finally:
        for p in (png_tmp, out_tmp):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


_SHORT_DURATION = 4  # seconds — seamless loop, < 5 s
_SHORT_FPS = 25
_SHORT_FRAMES = _SHORT_DURATION * _SHORT_FPS  # 100

_SHORT_VF = (
    f"zoompan="
    f"z='1+0.03*(on/{_SHORT_FRAMES})':"
    f"x='iw/2-(iw/zoom/2)':"
    f"y='ih/2-(ih/zoom/2)':"
    f"d={_SHORT_FRAMES}:"
    f"s={{w}}x{{h}}:"
    f"fps={_SHORT_FPS}"
)

# Soft C-major chord — same as long video, no fade so it loops cleanly.
_SHORT_AEVALSRC = (
    "aevalsrc="
    "0.08*sin(2*PI*130.81*t)"
    "+0.06*sin(2*PI*196.00*t)"
    "+0.05*sin(2*PI*261.63*t)"
    "+0.04*sin(2*PI*329.63*t)"
    ":s=44100:c=stereo"
)
# Equal-power crossfade-friendly: short fade in/out at the seams keeps the loop
# from clicking when the browser restarts the video.
_SHORT_AF = f"lowpass=f=550,aecho=0.5:0.3:30:0.25,volume=0.45,afade=t=in:st=0:d=0.2,afade=t=out:st={_SHORT_DURATION - 0.2}:d=0.2"


def composite_short_video(png_bytes: bytes, canvas_w: int, canvas_h: int) -> bytes | None:
    """
    4-second Ken Burns loop with audio.
    Uses a random royalty-free track from app/services/ai/music/ if any files
    exist there; otherwise falls back to a soft synthesized chord.
    Designed for seamless autoplay looping (TikTok/Reels style).
    """
    if not _ffmpeg_available():
        log.warning("ffmpeg not found — short video skipped")
        return None

    vf = _SHORT_VF.format(w=canvas_w, h=canvas_h)
    music_path = _pick_music_file()

    png_tmp = out_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png_bytes)
            png_tmp = f.name

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out_tmp = f.name

        if music_path:
            log.info("short video using music: %s", os.path.basename(music_path))
            audio_in = ["-stream_loop", "-1", "-i", music_path]
            # Real track: gentle volume, fade in/out for clean loop
            audio_filt = f"volume=0.55,afade=t=in:st=0:d=0.25,afade=t=out:st={_SHORT_DURATION - 0.4}:d=0.4"
        else:
            log.info("no music files in %s — using synthesized chord", _MUSIC_DIR)
            audio_in = ["-f", "lavfi", "-i", _SHORT_AEVALSRC]
            audio_filt = _SHORT_AF

        cmd = [
            "ffmpeg",
            "-y",
            "-r",
            str(_SHORT_FPS),
            "-loop",
            "1",
            "-i",
            png_tmp,
            *audio_in,
            "-vf",
            vf,
            "-af",
            audio_filt,
            "-t",
            str(_SHORT_DURATION),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            out_tmp,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            log.error("ffmpeg short failed (rc=%d): %s", result.returncode, result.stderr.decode()[-600:])
            return None

        with open(out_tmp, "rb") as f:
            return f.read()

    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out generating short video")
        return None
    except Exception as exc:
        log.error("short video_compositor error: %s", exc)
        return None
    finally:
        for p in (png_tmp, out_tmp):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False
