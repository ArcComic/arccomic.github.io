# ArcHelper — Handoff Notes

Read this first if you're picking this project up in a new chat.

## What this is

A Termux bot + local dashboard for cross-posting comics from a Telegram
channel (t.me/ArcComic) to Facebook and X/Twitter. Runs entirely on an
Android phone (no PC, no Android Studio) via Termux.

## Files (only 2, on purpose)

- `ArcHelper.py` — everything: Flask dashboard, Telegram long-polling
  watcher, HTML/CSS/JS embedded as a Python string (no separate
  templates folder). This is intentional — the user asked for minimal
  file count so the project is easy to hand off.
- `setup.sh` — one-time Termux setup: installs python + pip deps,
  creates `starthelp` / `stophelp` shell commands.

These two files must live together in a folder named **ArcComicHelper**
on the phone's shared storage (not Termux's private home directory) —
e.g. `~/storage/shared/ArcComicHelper` from within Termux, which maps
to real phone storage. The user has repeatedly hit bugs from having a
stale copy of `ArcHelper.py` sitting in the wrong place — **always
double check the file is genuinely updated on-device** (see "Common
gotcha" below) before diagnosing further.

At runtime, `ArcHelper.py` auto-creates these next to itself (do not
build them in advance, do not commit fake versions of them):

- `settings.json` — bot token, channel link, `tg_offset` (Telegram
  update cursor), and the last-selected platform/page (so the
  dashboard remembers your spot across refreshes)
- `data.json` — all platforms → pages → templates → posts →
  edited_covers → cover_filter_strength
- `placeholders.txt` — auto-regenerated every startup; documents every
  `{field}` usable inside a template (`{code}`, `{author}`, etc.) so
  the user can write their own templates without asking Claude
- `covers/` — downloaded ORIGINAL comic cover images (as sent to
  Telegram, untouched)
- `edited_covers/` — filtered + framed covers shown in the dashboard's
  Covers tab (see "Covers section" below). Auto-created; not committed.

The frame image is NOT auto-created — the user supplies it themselves
at `frame/frame.png` (create the `frame/` folder if it doesn't exist).
Without it, `generate_edited_cover` logs a warning and skips the effect
for that post (posts/templates still work normally either way).

## How it works

1. Bot long-polls the Telegram Bot API (`getUpdates`) for new
   `channel_post` updates on the user's channel. The bot's token comes
   from `settings.json`; the user already has a BotFather token and
   already added the bot as admin/member of the channel.
2. Every incoming post's caption is checked against
   `REQUIRED_FIELDS = ["code","author","categories","language","rating"]`
   (see `parse_comic_fields`). If all 5 are present, it's treated as a
   real comic post; otherwise it's silently ignored (filters out
   sponsor ads/channel announcements).
3. On a verified post, `handle_channel_post` pushes it into pages
   across every platform, per that platform's split setting: OFF
   (default) broadcasts a copy to every page on the platform; ON
   round-robins across pages one-per-comic. See "Split-posting
   feature" below for full details. Each page independently advances
   its own round-robin `templates` index. So the same comic shows up
   under Facebook AND X/Twitter (and any custom platform), each with
   its own rotating template text. The same post also gets its cover
   run through `generate_edited_cover` (filter + frame) and added to
   the Covers tab — see below.
4. The dashboard (`/`) is a single HTML page (dark mobile UI) with:
   - A tab strip built from `/api/platforms` — NOT hardcoded. Facebook
     and X/Twitter ship as the two defaults, but the `+` tab lets the
     user create arbitrary new platform tabs (e.g. Instagram) with
     their own pages/templates.
   - Per-platform: a page dropdown + ⚙️ button opening a template
     editor modal (add/edit/delete templates, add new pages).
   - Each page has "Haven't posted" (pending) and "Posted" (history)
     sections, each comic card showing cover image + filled template
     text + three big buttons: Copy / Posted / Delete.
   - A Settings tab (bot token + channel link).
5. Data storage: no database, just the two JSON files rewritten
   atomically (`load_json`/`save_json`, temp file + `os.replace`).

## Known-correct defaults (do not silently change wording)

**Facebook** — 4 templates, ONLY `{code}` varies, rest is fixed
wording. Exact text lives in `DEFAULT_FACEBOOK_TEMPLATES` in
`ArcHelper.py`. Example:
```
Sauce: {code}

Official Telegram Channel (Join Here):
https://www.facebook.com/61587350857766/posts/122126663451245028
```

**X/Twitter** — 10 templates, caption-only (no separate comment step
the way Facebook has). Exact text lives in `DEFAULT_X_TEMPLATES`.

If the user gives you new template wording, treat it as literal and
exact — this project has already had two rounds of Claude
misinterpreting "comment template" vs "post caption" content. When in
doubt, ask them to paste the exact wording rather than guessing at
intent.

## Common gotcha (has happened 2+ times)

The user ends up running a **stale** `ArcHelper.py` because:
- An old `data.json` on the phone still has old template text baked
  in (templates are only seeded once, on first data.json creation —
  editing `ArcHelper.py` later does NOT retroactively update an
  existing `data.json`)
- A stale copy of `ArcHelper.py` gets left in place because the user
  re-ran `setup.sh` instead of actually replacing the `.py` file, or
  unzipped/copied to the wrong folder

**When shipping any fix**, always tell the user to:
1. `stophelp`
2. Fully delete/replace `ArcHelper.py` (not just re-run setup.sh)
3. If templates/pages changed, also delete `data.json` (they'll lose
   any pending/posted comics, but that's usually fine for this
   project's testing stage — flag it though)
4. Verify with `wc -c ArcHelper.py` matching the byte count you tell
   them, and/or `grep` for a distinctive string you just added, BEFORE
   running `starthelp`
5. `starthelp`, then refresh the dashboard

## Cover image lifecycle (added after initial ship)

Since one Telegram post gets pushed into every platform tab, they all
share the same `cover_path`. Deleting rules:

- Deleting a post from one tab, or clearing history in one tab, only
  marks that post row `status: "deleted"` — it does NOT touch the
  cover file yet if another tab still has a surviving (pending or
  posted) row for the same `tg_message_id`.
- `_purge_orphaned_covers(data)` runs after every delete/clear-history
  call: it finds `tg_message_id`s with zero surviving rows anywhere,
  deletes their cover file from `covers/`, then drops those now-fully-
  dead `deleted` rows from `data.json` entirely (so the file doesn't
  grow forever holding tombstone rows).
- Settings tab has a "Clear All" button (`/api/clear_all`) that wipes
  every post (pending AND posted, across every tab) and deletes every
  file in `covers/` unconditionally, with a confirmation modal first.
  Templates and bot settings are untouched by this.

## Split-posting feature (added 2026-09-04)

Each platform now has `split_enabled` (bool) and `split_index` (int) in
`data.json`. When OFF (default), every incoming comic goes to that
platform's **first page only**. Previously (bug), `handle_channel_post`
unconditionally used `platform["pages"][0]` — so a comic never reached
any non-default page (e.g. an "Aggressive" page on X/Twitter), even
though the dashboard showed it fine on the default page. Fixed.

When split is turned ON for a platform (toggle switch next to the page
dropdown; `POST /api/platforms/<id>/split` with `{"enabled": true|false}`),
incoming comics round-robin across that platform's pages in page order —
1st page, then 2nd, then 3rd, then back to 1st — regardless of daily
posting volume (handles "sometimes 20, sometimes 10 a day" without
needing a known total in advance). Toggling split (either direction)
resets `split_index` back to 0, restarting the rotation from the first
page. Split is per-platform, independent — Facebook and X/Twitter (and
any custom platform tab) each have their own on/off state.

`load_data()` migrates old `data.json` files missing these keys via
`setdefault`, so no manual data.json edit is needed after upgrading —
just replace `ArcHelper.py` as usual per the gotcha steps above.

## Covers section (added 2026-09-15)

Every incoming comic's cover also gets a filtered + framed version saved
to a permanent "Covers" dashboard tab, separate from the per-platform
pages. Reason: reposting the exact same popular-manga cover everyone
else uses was tripping Facebook's automated "unoriginal content"
detection. Adding a visual filter + branded frame makes each repost
visually distinct even though the underlying manga is the same.

**Pipeline** (`apply_cover_effect` / `generate_edited_cover` in
`ArcHelper.py`):
1. Load the original downloaded cover (from `covers/`) and
   `frame/frame.png`.
2. Scale the cover to `_COVER_FRAME_SCALE = 0.9598` and anchor it at
   the frame canvas's top-left corner (864x1200, taken from
   `frame.png`'s own size), cropping any overflow past the canvas
   edges. This is NOT centered and does NOT add padding — matched
   exactly against a real reference image the user supplied, using
   OpenCV ORB feature-point alignment (not naive pixel-diffing, which
   gave false results at first because the filter itself shifts pixel
   values too much for raw diffing to find true alignment). Confirmed
   directly with the user after the numbers first looked surprising.
3. Apply a tone curve (`_TONE_CURVE_COEFFS`, a fitted cubic) that
   replicates the MIUI "Radiance" filter look from the user's sample:
   blacks stay dark, everything else gets a gentle lift. Plus a subtle
   cool-shadow/warm-highlight color grade (`rb_shift`, interpolated by
   luminance). Global `strength` (0-100, stored as
   `data["cover_filter_strength"]`, default 80) blends between
   untouched original and the full graded look.
4. Composite `frame.png` on top (alpha_composite - frame has a
   transparent interior + opaque border/logo art).
5. Save as PNG (lossless) to `edited_covers/`, filename
   `cover_{tg_message_id}_edited.png`.

**Data model**: `data["edited_covers"]` is a flat list of
`{id, tg_message_id, filename, created_at}`, independent of the
`posts` list (a cover exists once per Telegram message regardless of
how many platform/page rows that post fans out into).

**Dashboard**: permanent "Covers" tab (always present, not a
platform — sits between the `+` add-platform button and Settings).
Shows: a global filter-strength slider (saves via
`POST /api/covers/strength`, debounced 400ms; only affects covers
generated AFTER the change, not retroactively), a "Download All"
button (hits `GET /api/covers/download_all`, which zips every current
edited cover server-side and streams it back so the browser's normal
download flow saves one `.zip` — this is what makes it land in the
browser's actual Downloads folder even when ArcHelper is running on
someone else's phone with no direct filesystem access from the
dashboard), and a grid of cover cards (Download + Delete each).
"Clear History" always shows a Yes/Cancel confirm modal first
(`clearCoversConfirmModal`) — no destructive action fires without it.
Settings tab's existing "Clear All" button now also wipes
`edited_covers` (data + files), matching how it already wiped
`covers/`.

**Dependencies**: `setup.sh` now installs `pillow` and `numpy` in
addition to `flask`/`requests`. If picking up an existing install that
predates this, the user needs to re-run `setup.sh` (or just
`pip install pillow numpy` manually) before covers will generate —
missing deps will crash on import, not fail gracefully, since these
are stdlib-adjacent and assumed always-present once installed once.



`.post-card img.cover` was `object-fit:cover; max-height:260px`, which
crops comic pages to fill a fixed box — cutting off panels/text. Changed
to `object-fit:contain; max-height:420px` so the full page always shows
(letterboxed if narrower/taller than the box) instead of being cropped.

## Still open / not yet built

- X/Twitter platform currently reuses the exact same pending/posted
  comic-card UI as Facebook (cover image + template + Copy/Posted/
  Delete). User has not asked for anything different there yet, but
  hasn't explicitly confirmed the X UI is final either — check with
  them if it comes up.
- No image cross-posting automation, no direct API posting to
  Facebook/X/Telegram — this is a manual-copy-paste helper by design
  (the user does the actual posting; the bot just prepares the text
  and organizes the workflow).
- No pagination/cleanup for `covers/` OR `edited_covers/` — both
  accumulate forever except via the explicit Delete / Clear History /
  Clear All actions. Not raised as a problem yet, but worth flagging
  if either folder grows large.
- User mentioned wanting Pinterest added as a platform "in future" —
  not yet built, no requirements gathered on it beyond that one-line
  mention.
- The Covers filter strength slider only affects covers generated
  AFTER it's changed — there's no "re-apply to existing covers"
  button. Not asked for; flag if the user expects retroactive
  reprocessing.

## Where the fuller history lives

Claude's memory file `/areas/arc-helper-bot.md` has the full
conversation history and every requirement as originally stated by the
user, if you need more context than this file provides.
