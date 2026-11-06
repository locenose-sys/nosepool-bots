#!/usr/bin/env python3
"""
linkgrab - slow, polite, standalone media downloader for your own laptop.

Uses yt-dlp (video/audio) with gallery-dl as a fallback (image posts).
No Telegram, no bot, no hosting. Files go to ./Downloads/<platform>/

Usage:
  python linkgrab.py                    # paste links one by one (blank line to finish)
  python linkgrab.py links.txt          # read links from a text file (one per line)
  python linkgrab.py links.txt -q 720   # skip the quality question
  python linkgrab.py --cookies-dir cookies   # use per-site cookie files (optional)

Quality choices: best, 1080, 720, audio
Cookie files (optional, only if a site fails): put Netscape-format cookies in
  cookies/instagram.txt, cookies/reddit.txt, ... (name = platform name below)
Use a THROWAWAY account for cookies, never your main one.
"""
import argparse
import importlib.util
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PLATFORMS = {
    "instagram": ["instagram.com", "instagr.am"],
    "youtube": ["youtube.com", "youtu.be", "music.youtube.com"],
    "tiktok": ["tiktok.com"],
    "facebook": ["facebook.com", "fb.watch", "fb.com"],
    "twitter": ["twitter.com", "x.com", "t.co"],
    "reddit": ["reddit.com", "redd.it"],
    "pinterest": ["pinterest.com", "pin.it", "pinterest.co.uk", "pinterest.in"],
    "twitch": ["twitch.tv"],
    "vimeo": ["vimeo.com"],
    "soundcloud": ["soundcloud.com"],
    "bluesky": ["bsky.app"],
    "tumblr": ["tumblr.com"],
    "bilibili": ["bilibili.com", "b23.tv"],
}

BATCH_REST_EVERY = 10          # long rest after this many downloads
SHORT_PAUSE = (8, 25)          # random seconds between downloads
LONG_REST = (120, 240)         # random seconds after every batch
RATE_LIMIT_HINTS = ("rate-limit", "rate limit", "429", "too many requests",
                    "login required", "checkpoint", "challenge")


YTDLP = [sys.executable, "-m", "yt_dlp"]
GDL = [sys.executable, "-m", "gallery_dl"]


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def detect_platform(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for name, domains in PLATFORMS.items():
        for d in domains:
            if host == d or host.endswith("." + d):
                return name
    return "other"


def format_args(quality: str):
    if quality == "audio":
        return ["-x", "--audio-format", "mp3"]
    if quality in ("1080", "720"):
        return ["-f", f"bv*[height<={quality}]+ba/b[height<={quality}]/b",
                "--merge-output-format", "mp4"]
    return ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def is_rate_limited(text: str) -> bool:
    t = text.lower()
    if "redirect to login page" in t:
        return False   # needs a login (cookies); a permanent answer, not a temporary rate limit
    return any(h in t for h in RATE_LIMIT_HINTS)


def download(url, quality, out_root: Path, cookies_dir):
    platform = detect_platform(url)
    out_dir = out_root / platform
    out_dir.mkdir(parents=True, exist_ok=True)

    cookie_args = []
    gdl_cookie_args = []
    if cookies_dir:
        cf = Path(cookies_dir) / f"{platform}.txt"
        if cf.exists():
            cookie_args = ["--cookies", str(cf)]
            gdl_cookie_args = ["--cookies", str(cf)]

    notes = []
    # 1) yt-dlp
    if has_module("yt_dlp"):
        cmd = [*YTDLP, "--no-playlist", "--force-overwrites", "--write-info-json", "-o", str(out_dir / "%(title).80B [%(id)s].%(ext)s"),
               *format_args(quality), *cookie_args, url]
        code, out = run(cmd)
        if code == 0:
            return True, platform, "downloaded with yt-dlp", False
        notes.append(out.strip().splitlines()[-1] if out.strip() else "yt-dlp failed")
        if is_rate_limited(out):
            limited = True
        else:
            limited = False
    else:
        limited = False
        notes.append("yt-dlp not installed (run: python -m pip install -U yt-dlp)")

    # 2) gallery-dl fallback (image posts, carousels)
    if has_module("gallery_dl"):
        cmd = [*GDL, "-d", str(out_dir), "--write-metadata", *gdl_cookie_args, url]
        code, out = run(cmd)
        if code == 0:
            return True, platform, "downloaded with gallery-dl", False
        notes.append(out.strip().splitlines()[-1] if out.strip() else "gallery-dl failed")
        limited = limited or is_rate_limited(out)
    else:
        notes.append("gallery-dl not installed (run: python -m pip install -U gallery-dl)")

    hint = ""
    if platform != "other":
        hint = f" (if this site needs login, put cookies in cookies/{platform}.txt)"
    return False, platform, " | ".join(notes) + hint, limited


def read_links(path):
    if not Path(path).exists():
        print(f"File not found: {path}. Create it with one link per line, or run without a file to paste links.")
        sys.exit(1)
    links = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            links.append(line)
    return links


def paste_links():
    print("Paste links, one per line. Empty line to start.")
    links = []
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            break
        links.append(line)
    return links


def ask_quality():
    ans = input("Quality [best/1080/720/audio] (Enter = best): ").strip().lower()
    return ans if ans in ("best", "1080", "720", "audio") else "best"


def main():
    ap = argparse.ArgumentParser(description="Slow multi-platform downloader")
    ap.add_argument("links_file", nargs="?", help="text file with one link per line")
    ap.add_argument("-q", "--quality", choices=["best", "1080", "720", "audio"])
    ap.add_argument("-o", "--out", default="Downloads")
    ap.add_argument("--cookies-dir", default=None)
    ap.add_argument("--fast", action="store_true", help="skip pauses (not recommended)")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found. Videos may download as separate video/audio pieces.")
    links = read_links(args.links_file) if args.links_file else paste_links()
    if not links:
        print("No links given.")
        return
    quality = args.quality or ask_quality()
    out_root = Path(args.out)

    print(f"\n{len(links)} link(s), quality={quality}, saving to {out_root.resolve()}\n")
    ok = fail = 0
    failed_links = []
    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] {url}")
        success, platform, msg, limited = download(url, quality, out_root, args.cookies_dir)
        if success:
            ok += 1
            print(f"   OK  [{platform}] {msg}")
        else:
            fail += 1
            failed_links.append(url)
            print(f"   FAIL [{platform}] {msg}")
        if limited:
            print("   Looks rate-limited/blocked. Stopping so we don't hammer the site.")
            print("   Try again later; remaining links were not attempted.")
            failed_links.extend(links[i:])
            break
        if i < len(links) and not args.fast:
            if i % BATCH_REST_EVERY == 0:
                wait = random.uniform(*LONG_REST)
                print(f"   Batch rest: {wait:.0f}s")
            else:
                wait = random.uniform(*SHORT_PAUSE)
                print(f"   Pause: {wait:.0f}s")
            time.sleep(wait)

    print(f"\nDone. {ok} ok, {fail} failed.")
    if failed_links:
        Path("failed_links.txt").write_text("\n".join(failed_links) + "\n", encoding="utf-8")
        print("Failed/unattempted links saved to failed_links.txt (reuse it as input).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(1)
