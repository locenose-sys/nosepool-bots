# tgwatch - post a link in your channel -> laptop downloads -> file replaces the link

You post a link in your Telegram channel (@nosepool) from your phone. While `tgwatch.py`
is running on your laptop, it downloads the link slowly (using linkgrab), posts the
file to the same channel, and deletes your original link post. If the laptop is off,
links wait and are picked up when it is back on. Telegram keeps unread bot updates for
up to 24 hours (from the Bot API docs; not checked today), so links older than a day
are lost.

It also still works in a private chat with the bot (send a link there, it posts to
the channel).

## One-time setup

1. **New bot.** In Telegram open @BotFather, send `/newbot`, copy the token.
   Use a NEW bot, not your existing queue bot (that one uses a webhook and would clash).
2. **Make the bot an admin of the channel** with the rights "Post messages" and
   "Delete messages". You must be an admin of the channel to add it.
3. **Your Telegram user ID.** Message @userinfobot and copy the number.
4. **Config.** Copy `tgconfig.example.json` to `tgconfig.json` (same folder) and fill in
   BOT_TOKEN and ALLOWED_USER_ID. GROUP is `@nosepool`. Keep this file private; never
   paste the token into a chat or upload it to GitHub.
5. **Install requests** (usually already present): `python -m pip install requests`
6. **Open a private chat with your new bot and press Start.** Errors are sent there,
   not into the channel, and the bot can only message you after you press Start.

## Run

```
python tgwatch.py
```

Leave the window open. Post links in the channel:

- `https://www.instagram.com/reel/...`
- `720 https://...` (also `1080`, `audio`)
- several links in one post (done one at a time with pauses)

Posts with no link are ignored.

## How waiting feels

- **Instant acknowledgment.** A listener notices each new post within seconds and puts a
  👀 reaction on it, even while another download is running.
- **Nothing is dropped.** Every post is saved in `queue.json`. If you stop the script
  (or it pauses), the rest stays queued and resumes by itself next time.
- **Progress.** The terminal prints what it is doing, how many links are waiting, and
  pauses. Your private chat with the bot gets "Queued N link(s) ... roughly X min"
  (a rough guess, about 45 seconds per link).
- **Failures are visible.** A failed link gets a 👎 reaction and its error; its post stays.
  (A red X is not an allowed Telegram reaction, so 👎 is used.)
- Reactions may not work in every channel; if Telegram refuses them it carries on without.

## Captions

The post's own text (tweet text, Instagram caption, Reddit/YouTube title) is read from the
metadata the downloaders save and sent as the caption of the file. Telegram allows 1024
characters, so longer text is cut with a "..." at the end. Albums show the caption on the
first item. If a site gives no text, the file is sent without a caption.
(Tested with sample metadata only; real tweets and captions come from yt-dlp/gallery-dl.)

## Behaviour

- Channel posts have no sender attached, so who can trigger it = who can post in the
  channel (its admins). In a group, only ALLOWED_USER_ID can.
- The original link post is deleted only if every link in it succeeded. A failed link
  stays in place and the error goes to your private chat. Set DELETE_ORIGINAL to
  "false" in tgconfig.json to keep link posts.
- Over Telegram's ~50 MB bot upload limit: retries once at 720p, otherwise keeps the
  file on the laptop and tells you privately.
- Photo/video carousels are posted as one album.
- A real rate limit pauses 30 minutes, then retries (up to 3 times, then gives up on that
  link). A "needs login" answer does not pause anything; that link just fails.
- Instagram photo/carousel posts usually need throwaway-account cookies
  (`cookies/instagram.txt`); reels worked without one in our test.

## Not tested live

Tested against a fake Telegram server only. The first real run may need a fix. Send
one link first and read what comes back. Never send the bot token to anyone.

## Separate bots per site (new)

Each site can have its own bot, so one site's block or long wait never slows the others.

- Make one bot per site in @BotFather and make every one an admin of @nosepool
  ("Post messages" + "Delete messages"). Press Start in a private chat with each bot
  (that's how it can message you errors).
- Put the tokens in `tgconfig.json` under `TOKENS` (see `tgconfig.example.json`), or in
  environment variables `TOKEN_INSTAGRAM`, `TOKEN_TWITTER`, `TOKEN_PINTEREST`, `TOKEN_YOUTUBE`.
- Every bot reads the channel but only takes links from its own site. Each has its own queue
  file (`queue_<site>.json`), its own downloads folder and its own pacing.
- `ONLY=youtube` makes a computer run just that bot; `ONLY=instagram,twitter,pinterest` runs
  those three. So the laptop can do YouTube while a server does the rest.
- A post that mixes sites (say an Instagram and a YouTube link) is handled by both bots, but the
  original post is NOT deleted, so a failure on one link is never lost. Delete it yourself.
- Without `TOKENS` it runs as one bot for every site, exactly as before.
- Pacing is now irregular: usually a short pause, sometimes a longer break, and a long rest after
  a varying 6-12 downloads. This makes it look less mechanical, but it cannot hide the server's IP.
- If the environment variable `PORT` is set (Render does this), a plain status page answers on it
  so a free pinger can keep the service awake. It shows only queue counts, no secrets.
- Tested against a fake Telegram server only (four bots, mixed posts, blocked YouTube).

## Following profiles (new)

Post `follow <profile link>` in the channel (or privately to a bot) and whatever that profile
posts next is downloaded and posted to the channel by itself.

- `follow https://www.instagram.com/someone/`   start following
- `unfollow https://www.instagram.com/someone/` stop
- `following`                                   each bot lists the profiles it follows (to your private chat)
- The bot that owns that site (Instagram, Twitter/X, Pinterest, YouTube) takes the command.
  Your command post is deleted once it worked; if the profile can't be read (private, login needed)
  it gets a thumbs-down and stays.
- Posts that already exist when you start following are **not** posted, only new ones.
- Each profile is checked about every 120 minutes (change with `FOLLOW_EVERY_MIN`), with random
  jitter and a minute or more between any two checks. Followed posts go through the same slow queue
  as ordinary links and are posted with their caption; nothing is deleted or reacted to.
- YouTube: a channel's `/videos` list (Shorts are not watched). Twitter/X: their own tweets, not
  retweets. Instagram: posts and reels from the profile page.
- Instagram profiles and Twitter/X timelines often need a login (cookies), and checking a profile
  again and again is what Instagram blocks soonest. If a check fails you get one message, then it
  backs off (up to 6 hours between tries).
- The follow list is saved in `follows_<site>.json`. On a free host that loses files on restart
  (Render free), set `FOLLOWS=<link>,<link>` in the environment: every restart re-adds those profiles
  (silently, without posting what already exists).
- Tested against a fake Telegram and sample downloader output only, not against the real sites.

## Daily limit
Each bot counts the links it fetched today and stops at its limit: 100 for instagram, 200 for twitter, pinterest and youtube.
Change with env `DAILY_LIMIT_INSTAGRAM` (or `_TWITTER`, `_PINTEREST`, `_YOUTUBE`), or `DAILY_LIMIT` for all (0 = no limit).
Waiting links stay queued and continue after midnight India time (`DAY_OFFSET_HOURS`, default 5.5). One message is sent when a bot reaches its limit.
