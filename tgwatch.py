#!/usr/bin/env python3
"""
tgwatch - post a link in your Telegram channel; this laptop downloads it
(slowly, via linkgrab) and posts the file to the same channel.

Two parts run side by side:
  * a LISTENER that notices new posts within seconds, puts a reaction on them
    and saves them in a queue file (queue.json), so nothing is ever dropped;
  * a WORKER that goes through the queue one link at a time with polite pauses.

Run on the laptop and leave the window open:
    python tgwatch.py

Needs tgconfig.json (copy tgconfig.example.json) or environment variables:
    BOT_TOKEN, GROUP, ALLOWED_USER_ID   (optional: DELETE_ORIGINAL=true/false)

In the channel, post one or more links. Optional quality word first:
    720 <link>   1080 <link>   audio <link>

Reactions on your link post:  eyes = received/queued,  thumbs-down = failed.
On success the link post is deleted (the file replaces it).

SEPARATE BOTS (optional): give each site its own bot, so a block or a long wait on one
never slows the others. Put the tokens in tgconfig.json as
    "TOKENS": {"instagram": "...", "twitter": "...", "pinterest": "...", "youtube": "..."}
or as environment variables TOKEN_INSTAGRAM, TOKEN_TWITTER, TOKEN_PINTEREST, TOKEN_YOUTUBE.
Every bot reads the channel but only takes links from its own site. ONLY=youtube (or
ONLY=instagram,twitter,pinterest) picks which of them this computer/server runs.
If PORT is set (e.g. on Render) a tiny status page is served so a pinger can keep it awake.
"""
import json
import os
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import linkgrab as lg

API = os.environ.get("TG_API_BASE", "https://api.telegram.org")
URL_RE = re.compile(r"https?://[^\s]+")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXT = {".mp3", ".m4a"}
LEFTOVER = re.compile(r"\.f[\w-]+\.\w+$|\.part$|\.ytdl$|\.json$")
MAX_UPLOAD = 49 * 1024 * 1024      # Bot API upload limit is ~50 MB
MAX_PHOTO = 10 * 1024 * 1024
QUALITY_WORDS = ("best", "1080", "720", "audio")
RATE_LIMIT_SLEEP = 30 * 60
MAX_TRIES = 2                      # rate-limit attempts per link (one retry) before giving up on it
EST_SECONDS_PER_LINK = 45          # rough, only used for the "about N min" hint
REACT_RECEIVED = "\U0001F440"      # eyes
REACT_FAILED = "\U0001F44E"        # thumbs down  (red X is not an allowed reaction)
REACT_DONE = "\U0001F44D"          # thumbs up (private chat only; channel posts get deleted)

BOT_DEFS = {"instagram": ["instagram"], "twitter": ["twitter"],
            "pinterest": ["pinterest"], "youtube": ["youtube"]}   # one bot per site

STOP = threading.Event()
OUT_ROOT = Path("Downloads")


class TgError(Exception):
    pass


# ---------------------------------------------------------------- config / api
def load_config():
    cfg = {}
    p = Path("tgconfig.json")
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    for k in ("BOT_TOKEN", "GROUP", "ALLOWED_USER_ID"):
        cfg[k] = str(os.environ.get(k, cfg.get(k, ""))).strip()
    cfg["DELETE_ORIGINAL"] = str(os.environ.get("DELETE_ORIGINAL", cfg.get("DELETE_ORIGINAL", "true"))).strip()
    only = {x.strip().lower() for x in os.environ.get("ONLY", "").split(",") if x.strip()}
    tokens = {}
    for name in BOT_DEFS:
        t = str(os.environ.get("TOKEN_" + name.upper()) or (cfg.get("TOKENS") or {}).get(name) or "").strip()
        if t and (not only or name in only):
            tokens[name] = t
    cfg["TOKENS"] = tokens
    required = ("GROUP", "ALLOWED_USER_ID") if tokens else ("BOT_TOKEN", "GROUP", "ALLOWED_USER_ID")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit("Missing settings: " + ", ".join(missing) + ". See tgconfig.example.json.")
    return cfg


def api(cfg, method, data=None, files=None, timeout=60):
    url = f"{API}/bot{cfg['BOT_TOKEN']}/{method}"
    try:
        r = requests.post(url, data=data, files=files, timeout=timeout)
        j = r.json()
    except Exception as e:  # never leak the token in error text
        raise TgError(str(e).replace(cfg["BOT_TOKEN"], "***")) from None
    if not j.get("ok"):
        raise TgError(str(j.get("description", "unknown Telegram error")))
    return j["result"]


def now():
    return time.strftime("%H:%M:%S")


def say(cfg, chat_id, text):
    print("  >>", text[:500])   # always show it here too, in case Telegram can't deliver it
    try:
        api(cfg, "sendMessage", {"chat_id": chat_id, "text": text[:3500]})
    except TgError as e:
        print("  (could not reply:", e, ")")


def react(cfg, chat_id, message_id, emoji):
    """Best-effort reaction; stops trying (for this bot) after repeated failures."""
    if cfg.get("_rf", 0) >= 3:
        return
    try:
        api(cfg, "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id,
             "reaction": json.dumps([{"type": "emoji", "emoji": emoji}])}, timeout=20)
        cfg["_rf"] = 0
    except TgError as e:
        cfg["_rf"] = cfg.get("_rf", 0) + 1
        print(f"  [{cfg.get('NAME', 'bot')}] (reaction not possible: {e})")
        if cfg["_rf"] >= 3:
            print(f"  [{cfg.get('NAME', 'bot')}] (giving up on reactions; everything else keeps working)")


def countdown_sleep(seconds, label):
    """Sleep in small steps so Ctrl+C works; show a heartbeat on long waits."""
    if seconds <= 0:
        return
    if seconds >= 2:
        print(f"  ... {label}: waiting {seconds:.0f}s")
    end = time.monotonic() + seconds
    last = time.monotonic()
    while not STOP.is_set():
        left = end - time.monotonic()
        if left <= 0:
            return
        if time.monotonic() - last >= 60:
            print(f"  ... {label}: {left / 60:.0f} min left")
            last = time.monotonic()
        STOP.wait(min(1.0, left))


# ---------------------------------------------------------------- queue file
class QueueStore:
    """Jobs (one per channel post) saved to queue.json so nothing is lost."""

    def __init__(self, path="queue.json"):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.jobs = []
        self.seen = []
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.jobs, self.seen = d.get("jobs", []), d.get("seen", [])
            except Exception:
                print("queue.json could not be read; starting with an empty queue.")

    def _save(self):   # caller holds the lock
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"jobs": self.jobs, "seen": self.seen[-500:]}), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, job):
        with self.lock:
            if job["key"] in self.seen:
                return False
            self.seen.append(job["key"])
            self.jobs.append(job)
            self._save()
            return True

    def front(self):
        with self.lock:
            return self.jobs[0] if self.jobs else None

    def next_ready(self):
        """First job that is not sitting out a rate-limit timer."""
        with self.lock:
            t = time.time()
            for j in self.jobs:
                if j.get("not_before", 0) <= t:
                    return j
            return None

    def earliest_wait(self):
        with self.lock:
            if not self.jobs:
                return 0
            return max(0, min(j.get("not_before", 0) for j in self.jobs) - time.time())

    def rotate(self, job):
        """Send a job to the back of the queue."""
        with self.lock:
            if job in self.jobs:
                self.jobs.remove(job)
                self.jobs.append(job)
            self._save()

    def update(self):
        with self.lock:
            self._save()

    def finish(self, job=None):
        with self.lock:
            if job is None and self.jobs:
                job = self.jobs[0]
            if job in self.jobs:
                self.jobs.remove(job)
            self._save()

    def waiting_links(self):
        with self.lock:
            return sum(len(j["urls"]) for j in self.jobs)


# ---------------------------------------------------------------- files / posting
def snapshot(root: Path):
    """path -> modification time (ns), so a re-downloaded same-name file counts as new."""
    return {p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()} if root.exists() else {}


def new_files(before, after):
    changed = [p for p, m in after.items() if before.get(p) != m]
    return [p for p in sorted(changed) if not LEFTOVER.search(p.name)]


CAPTION_MAX = 1024                 # Telegram's limit for a media caption
TITLE_PLATFORMS = {"reddit", "youtube", "vimeo", "bilibili", "twitch", "soundcloud"}


def find_caption(before, after, platform):
    """Caption text from the metadata files the downloaders just wrote (tweet text, post caption...)."""
    changed = [p for p, m in sorted(after.items()) if before.get(p) != m and p.name.endswith(".json")]
    for p in changed:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        for key in ("description", "content", "caption"):
            v = d.get(key)
            if isinstance(v, str) and v.strip():
                return clip(v.strip())
        if platform in TITLE_PLATFORMS and isinstance(d.get("title"), str) and d["title"].strip():
            return clip(d["title"].strip())
    return ""


def clip(text):
    return text if len(text) <= CAPTION_MAX else text[:CAPTION_MAX - 1] + "\u2026"


def kind(p: Path):
    e = p.suffix.lower()
    if e in VIDEO_EXT:
        return "video"
    if e in PHOTO_EXT:
        return "photo"
    if e in AUDIO_EXT:
        return "audio"
    return "document"


def too_big(p: Path):
    return p.stat().st_size > MAX_UPLOAD


def send_one(cfg, p: Path, caption=""):
    k = kind(p)
    if k == "photo" and p.stat().st_size > MAX_PHOTO:
        k = "document"
    method = {"video": "sendVideo", "photo": "sendPhoto", "audio": "sendAudio", "document": "sendDocument"}[k]
    data = {"chat_id": cfg["GROUP"]}
    if k == "video":
        data["supports_streaming"] = "true"
    if caption:
        data["caption"] = clip(caption)
    with open(p, "rb") as f:
        api(cfg, method, data, {k: f}, timeout=600)


def send_album(cfg, paths, caption=""):
    media, handles, files = [], [], {}
    try:
        for i, p in enumerate(paths):
            h = open(p, "rb")
            handles.append(h)
            files[f"f{i}"] = h
            item = {"type": "video" if kind(p) == "video" else "photo", "media": f"attach://f{i}"}
            if i == 0 and caption:
                item["caption"] = clip(caption)    # Telegram shows the first item's caption for the album
            media.append(item)
        api(cfg, "sendMediaGroup", {"chat_id": cfg["GROUP"], "media": json.dumps(media)}, files, timeout=600)
    finally:
        for h in handles:
            h.close()


def post_to_group(cfg, files, caption=""):
    """Send files; carousels of photos/videos go as one album."""
    albumable = [p for p in files if kind(p) in ("video", "photo")
                 and not (kind(p) == "photo" and p.stat().st_size > MAX_PHOTO)]
    if len(files) > 1 and len(albumable) == len(files):
        for i in range(0, len(files), 10):
            chunk = files[i:i + 10]
            cap = caption if i == 0 else ""
            if len(chunk) == 1:
                send_one(cfg, chunk[0], cap)
            else:
                send_album(cfg, chunk, cap)
    else:
        for n, p in enumerate(files):
            send_one(cfg, p, caption if n == 0 else "")


def is_watched(cfg, chat):
    """True if this chat is the group/channel we post to (match by @username or numeric id)."""
    g = cfg["GROUP"].lstrip("@").lower()
    return str(chat.get("id")) == cfg["GROUP"] or (chat.get("username") or "").lower() == g


def handle_link(cfg, chat_id, url, quality, out_root, cookies_dir, chatty=True):
    """Download then post. Returns 'ok', 'fail' or 'limited'.
    chatty=False keeps status chatter out (errors still reported to chat_id)."""
    attempts = [quality] + (["720"] if quality == "best" else [])
    for n, q in enumerate(attempts):
        platform = lg.detect_platform(url)
        before = snapshot(out_root / platform)
        ok, platform, msg, limited = lg.download(url, q, out_root, cookies_dir)
        after = snapshot(out_root / platform)
        files = new_files(before, after)
        caption = find_caption(before, after, platform)
        if not ok or not files:
            say(cfg, chat_id, f"Could not download ({platform}): {msg[:600]}")
            return "limited" if limited else "fail"
        big = [p for p in files if too_big(p)]
        if big:
            if n + 1 < len(attempts):
                for p in big:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                if chatty:
                    say(cfg, chat_id, "File over 50 MB, retrying at 720p...")
                continue
            say(cfg, chat_id, "Still over Telegram's ~50 MB bot upload limit: "
                              + ", ".join(p.name for p in big) + ". Kept on the laptop, not posted.")
            return "fail"
        try:
            post_to_group(cfg, files, caption)
        except TgError as e:
            say(cfg, chat_id, f"Downloaded but could not post to the group: {e}. "
                              "Check the bot is an admin of the group and GROUP is correct.")
            return "fail"
        if chatty:
            say(cfg, chat_id, f"Posted {len(files)} file(s) to the group.")
        return "ok"
    return "fail"


def parse_message(text):
    urls = URL_RE.findall(text or "")
    first = (text or "").strip().split(None, 1)[0].lower() if (text or "").strip() else ""
    quality = first if first in QUALITY_WORDS else "best"
    return urls, quality


# ---------------------------------------------------------------- listener
def ingest(cfg, qs, u):
    """Turn one Telegram update into a queued job and acknowledge it straight away."""
    msg = u.get("message") or u.get("channel_post")
    if not msg:
        return
    watched = is_watched(cfg, msg["chat"])
    sender = msg.get("from")
    if watched:
        # Channel posts carry no 'from' (only admins can post there).
        # In a group, only ALLOWED_USER_ID may trigger it.
        if sender is not None and str(sender.get("id")) != cfg["ALLOWED_USER_ID"]:
            return
        reply = int(cfg["ALLOWED_USER_ID"])      # errors/progress go to your private chat
    else:
        if str((sender or {}).get("id")) != cfg["ALLOWED_USER_ID"]:
            return  # ignore everyone else silently
        reply = msg["chat"]["id"]
    text = msg.get("text") or msg.get("caption") or ""
    if not watched and (text.startswith("/start") or text.startswith("/help")):
        say(cfg, reply, "Post a link in the channel (optionally 720 / 1080 / audio first), "
                        "or send it here. I download it on the laptop and post it to the channel.")
        return
    urls, quality = parse_message(text)
    if not urls:
        if not watched:
            say(cfg, reply, "No link found in that message.")
        return   # ordinary posts in the watched chat are left alone
    all_mine = True
    plats = cfg.get("PLATFORMS")
    if plats:
        mine = [u for u in urls if lg.detect_platform(u) in plats]
        if not mine:
            if not watched:
                say(cfg, reply, f"This bot only handles {', '.join(plats)} links.")
            return   # another bot owns these links
        all_mine = len(mine) == len(urls)
        urls = mine
    job = {"key": f"{msg['chat']['id']}:{msg['message_id']}", "chat": msg["chat"]["id"],
           "mid": msg["message_id"], "urls": urls, "quality": quality, "watched": watched,
           "reply": reply, "ok": True, "tries": 0, "all_mine": all_mine}
    if not qs.add(job):
        return   # already queued (Telegram delivered it twice)
    react(cfg, job["chat"], job["mid"], REACT_RECEIVED)
    total = qs.waiting_links()
    ahead = total - len(urls)
    mins = max(1, round(total * EST_SECONDS_PER_LINK / 60))
    say(cfg, reply, f"Queued {len(urls)} link(s), {ahead} already ahead. "
                    f"Roughly {mins} min for everything waiting (rough guess).")


def listener_step(cfg, qs, state, timeout=25):
    try:
        updates = api(cfg, "getUpdates", {"offset": state["offset"], "timeout": timeout}, timeout=timeout + 15)
    except TgError as e:
        print("getUpdates problem:", e)
        STOP.wait(5)
        return
    for u in updates:
        try:
            ingest(cfg, qs, u)
        except Exception as e:   # one bad update must not stop the listener
            print("  (could not handle an update:", str(e).replace(cfg["BOT_TOKEN"], "***"), ")")
        state["offset"] = u["update_id"] + 1   # confirmed on the NEXT call, after the queue is saved


def listener_loop(cfg, qs, state):
    while not STOP.is_set():
        listener_step(cfg, qs, state)


# ---------------------------------------------------------------- worker
def pace_wait(state):
    """Irregular, human-ish gaps: usually a short pause, sometimes a longer break,
    and a long rest after a varying number of downloads (not a fixed every-10th)."""
    state["since_rest"] = state.get("since_rest", 0) + 1
    if "rest_after" not in state:
        state["rest_after"] = random.randint(6, 12)
    if state["since_rest"] >= state["rest_after"]:
        state["since_rest"] = 0
        state["rest_after"] = random.randint(6, 12)
        return random.uniform(*lg.LONG_REST), "long rest"
    if random.random() < 0.12:
        hi = lg.SHORT_PAUSE[1]
        return random.uniform(hi * 3, hi * 6), "short break"
    return random.uniform(*lg.SHORT_PAUSE), "pause"


def process_job(cfg, qs, job, out_root, cookies_dir, state):
    chatty = not job["watched"]
    job.pop("not_before", None)
    while job["urls"] and not STOP.is_set():
        url = job["urls"][0]
        print(f"[{now()}] downloading {url} ({job['quality']}) - {qs.waiting_links() - 1} more waiting after this")
        result = handle_link(cfg, job["reply"], url, job["quality"], out_root, cookies_dir, chatty)
        state["count"] += 1
        if result == "limited":
            job["tries"] += 1
            if job["tries"] >= MAX_TRIES:
                say(cfg, job["reply"], f"Gave up on this link, the site kept refusing: {url}")
                job["ok"] = False
                job["urls"].pop(0)
                job["tries"] = 0
                qs.update()
            else:
                # Do not hold up the rest of the queue: this post waits at the back.
                job["not_before"] = time.time() + RATE_LIMIT_SLEEP
                qs.update()
                say(cfg, job["reply"], f"Site refused this link (rate limit or login). It goes to the back of "
                                       f"the queue and is retried once in {RATE_LIMIT_SLEEP / 60:.0f} min; "
                                       "your other links carry on meanwhile.")
                qs.rotate(job)
                return
        else:
            if result != "ok":
                job["ok"] = False
            job["urls"].pop(0)
            job["tries"] = 0
            qs.update()
        if qs.waiting_links() > 0:
            wait, label = pace_wait(state)
            countdown_sleep(wait, f"[{cfg.get('NAME', 'bot')}] {label}")
    if STOP.is_set() and job["urls"]:
        return   # stopped mid-job: it stays in queue.json and resumes next time
    if job["ok"]:
        if job["watched"]:
            if not job.get("all_mine", True):
                pass   # the post also has links for other bots; leave it so a failure there is not lost
            elif cfg.get("DELETE_ORIGINAL", "true").lower() != "false":
                try:
                    api(cfg, "deleteMessage", {"chat_id": job["chat"], "message_id": job["mid"]})
                except TgError as e:
                    say(cfg, job["reply"], f"Posted, but could not delete the original link post: {e}. "
                                           "Give the bot the 'Delete messages' admin right, "
                                           "or set DELETE_ORIGINAL to false.")
        else:
            react(cfg, job["chat"], job["mid"], REACT_DONE)
    else:
        react(cfg, job["chat"], job["mid"], REACT_FAILED)
    qs.finish(job)
    print(f"[{now()}] finished a post. {qs.waiting_links()} link(s) still waiting.")


def worker_loop(cfg, qs, out_root, cookies_dir, state):
    last_beat = 0.0
    while not STOP.is_set():
        job = qs.next_ready()
        if job is None:
            wait = qs.earliest_wait() if qs.jobs else 0
            if wait > 0 and time.monotonic() - last_beat >= 60:
                print(f"  ... everything left is waiting out a site refusal: {wait / 60:.0f} min to the next retry")
                last_beat = time.monotonic()
            STOP.wait(1)
            continue
        try:
            process_job(cfg, qs, job, out_root, cookies_dir, state)
        except Exception as e:   # a bug on one job must not freeze the queue
            print("  (unexpected problem, skipping this post:", str(e).replace(cfg["BOT_TOKEN"], "***"), ")")
            react(cfg, job["chat"], job["mid"], REACT_FAILED)
            qs.finish(job)


def start_health(port, bots):
    """Tiny status page so a free host can be kept awake by a pinger. Shows no secrets."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self):
            return ("ok\n" + "\n".join(f"{n}: {q.waiting_links()} link(s) waiting" for n, q in bots.items())).encode()

        def do_GET(self):
            b = self._body()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self._body())))
            self.end_headers()

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Status page on port {port}")


def start_bot(base, name, token, cookies_dir):
    """One separate bot: own token, own queue file, own downloads folder, own pacing."""
    cfg = dict(base)
    cfg.update(BOT_TOKEN=token, PLATFORMS=BOT_DEFS[name], NAME=name, _rf=0)
    try:
        me = api(cfg, "getMe")
    except TgError as e:
        print(f"[{name}] could not reach Telegram with that token, skipping this bot: {e}")
        return None
    print(f"[{name}] listening as @{me.get('username')} (takes {', '.join(BOT_DEFS[name])} links)")
    qs = QueueStore(f"queue_{name}.json")
    if qs.jobs:
        print(f"[{name}] resuming {len(qs.jobs)} saved post(s) from queue_{name}.json")
    state = {"offset": None, "count": 0}
    threading.Thread(target=worker_loop, args=(cfg, qs, OUT_ROOT / name, cookies_dir, state), daemon=True).start()
    threading.Thread(target=listener_loop, args=(cfg, qs, state), daemon=True).start()
    return qs


def main():
    cfg = load_config()
    cookies_dir = "cookies" if Path("cookies").is_dir() else None
    if not lg.shutil.which("ffmpeg"):
        print("WARNING: ffmpeg not found. Videos may download as separate video/audio pieces.")
    if cfg["TOKENS"]:
        bots = {}
        for name, token in cfg["TOKENS"].items():
            qs = start_bot(cfg, name, token, cookies_dir)
            if qs is not None:
                bots[name] = qs
        if not bots:
            sys.exit("No bot could start. Check the tokens.")
        port = os.environ.get("PORT")
        if port:
            start_health(int(port), bots)
        print(f"Running {len(bots)} separate bot(s) for {cfg['GROUP']}. Ctrl+C to stop.")
        try:
            while not STOP.is_set():
                STOP.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            STOP.set()
            print("\nStopped. Waiting links stay in the queue_*.json files.")
        return
    try:
        me = api(cfg, "getMe")
    except TgError as e:
        sys.exit(f"Could not reach Telegram with that BOT_TOKEN: {e}")
    print(f"Listening as @{me.get('username')}. Group: {cfg['GROUP']}. Ctrl+C to stop.")
    qs = QueueStore()
    if qs.jobs:
        print(f"Resuming {len(qs.jobs)} saved post(s) ({qs.waiting_links()} link(s)) from queue.json.")
    state = {"offset": None, "count": 0}
    worker = threading.Thread(target=worker_loop, args=(cfg, qs, OUT_ROOT, cookies_dir, state), daemon=True)
    worker.start()
    try:
        listener_loop(cfg, qs, state)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        print(f"\nStopped. {qs.waiting_links()} link(s) remain in queue.json and resume next time.")


if __name__ == "__main__":
    main()
