#!/data/data/com.termux/files/usr/bin/env python3
"""
Arc Comic Bot - Auto-poster for GitHub Pages
Minimal dependencies: no lxml, no Pillow
"""

import os
import sys
import json
import re
import html
import unicodedata
import time
import atexit
import hashlib
import asyncio
import subprocess
import threading
import functools
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from flask import Flask, render_template_string, request, jsonify
import r2_upload

# ============== LOG FLUSH FIX ==============
# When bot.py runs under nohup (stdout redirected to bot.log, not a TTY),
# Python fully buffers stdout instead of line-buffering it. Flask's own
# request logging (via the `logging` module) flushes independently per
# line, which is why access-log GET/POST lines always showed up in
# bot.log promptly — but every plain print() in this file, including
# ones on background threads (the Telethon worker, the posting queue),
# could sit in that buffer for a long time before actually reaching
# disk. This silently broke live debugging: prints looked like they
# never ran, when they were really just delayed/invisible until the
# buffer filled or the process exited. Forcing flush=True on every
# print() call in this file fixes that with no other code changes
# needed anywhere else.
print = functools.partial(print, flush=True)

try:
    import yaml
except ImportError:
    print("📦 Installing PyYAML (needed to auto-configure _config.yml)...")
    subprocess.run([sys.executable, "-m", "pip", "install", "pyyaml",
                    "--break-system-packages", "-q"], check=False)
    import yaml

# Paths
WORK_DIR = "/storage/emulated/0/arccomic.github.io"
CONFIG_FILE = os.path.join(WORK_DIR, "config.json")
COVERS_DIR = os.path.join(WORK_DIR, "covers")
WORKS_DIR = os.path.join(WORK_DIR, "_works")
STATS_FILE = os.path.join(WORK_DIR, "stats.json")
SOCIAL_LINKS_FILE = os.path.join(WORK_DIR, "_data", "social_links.json")

DEFAULT_SOCIAL_LINKS = [
    {"platform": "telegram", "label": "Arc Comic", "sublabel": "Comics, art drops & story updates",
     "url": "https://t.me/ArcComic", "icon": "telegram"}
]

# SECURITY: tokens live OUTSIDE the git repo folder entirely, in a sibling
# directory. This makes it structurally impossible for `git add .` inside
# WORK_DIR to ever pick them up, even if .gitignore rules are ever missing,
# forgotten, or edited by mistake. This is the root cause fix for the
# earlier incident where a GitHub token was committed and had to be purged
# from history.
SECRETS_DIR = "/storage/emulated/0/ArcComicSecrets"
SECRETS_FILE = os.path.join(SECRETS_DIR, "secrets.json")
os.makedirs(SECRETS_DIR, exist_ok=True)

# Scan state lives outside the repo — it's operational bookkeeping, not
# something that needs to be in git history.
BACKLOG_FILE = os.path.join(SECRETS_DIR, "backlog_scan.json")

DEFAULT_SECRETS = {
    "telegram_bot_token": "",
    "github_token": "",
    # For the backlog scanner (Telethon, personal account login) — a
    # different, more sensitive credential than the bot token above, but
    # stored the same protected way, outside the git repo.
    "telegram_api_id": "",
    "telegram_api_hash": "",
    "telegram_phone": "",
    # Cloudflare R2 (image hosting for covers) — same protected treatment
    # as every other credential here: stored outside the repo, never
    # committed, and masked when echoed back to the dashboard.
    "r2_access_key_id": "",
    "r2_secret_access_key": "",
    "r2_account_id": "",
    "r2_bucket_name": "",
    "r2_public_url": "",
}

# Ensure dirs exist
os.makedirs(COVERS_DIR, exist_ok=True)
os.makedirs(WORKS_DIR, exist_ok=True)

# Config template (non-sensitive settings only — safe to sit inside the repo)
DEFAULT_CONFIG = {
    "channel_username": "@ArcComic",
    "site_domain": "https://nhentai.net",
    "posts_per_page": 15,
    "github_repo": "ArcComic/arccomic.github.io",
    "last_message_id": 0,
    "google_verification_tag": "",
    "auto_ping_google": True,
    "use_indexnow": True,
    "indexnow_key": ""
}

def load_secrets():
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, 'r') as f:
            return json.load(f)
    return DEFAULT_SECRETS.copy()

def save_secrets(secrets):
    with open(SECRETS_FILE, 'w') as f:
        json.dump(secrets, f, indent=2)
    # Lock down permissions so only this app's user can read it
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except Exception:
        pass

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
    else:
        cfg = DEFAULT_CONFIG.copy()
    # Merge in secrets for convenience when code reads a single "cfg" dict.
    # Secrets are never written back to CONFIG_FILE — see save_config().
    cfg.update(load_secrets())
    return cfg

def save_config(cfg):
    """Splits cfg into secrets (outside repo) and settings (inside repo)."""
    secrets = {
        "telegram_bot_token": cfg.get("telegram_bot_token", ""),
        "github_token": cfg.get("github_token", ""),
        "telegram_api_id": cfg.get("telegram_api_id", ""),
        "telegram_api_hash": cfg.get("telegram_api_hash", ""),
        "telegram_phone": cfg.get("telegram_phone", ""),
        "r2_access_key_id": cfg.get("r2_access_key_id", ""),
        "r2_secret_access_key": cfg.get("r2_secret_access_key", ""),
        "r2_account_id": cfg.get("r2_account_id", ""),
        "r2_bucket_name": cfg.get("r2_bucket_name", ""),
        "r2_public_url": cfg.get("r2_public_url", ""),
    }
    save_secrets(secrets)

    settings = {k: v for k, v in cfg.items() if k not in secrets}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

# ============== JEKYLL CONFIG AUTO-FIX ==============
CONFIG_YML_PATH = os.path.join(WORK_DIR, "_config.yml")

def ensure_jekyll_works_collection():
    """
    Makes sure _config.yml has a 'works' collection configured. Without
    this, files written to _works/*.md by generate_md() are never turned
    into real pages by Jekyll — they just sit as raw markdown and every
    post URL 404s. This runs automatically on every bot startup so it's
    a one-time invisible fix, not something that has to be hand-edited
    in a text editor on the phone.
    Returns True if it changed the file (so caller can push the change).
    """
    if not os.path.exists(CONFIG_YML_PATH):
        print("⚠️ _config.yml not found, skipping Jekyll auto-config")
        return False

    with open(CONFIG_YML_PATH, 'r', encoding='utf-8') as f:
        raw = f.read()

    try:
        cfg = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        print(f"⚠️ Could not parse _config.yml, leaving it untouched: {e}")
        return False

    changed = False

    # 1. Ensure the 'works' collection exists with correct output/permalink
    collections = cfg.get("collections") or {}
    works_cfg = collections.get("works") or {}
    if works_cfg.get("output") is not True or works_cfg.get("permalink") != "/works/:path/":
        collections["works"] = {"output": True, "permalink": "/works/:path/"}
        cfg["collections"] = collections
        changed = True

    # 1b. Ensure the 'tags' collection exists too, so tag browse pages
    # (auto-generated per unique tag by ensure_tag_pages()) build into
    # real, crawlable URLs the same way individual comics do.
    tags_cfg = collections.get("tags") or {}
    if tags_cfg.get("output") is not True or tags_cfg.get("permalink") != "/tags/:path/":
        collections["tags"] = {"output": True, "permalink": "/tags/:path/"}
        cfg["collections"] = collections
        changed = True

    # 1c. Ensure the 'artists' collection exists too, so artist pages
    # (auto-generated per unique author by regenerate_artist_pages())
    # build into real, crawlable URLs the same way tags do.
    artists_cfg = collections.get("artists") or {}
    if artists_cfg.get("output") is not True or artists_cfg.get("permalink") != "/artists/:path/":
        collections["artists"] = {"output": True, "permalink": "/artists/:path/"}
        cfg["collections"] = collections
        changed = True

    # 2. Ensure a default layout applies to the works collection
    defaults = cfg.get("defaults") or []
    has_works_default = any(
        isinstance(d, dict) and d.get("scope", {}).get("type") == "works"
        for d in defaults
    )
    if not has_works_default:
        defaults.append({
            "scope": {"path": "", "type": "works"},
            "values": {"layout": "post"}
        })
        cfg["defaults"] = defaults
        changed = True

    # 2b. Same for tags
    has_tags_default = any(
        isinstance(d, dict) and d.get("scope", {}).get("type") == "tags"
        for d in defaults
    )
    if not has_tags_default:
        defaults.append({
            "scope": {"path": "", "type": "tags"},
            "values": {"layout": "tag"}
        })
        cfg["defaults"] = defaults
        changed = True

    # 2c. Same for artists
    has_artists_default = any(
        isinstance(d, dict) and d.get("scope", {}).get("type") == "artists"
        for d in defaults
    )
    if not has_artists_default:
        defaults.append({
            "scope": {"path": "", "type": "artists"},
            "values": {"layout": "artist"}
        })
        cfg["defaults"] = defaults
        changed = True

    if changed:
        with open(CONFIG_YML_PATH, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        print("🔧 _config.yml updated: added 'works'/'tags'/'artists' collections so pages build correctly")

    return changed

POST_LAYOUT_PATH = os.path.join(WORK_DIR, "_layouts", "post.html")
POST_LAYOUT_VERSION = 9  # bump when the template below changes materially

# Native ads (Mondiad "Native" zone type) were removed sitewide — they
# render as in-flow content the network fully controls, and were both
# breaking grid layouts on load and misleading readers by visually
# impersonating a real comic card. Banner is the only ad format used now:
# a fixed rectangular slot that looks like an ad, styled by us, sized by us.
BANNER_AD_ZONE_ID = "6682c345-631e-456b-82ef-cc3bd9dbb29a"

POST_LAYOUT_TEMPLATE = f"""<!-- arc-comic-layout-version: {POST_LAYOUT_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{{{ page.title }}}} - Arc Comic</title>
    <meta name="description" content="{{{{ page.title }}}} by {{{{ page.author }}}} - Arc Comic">
    <script async src="https://ss.mrmnd.com/banner.js"></script>
    <style>
        :root {{
            --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230;
            --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0;
            --border: #2a2a3a;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: var(--bg); color: var(--text); min-height: 100vh;
        }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
        .logo-link {{ display: inline-flex; align-items: center; gap: 8px; text-decoration: none;
            color: var(--accent); font-weight: 800; font-size: 18px; margin-bottom: 16px; }}
        .breadcrumb {{
            color: var(--text-muted); font-size: 14px; margin-bottom: 20px;
        }}
        .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
        .breadcrumb a:hover {{ text-decoration: underline; }}
        .meta-row {{ display: flex; align-items: center; gap: 14px; color: var(--text-muted);
            font-size: 13px; margin-bottom: 12px; flex-wrap: wrap; }}
        .cover {{
            width: 100%; max-width: 400px; border-radius: 12px; display: block;
            margin: 0 auto 24px; border: 1px solid var(--border); position: relative;
        }}
        .rating-badge {{
            position: absolute; top: 12px; right: 12px; background: var(--bg);
            color: var(--accent); padding: 6px 14px; border-radius: 20px;
            font-weight: 800; font-size: 14px;
        }}
        h1 {{ font-size: 26px; font-weight: 800; margin-bottom: 20px; line-height: 1.3; }}
        .info-grid {{
            display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px;
        }}
        .info-box {{
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; padding: 14px; display: flex; align-items: center; gap: 12px;
        }}
        .info-icon {{
            width: 36px; height: 36px; border-radius: 10px; background: var(--bg-elevated);
            display: flex; align-items: center; justify-content: center; font-size: 18px;
            flex-shrink: 0;
        }}
        .info-label {{
            color: var(--text-muted); font-size: 11px; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 2px;
        }}
        .info-value {{ font-size: 16px; font-weight: 700; }}
        .tags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 24px; }}
        .tag {{
            background: var(--bg-elevated); color: var(--text-muted);
            padding: 6px 14px; border-radius: 20px; font-size: 13px;
            text-decoration: none; border: 1px solid var(--border);
        }}
        .tag:hover {{ border-color: var(--accent); color: var(--accent); }}
        .ad-slot {{
            margin: 24px 0; display: flex; justify-content: center; align-items: center;
            min-height: 100px; max-height: 300px; width: 100%;
            background: var(--bg-card); border-radius: 12px;
            border: 1px solid var(--border); overflow: hidden;
        }}
        /* Mondiad's injected creative can be an <img>, <iframe>, or nested
           <div>s depending on the ad served — without this, a large raw
           image creative (e.g. a "claim your reward" banner) renders at
           its native pixel size and blows the slot out, breaking the
           whole page's layout around it. Forcing every possible child to
           respect the slot's own box keeps this contained no matter what
           creative gets served. */
        .ad-slot > * {{ max-width: 100% !important; max-height: 100% !important;
            width: auto !important; height: auto !important; }}
        .ad-slot img, .ad-slot iframe {{ object-fit: contain; }}
        .read-btn {{
            display: flex; align-items: center; justify-content: center; gap: 10px;
            text-align: center; background: var(--accent);
            color: #000; font-weight: 700; padding: 16px; border-radius: 12px;
            text-decoration: none; font-size: 16px; border: none; width: 100%;
            cursor: pointer; transition: background 0.15s, color 0.15s;
        }}
        .read-btn:hover, .read-btn:active {{ background: #000; color: #fff; }}
        .read-btn svg {{ width: 20px; height: 20px; fill: currentColor; flex-shrink: 0; }}
        .join-notice {{
            font-size: 12px; color: var(--text-muted); text-align: center;
            margin-bottom: 8px; line-height: 1.4;
        }}
        .join-notice a {{ color: var(--accent); }}
        .or-divider {{
            display: flex; align-items: center; gap: 12px;
            margin: 14px 0; color: var(--text-muted); font-weight: 800; font-size: 15px;
        }}
        .or-divider::before, .or-divider::after {{
            content: ""; flex: 1; height: 1px; background: var(--border);
        }}
        .similar-section {{ margin-top: 40px; }}
        .similar-title {{
            font-size: 18px; font-weight: 800; margin-bottom: 16px;
            display: flex; align-items: center; gap: 8px;
        }}
        .similar-grid {{
            display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;
        }}
        .similar-card {{
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; overflow: hidden; text-decoration: none; color: var(--text);
        }}
        .similar-cover {{ width: 100%; aspect-ratio: 3/4; object-fit: cover; display: block;
            background: var(--bg-elevated); }}
        .similar-info {{ padding: 10px; }}
        .similar-info-title {{ font-size: 13px; font-weight: 700; line-height: 1.3;
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
        .mini-search {{ display: flex; gap: 8px; margin-bottom: 20px; }}
        .mini-search input {{
            flex: 1; padding: 10px 14px; background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; color: var(--text); font-size: 13px; outline: none;
        }}
        .mini-search input:focus {{ border-color: var(--accent); }}
        .mini-search button {{
            background: var(--accent); color: #000; border: none; border-radius: 10px;
            padding: 0 16px; font-weight: 700; font-size: 13px; cursor: pointer;
        }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="logo-link">✨ Arc Comic</a>

        <div class="mini-search">
            <input type="text" placeholder="Search comics..." id="miniSearchInput">
            <button id="miniSearchBtn">🔍</button>
        </div>

        <div class="breadcrumb">
            <a href="/">Home</a> ›
            <a href="/?category={{{{ page.categories | first | url_encode }}}}">{{{{ page.categories | first | capitalize }}}}</a> ›
            {{{{ page.title }}}}
        </div>

        <div class="cover">
            <img src="{{{{ page.cover }}}}" alt="{{{{ page.title }}}}" style="width:100%;display:block;border-radius:12px;">
            <span class="rating-badge">⭐ {{{{ page.rating }}}}</span>
        </div>

        <div class="meta-row">
            <span id="viewCount">👁 -- views</span>
            <span>📅 {{{{ page.date | date: "%B %d, %Y" }}}}</span>
        </div>

        <h1>{{{{ page.title }}}}</h1>

        <div class="info-grid">
            <div class="info-box">
                <div class="info-icon">✨</div>
                <div><div class="info-label">Author</div><div class="info-value"><a href="/artists/{{{{ page.author | slugify }}}}/" style="color:inherit;text-decoration:none;">{{{{ page.author }}}}</a></div></div>
            </div>
            <div class="info-box">
                <div class="info-icon">🌟</div>
                <div><div class="info-label">Code</div><div class="info-value">{{{{ page.code }}}}</div></div>
            </div>
            <div class="info-box">
                <div class="info-icon">⚡</div>
                <div><div class="info-label">Category</div><div class="info-value">{{{{ page.categories | join: ", " | capitalize }}}}</div></div>
            </div>
            <div class="info-box">
                <div class="info-icon">🌐</div>
                <div><div class="info-label">Language</div><div class="info-value">{{{{ page.language | capitalize }}}}</div></div>
            </div>
            <div class="info-box">
                <div class="info-icon">🎨</div>
                <div><div class="info-label">Full Color</div><div class="info-value">{{% if page.full_color %}}Yes{{% else %}}No{{% endif %}}</div></div>
            </div>
            <div class="info-box">
                <div class="info-icon">🌙</div>
                <div><div class="info-label">NTR</div><div class="info-value">{{% if page.ntr %}}Yes{{% else %}}No{{% endif %}}</div></div>
            </div>
        </div>

        <div class="tags">
            {{% for tag in page.tags %}}
            <a href="/tags/{{{{ tag | slugify }}}}/" class="tag">{{{{ tag }}}}</a>
            {{% endfor %}}
        </div>

        <div class="ad-slot" data-mndbanid="{BANNER_AD_ZONE_ID}"></div>

        <div class="join-notice">{{{{ site.data.site_meta.join_notice_html }}}}</div>
        <a href="https://t.me/ArcComicBot?start={{{{ page.code }}}}" class="read-btn" target="_blank" id="readBotBtn">
            <svg viewBox="0 0 24 24"><path d="M21.94 4.2a1.5 1.5 0 00-1.53-.24L2.7 10.9a1.4 1.4 0 00.1 2.63l4.55 1.42 1.75 5.6a1.4 1.4 0 002.3.57l2.6-2.36 4.68 3.46a1.4 1.4 0 002.23-.85l3.05-15.1a1.5 1.5 0 00-.02-.07zM8.5 14.3l9.3-6.9c.2-.15.4.1.24.28l-7.6 7.4-.3 3.2-1.64-3.98z"/></svg>
            Read Now On Telegram
        </a>

        <div class="or-divider">OR</div>

        <a href="{{{{ page.telegram_post }}}}" class="read-btn" target="_blank" id="readBtn">
            <svg viewBox="0 0 24 24"><path d="M21.94 4.2a1.5 1.5 0 00-1.53-.24L2.7 10.9a1.4 1.4 0 00.1 2.63l4.55 1.42 1.75 5.6a1.4 1.4 0 002.3.57l2.6-2.36 4.68 3.46a1.4 1.4 0 002.23-.85l3.05-15.1a1.5 1.5 0 00-.02-.07zM8.5 14.3l9.3-6.9c.2-.15.4.1.24.28l-7.6 7.4-.3 3.2-1.64-3.98z"/></svg>
            Read Now
        </a>

        {{% assign this_tags = page.tags %}}
        {{% assign candidates = "" | split: "" %}}
        {{% for work in site.works %}}
            {{% if work.code != page.code %}}
                {{% assign overlap = 0 %}}
                {{% for t in work.tags %}}
                    {{% if this_tags contains t %}}{{% assign overlap = overlap | plus: 1 %}}{{% endif %}}
                {{% endfor %}}
                {{% if overlap > 0 %}}
                    {{% assign candidates = candidates | push: work %}}
                {{% endif %}}
            {{% endif %}}
        {{% endfor %}}

        <div class="similar-section">
            <div class="similar-title">📖 Similar Comics</div>
            <div class="similar-grid">
                {{% assign shown_count = 0 %}}
                {{% assign shown_series = "" | split: "," %}}
                {{% for work in candidates limit: 20 %}}
                    {{% if shown_count >= 4 %}}{{% break %}}{{% endif %}}
                    {{% assign base_title = work.title | split: " " | first %}}
                    {{% unless shown_series contains base_title %}}
                        {{% assign shown_series = shown_series | push: base_title %}}
                        {{% assign shown_count = shown_count | plus: 1 %}}
                        <a href="/works/{{{{ work.code }}}}/" class="similar-card">
                            <img src="{{{{ work.cover }}}}" alt="{{{{ work.title }}}}" class="similar-cover">
                            <div class="similar-info">
                                <div class="similar-info-title">{{{{ work.title }}}}</div>
                            </div>
                        </a>
                    {{% endunless %}}
                {{% endfor %}}
            </div>
        </div>

        <div class="ad-slot" data-mndbanid="{BANNER_AD_ZONE_ID}"></div>

        {{% include follow_us.html %}}
    </div>

    <script>
        function goMiniSearch() {{
            var q = document.getElementById('miniSearchInput').value.trim();
            if (q) window.location.href = '/search/?q=' + encodeURIComponent(q);
        }}
        document.getElementById('miniSearchBtn').addEventListener('click', goMiniSearch);
        document.getElementById('miniSearchInput').addEventListener('keydown', function(e) {{
            if (e.key === 'Enter') goMiniSearch();
        }});

        // View counter. The original api.countapi.xyz domain is dead (shut
        // down, SSL expired) — this was why the view counter never showed
        // anything. Using the actively maintained community replacement
        // instead. Same request shape, drop-in swap.
        (function() {{
            var code = "{{{{ page.code }}}}";
            fetch("https://countapi.mileshilliard.com/api/v1/hit/arccomic-github-io_" + code)
                .then(function(r) {{ return r.json(); }})
                .then(function(data) {{
                    document.getElementById("viewCount").textContent = "👁 " + data.value + " views";
                }})
                .catch(function() {{
                    document.getElementById("viewCount").textContent = "";
                }});
        }})();
    </script>
</body>
</html>
"""

def ensure_post_layout():
    """
    Writes/upgrades _layouts/post.html automatically. The bot fully owns
    this file — it always keeps it at the latest version. (An earlier
    version of this function tried to detect "hand-edited" files by
    checking for a version marker and skipping files without one, but
    that incorrectly treated every file written before the marker system
    existed as permanently off-limits, silently blocking all future
    upgrades. Since this file is never meant to be hand-edited, the bot
    now simply always keeps it current.)
    Returns True if it wrote/changed the file.
    """
    os.makedirs(os.path.dirname(POST_LAYOUT_PATH), exist_ok=True)

    if os.path.exists(POST_LAYOUT_PATH):
        with open(POST_LAYOUT_PATH, 'r', encoding='utf-8') as f:
            first_line = f.readline()
        match = re.search(r"arc-comic-layout-version:\s*(\d+)", first_line)
        current_version = int(match.group(1)) if match else 0
        if current_version >= POST_LAYOUT_VERSION:
            return False  # already up to date

    with open(POST_LAYOUT_PATH, 'w', encoding='utf-8') as f:
        f.write(POST_LAYOUT_TEMPLATE)
    print(f"🔧 _layouts/post.html updated to v{POST_LAYOUT_VERSION} "
          f"(clean layout, no emoji tags, proper Yes/No labels)")
    return True

# ============== SITE TAGLINE ("site_meta") — dashboard-editable, sponsorable ==============
# Lives in _data/ like social_links.json, so Jekyll reads it directly at build
# time via site.data.site_meta — no Python string-substitution into the big
# INDEX_HTML_TEMPLATE f-string is needed, and it self-heals the same way
# everything else in this file does (see ensure_site_meta below).
SITE_META_FILE = os.path.join(WORK_DIR, "_data", "site_meta.json")

DEFAULT_TAGLINE = "Art & Story — only 4🌟 Manga & Doujinshi Gallery"
# Shown on the reading page, right above the "Read Now On Telegram" button —
# reminds readers they must join the mandatory channel(s) before the bot's
# ?start= deep-link will work for them the first time. Dashboard-editable via
# the same (linktext:url) mini-syntax as the homepage tagline, e.g. so Master
# can link the actual channel(s) to join without hand-editing any template.
DEFAULT_JOIN_NOTICE = "(Join Mandatory Telegram channels to Activate Bot first time)"
DEFAULT_SITE_META = {"tagline": DEFAULT_TAGLINE, "join_notice": DEFAULT_JOIN_NOTICE}

# Sponsor-link mini-syntax: (linktext:url) anywhere inside the tagline becomes
# a clickable link, e.g. "Sponsored by (MangaHost:https://example.com) this week"
# renders "Sponsored by MangaHost this week" with "MangaHost" as a link.
# Only http:// and https:// URLs are accepted — anything else (javascript:,
# data:, etc.) is left as plain literal text instead of becoming a link, since
# this text is typed into the dashboard by Master and could in principle be
# copy-pasted from anywhere.
TAGLINE_LINK_PATTERN = re.compile(r"\(([^():]+):(https?://[^\s()]+)\)")

def render_tagline_html(raw_text):
    """Turns 'text (linktext:https://url) more text' into safe HTML with a
    real <a> tag for the (linktext:url) part, and the rest HTML-escaped.
    Any (linktext:url)-shaped chunk with a non-http(s) URL is left as literal
    text rather than becoming a link."""
    parts = []
    last_end = 0
    for m in TAGLINE_LINK_PATTERN.finditer(raw_text):
        parts.append(html.escape(raw_text[last_end:m.start()]))
        link_text, url = m.group(1), m.group(2)
        parts.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="sponsored noopener noreferrer">{html.escape(link_text)}</a>'
        )
        last_end = m.end()
    parts.append(html.escape(raw_text[last_end:]))
    return "".join(parts)

def load_site_meta():
    meta = None
    if os.path.exists(SITE_META_FILE):
        try:
            with open(SITE_META_FILE, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            # Corrupted file (bad JSON, truncated write, etc.) — fall back
            # to defaults instead of crashing, same reasoning as the Jekyll
            # front-matter self-heal checks elsewhere in this file.
            meta = None
    if not isinstance(meta, dict):
        meta = dict(DEFAULT_SITE_META)
    if "tagline" not in meta or not str(meta["tagline"]).strip():
        meta["tagline"] = DEFAULT_TAGLINE
    if "join_notice" not in meta or not str(meta["join_notice"]).strip():
        meta["join_notice"] = DEFAULT_JOIN_NOTICE
    return meta

def save_site_meta(meta):
    os.makedirs(os.path.dirname(SITE_META_FILE), exist_ok=True)
    tagline = str(meta.get("tagline", "")).strip() or DEFAULT_TAGLINE
    join_notice = str(meta.get("join_notice", "")).strip() or DEFAULT_JOIN_NOTICE
    data = {
        "tagline": tagline,
        "tagline_html": render_tagline_html(tagline),
        "join_notice": join_notice,
        "join_notice_html": render_tagline_html(join_notice),
    }
    with open(SITE_META_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    return data

def ensure_site_meta():
    """Self-healing: makes sure _data/site_meta.json exists with valid
    tagline_html and join_notice_html fields even if the file was never
    created or got corrupted, same reasoning as ensure_follow_us_include()'s
    use of social_links.json."""
    if os.path.exists(SITE_META_FILE):
        try:
            with open(SITE_META_FILE, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            if meta.get("tagline") and meta.get("tagline_html") \
                    and meta.get("join_notice") and meta.get("join_notice_html"):
                return False  # already fine
        except Exception:
            pass
    save_site_meta(load_site_meta())
    print("🔧 _data/site_meta.json created/repaired with default tagline/join notice")
    return True

# ============== SOCIAL LINKS ("Follow Us") ==============
def load_social_links():
    if os.path.exists(SOCIAL_LINKS_FILE):
        with open(SOCIAL_LINKS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return list(DEFAULT_SOCIAL_LINKS)

def save_social_links(links):
    os.makedirs(os.path.dirname(SOCIAL_LINKS_FILE), exist_ok=True)
    with open(SOCIAL_LINKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(links, f, indent=2)

# Small inline SVGs so the site has zero external icon dependencies (no
# extra requests, nothing that can break if a CDN goes down).
SOCIAL_ICONS = {
    "telegram": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M21.94 4.2a1.5 1.5 0 00-1.53-.24L2.7 10.9a1.4 1.4 0 00.1 2.63l4.55 1.42 1.75 5.6a1.4 1.4 0 002.3.57l2.6-2.36 4.68 3.46a1.4 1.4 0 002.23-.85l3.05-15.1a1.5 1.5 0 00-.02-.07zM8.5 14.3l9.3-6.9c.2-.15.4.1.24.28l-7.6 7.4-.3 3.2-1.64-3.98z"/></svg>',
    "youtube": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M23.5 6.2a3 3 0 00-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 00.5 6.2 31 31 0 000 12a31 31 0 00.5 5.8 3 3 0 002.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 002.1-2.1A31 31 0 0024 12a31 31 0 00-.5-5.8zM9.6 15.6V8.4l6.3 3.6-6.3 3.6z"/></svg>',
    "facebook": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M22 12a10 10 0 10-11.6 9.9v-7H7.9V12h2.5V9.8c0-2.5 1.5-3.9 3.8-3.9 1.1 0 2.2.2 2.2.2v2.5h-1.3c-1.2 0-1.6.8-1.6 1.6V12h2.8l-.4 2.9h-2.4v7A10 10 0 0022 12z"/></svg>',
    "twitter": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.9 2H22l-7.2 8.3L23.3 22h-6.6l-5.2-6.8L5.6 22H2.4l7.7-8.8L1 2h6.8l4.7 6.2zm-1.2 18h1.7L7.4 4H5.6z"/></svg>',
    "website": '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm7.9 9h-3.2c-.1-2.1-.6-4-1.3-5.4A8 8 0 0119.9 11zm-9-6.9c1 .8 2.2 3 2.4 6.9H9.6c.2-3.9 1.4-6.1 2.4-6.9zM9.6 13h4.8c-.2 3.9-1.4 6.1-2.4 6.9-1-.8-2.2-3-2.4-6.9zm-2 0c.1 2.1.6 4 1.3 5.4A8 8 0 014.1 13zm0-2A8 8 0 018.9 5.6c-.7 1.4-1.2 3.3-1.3 5.4zm7.9 8.4c.7-1.4 1.2-3.3 1.3-5.4h3.2a8 8 0 01-4.5 5.4z"/></svg>',
}

FOLLOW_US_INCLUDE_PATH = os.path.join(WORK_DIR, "_includes", "follow_us.html")
FOLLOW_US_VERSION = 1

def ensure_follow_us_include():
    """
    Self-healing _includes/follow_us.html — a Jekyll include so the "Follow
    for more" block can appear on every page from one shared source, and
    stays in sync with whatever links are set in the dashboard (stored in
    _data/social_links.json, which Jekyll makes available as site.data
    automatically). The bot fully owns this file and always keeps it
    current — see ensure_post_layout() for why "protect unmarked files"
    was removed.
    """
    os.makedirs(os.path.dirname(FOLLOW_US_INCLUDE_PATH), exist_ok=True)

    if os.path.exists(FOLLOW_US_INCLUDE_PATH):
        with open(FOLLOW_US_INCLUDE_PATH, 'r', encoding='utf-8') as f:
            first_line = f.readline()
        match = re.search(r"arc-comic-include-version:\s*(\d+)", first_line)
        current_version = int(match.group(1)) if match else 0
        if current_version >= FOLLOW_US_VERSION:
            return False

    icons_json = json.dumps(SOCIAL_ICONS)
    template = f"""<!-- arc-comic-include-version: {FOLLOW_US_VERSION} -->
<div class="follow-us-section">
    <div class="follow-us-title">⚡ FOLLOW FOR MORE</div>
    <div class="follow-us-list">
        {{% for link in site.data.social_links %}}
        <a href="{{{{ link.url }}}}" class="follow-us-item" target="_blank" rel="noopener">
            <span class="follow-us-icon follow-us-icon-{{{{ link.icon }}}}"></span>
            <span class="follow-us-text">
                <span class="follow-us-label">{{{{ link.label }}}}</span>
                <span class="follow-us-sublabel">{{{{ link.sublabel }}}}</span>
            </span>
            <span class="follow-us-arrow">↗</span>
        </a>
        {{% endfor %}}
    </div>
</div>
<style>
    .follow-us-section {{ max-width: 900px; margin: 40px auto; padding: 0 24px; }}
    .follow-us-title {{
        color: #8888a0; font-size: 12px; font-weight: 700; letter-spacing: 1px;
        margin-bottom: 12px; border-bottom: 1px solid #2a2a3a; padding-bottom: 10px;
    }}
    .follow-us-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .follow-us-item {{
        display: flex; align-items: center; gap: 14px; background: #1a1a24;
        border: 1px solid #2a2a3a; border-radius: 14px; padding: 14px 16px;
        text-decoration: none; color: inherit; transition: border-color 0.15s;
    }}
    .follow-us-item:hover {{ border-color: #f59e0b; }}
    .follow-us-icon {{
        width: 38px; height: 38px; border-radius: 10px; flex-shrink: 0;
        display: flex; align-items: center; justify-content: center;
        background: #f59e0b; color: #000;
    }}
    .follow-us-icon svg {{ width: 20px; height: 20px; }}
    .follow-us-icon-telegram {{ background: #2AABEE; color: #fff; }}
    .follow-us-icon-youtube {{ background: #FF0000; color: #fff; }}
    .follow-us-icon-facebook {{ background: #1877F2; color: #fff; }}
    .follow-us-icon-twitter {{ background: #000; color: #fff; }}
    .follow-us-icon-website {{ background: #f59e0b; color: #000; }}
    .follow-us-text {{ flex: 1; min-width: 0; }}
    .follow-us-label {{ display: block; font-weight: 700; font-size: 15px; color: #e2e2e8; }}
    .follow-us-sublabel {{ display: block; font-size: 12px; color: #8888a0; margin-top: 2px; }}
    .follow-us-arrow {{ color: #666; font-size: 16px; }}
</style>
<script>
document.querySelectorAll('.follow-us-icon').forEach(function(el) {{
    var icons = {icons_json};
    var cls = Array.from(el.classList).find(function(c) {{ return c.startsWith('follow-us-icon-'); }});
    if (cls) {{
        var key = cls.replace('follow-us-icon-', '');
        if (icons[key]) el.innerHTML = icons[key];
    }}
}});
</script>
"""
    with open(FOLLOW_US_INCLUDE_PATH, 'w', encoding='utf-8') as f:
        f.write(template)
    print(f"🔧 _includes/follow_us.html updated to v{FOLLOW_US_VERSION}")
    return True

# ============== FAVICON ==============
FAVICON_PATH = os.path.join(WORK_DIR, "favicon.svg")
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<rect width="100" height="100" rx="20" fill="#0f0f13"/>
<path d="M50 15 L58 42 L85 50 L58 58 L50 85 L42 58 L15 50 L42 42 Z" fill="#f59e0b"/>
</svg>"""

def ensure_favicon():
    """
    Generates a small SVG favicon matching the site's ✨ sparkle branding.
    Without this, Chrome shows a generic placeholder icon in the address
    bar (the "ugly logo" in the top corner) — that placeholder isn't
    coming from anything on the site, it's just what browsers show when
    no favicon is defined at all. SVG is used instead of PNG so this
    needs no image library (no Pillow) on Termux.
    """
    if os.path.exists(FAVICON_PATH):
        return False
    with open(FAVICON_PATH, 'w', encoding='utf-8') as f:
        f.write(FAVICON_SVG)
    print("🔧 favicon.svg created")
    return True

# ============== SHARED: PAGINATION JS ==============
# Used by the homepage, tag pages, search page, and artist pages — all
# four independently render a page-number strip and previously each had
# their own copy of a naive "print every page number" loop, which broke
# visually on mobile once a listing had more than ~10-15 pages (see the
# bug report: with 17 pages, all 17 numbers rendered in one row and
# overflowed/wrapped badly on narrow screens). This single shared
# function replaces all four copies so future changes only need to
# happen once. Renders a condensed strip: first page, last page, the
# pages immediately around the current page, and "…" ellipsis gaps in
# between — the same pattern most pagination UIs use (e.g. "1 … 6 7 8
# 9 10 … 42"). MAX_PAGINATION_BUTTONS below controls how many numbered
# buttons are shown at once (not counting the two ellipsis "…" spacers);
# Master asked for 7, this defaults to that.
PAGINATION_JS = """
        const MAX_PAGINATION_BUTTONS = 7;
        function buildPaginationHtml(current, total, onClickFnName) {
            if (total <= 1) return '';
            const mk = (i) => {
                if (i === current) return `<span>${i}</span>`;
                return `<a href="#" onclick="${onClickFnName}(${i}); return false;">${i}</a>`;
            };
            const ellipsis = `<span class="pagination-ellipsis">…</span>`;

            if (total <= MAX_PAGINATION_BUTTONS) {
                let html = '';
                for (let i = 1; i <= total; i++) html += mk(i);
                return html;
            }

            // Always show first and last page. Fill the remaining slots
            // centered on the current page, then clamp to valid range.
            const sideSlots = MAX_PAGINATION_BUTTONS - 2; // minus first+last
            let start = current - Math.floor(sideSlots / 2);
            let end = start + sideSlots - 1;
            if (start < 2) { start = 2; end = start + sideSlots - 1; }
            if (end > total - 1) { end = total - 1; start = end - sideSlots + 1; }
            if (start < 2) start = 2;

            let html = mk(1);
            if (start > 2) html += ellipsis;
            for (let i = start; i <= end; i++) html += mk(i);
            if (end < total - 1) html += ellipsis;
            html += mk(total);
            return html;
        }
"""

# ============== HOMEPAGE (index.html) ==============
INDEX_HTML_PATH = os.path.join(WORK_DIR, "index.html")
INDEX_HTML_VERSION = 14  # bump when the template below changes materially

INDEX_HTML_TEMPLATE = f"""---
# No 'layout:' key here on purpose — index.html is a complete, self-contained
# page (own <head>, <style>, <body>) and does not need a wrapper layout.
# An earlier version referenced 'layout: default', but no _layouts/default.html
# file was ever created, which caused Jekyll to render broken/unstyled
# leftover content at the top of the page (the "duplicate blue logo" bug).
# The empty front matter block below is still required so Jekyll processes
# the Liquid tags (site.google_verification, site.works, etc.) in this file.
---
<!-- arc-comic-index-version: {INDEX_HTML_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {{% if site.google_verification != "" %}}
    {{{{ site.google_verification }}}}
    {{% endif %}}
    <title>✨ Arc Comic — Manga & Doujinshi Gallery</title>
    <meta name="description" content="Arc Comic — Curated manga and doujinshi gallery">
    <style>
        :root {{
            --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230;
            --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0;
            --border: #2a2a3a;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: var(--bg); color: var(--text); min-height: 100vh;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .header {{
            text-align: center; padding: 40px 20px;
            border-bottom: 1px solid var(--border); margin-bottom: 30px;
        }}
        .header a.logo-link {{ text-decoration: none; }}
        .header h1 {{
            font-size: 42px; font-weight: 800;
            background: linear-gradient(135deg, var(--accent), #f97316);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }}
        .header p {{ color: var(--text-muted); font-size: 16px; margin-top: 10px; }}
        .search-box {{
            max-width: 500px; margin: 0 auto 30px;
            display: flex; gap: 8px; position: relative;
        }}
        .search-box input {{
            flex: 1; padding: 14px 20px 14px 48px;
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; color: var(--text); font-size: 15px;
            outline: none;
        }}
        .search-box input:focus {{ border-color: var(--accent); }}
        .search-box .search-icon {{
            position: absolute; left: 16px; top: 50%;
            transform: translateY(-50%); font-size: 18px; pointer-events: none;
        }}
        .search-box button {{
            background: var(--accent); color: #000; border: none;
            border-radius: 12px; padding: 0 22px; font-weight: 700;
            font-size: 14px; cursor: pointer;
        }}
        .section-title {{
            font-size: 20px; font-weight: 700; margin-bottom: 16px;
            display: flex; align-items: center; gap: 10px;
        }}
        .section-title .badge {{
            background: var(--accent); color: #000;
            padding: 4px 10px; border-radius: 20px;
            font-size: 12px; font-weight: 700;
        }}
        .toolbar {{
            display: flex; gap: 10px; margin-bottom: 20px; flex-wrap: wrap;
        }}
        .toolbar select {{
            background: var(--bg-card); color: var(--text); border: 1px solid var(--border);
            border-radius: 10px; padding: 10px 14px; font-size: 13px; cursor: pointer;
        }}
        .filter-panel {{
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
            padding: 16px; margin-bottom: 20px; display: none; gap: 20px; flex-wrap: wrap;
        }}
        .filter-panel.open {{ display: flex; }}
        .filter-group {{ display: flex; flex-direction: column; gap: 8px; }}
        .filter-group-label {{ font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
        .filter-option {{ display: flex; align-items: center; gap: 6px; font-size: 13px; }}
        .popular-grid {{
            display: grid; grid-template-columns: repeat(4, 1fr);
            gap: 16px; margin-bottom: 40px;
        }}
        .popular-card {{
            background: var(--bg-card); border-radius: 12px;
            border: 1px solid var(--border); overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s; cursor: pointer;
            text-decoration: none; color: var(--text);
        }}
        .popular-card:hover {{
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.4);
        }}
        .popular-card .cover {{
            width: 100%; padding-top: 140%; position: relative;
            background: var(--bg-elevated);
        }}
        .popular-card .cover img {{
            position: absolute; top: 0; left: 0;
            width: 100%; height: 100%; object-fit: cover;
        }}
        .popular-card .info {{ padding: 12px; }}
        .popular-card .info h3 {{
            font-size: 14px; font-weight: 600;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
        .popular-card .info .meta {{
            font-size: 12px; color: var(--text-muted); margin-top: 4px;
        }}
        .works-grid {{
            display: grid; grid-template-columns: repeat(5, 1fr);
            gap: 16px; margin-bottom: 30px;
        }}
        .work-card {{
            background: var(--bg-card); border-radius: 12px;
            border: 1px solid var(--border); overflow: hidden;
            transition: transform 0.2s; cursor: pointer;
            text-decoration: none; color: var(--text);
        }}
        .work-card:hover {{ transform: translateY(-4px); }}
        .work-card .cover {{
            width: 100%; padding-top: 140%; position: relative;
            background: var(--bg-elevated);
        }}
        .work-card .cover img {{
            position: absolute; top: 0; left: 0;
            width: 100%; height: 100%; object-fit: cover;
        }}
        .work-card .info {{ padding: 10px; }}
        .work-card .info h3 {{
            font-size: 13px; font-weight: 600;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }}
        .work-card .info .meta {{
            font-size: 11px; color: var(--text-muted); margin-top: 3px;
        }}
        .no-results {{ text-align: center; padding: 60px 20px; color: var(--text-muted); }}
        .pagination {{
            display: flex; justify-content: center; gap: 8px; flex-wrap: wrap;
            margin-top: 30px; padding-top: 20px;
            border-top: 1px solid var(--border);
        }}
        .pagination a, .pagination span {{
            padding: 8px 14px; border-radius: 8px;
            font-size: 14px; font-weight: 600;
            text-decoration: none;
        }}
        .pagination a {{
            background: var(--bg-card); color: var(--text);
            border: 1px solid var(--border);
        }}
        .pagination a:hover {{ background: var(--accent); color: #000; }}
        .pagination span {{ background: var(--accent); color: #000; }}
        .pagination-ellipsis {{
            padding: 8px 6px; color: var(--text-muted);
            font-weight: 600; user-select: none;
        }}
        .footer {{
            text-align: center; padding: 30px;
            color: var(--text-muted); font-size: 13px;
            border-top: 1px solid var(--border); margin-top: 40px;
        }}
        @media (max-width: 768px) {{
            .popular-grid {{ grid-template-columns: repeat(2, 1fr); }}
            .works-grid {{ grid-template-columns: repeat(2, 1fr); }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="logo-link"><h1>✨ Arc Comic</h1></a>
            <p>{{{{ site.data.site_meta.tagline_html }}}}</p>
        </div>
        <div class="search-box">
            <span class="search-icon">🔍</span>
            <input type="text" placeholder="Search by title, author, or tag..." id="searchInput">
            <button id="searchBtn">Search</button>
        </div>
        <div style="text-align:center;margin-bottom:30px;display:flex;gap:20px;justify-content:center;">
            <a href="/tags/" style="color:var(--text-muted);font-size:13px;text-decoration:none;border-bottom:1px dashed var(--border);padding-bottom:2px;">
                💥 Browse All Tags →
            </a>
            <a href="/artists/" style="color:var(--text-muted);font-size:13px;text-decoration:none;border-bottom:1px dashed var(--border);padding-bottom:2px;">
                ✨ Browse Artists →
            </a>
        </div>
        <div id="popularSection">
            <h2 class="section-title">
                🔥 Popular Today <span class="badge">TOP 4</span>
            </h2>
            <div class="popular-grid" id="popularGrid"></div>
        </div>
        <h2 class="section-title">📚 Latest Upload</h2>
        <div class="toolbar">
            <select id="sortSelect">
                <option value="">Sort: Default</option>
                <option value="popular_today">Most Popular Today</option>
                <option value="popular_weekly">Most Popular This Week</option>
                <option value="popular_monthly">Most Popular This Month</option>
                <option value="popular_yearly">Most Popular This Year</option>
                <option value="recent">Most Recent</option>
                <option value="oldest">Oldest</option>
            </select>
            <button id="filterToggleBtn" style="background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-size:13px;cursor:pointer;">
                ⚙️ Filters
            </button>
        </div>
        <div class="filter-panel" id="filterPanel">
            <div class="filter-group">
                <div class="filter-group-label">Full Color</div>
                <label class="filter-option"><input type="checkbox" id="filterFullColor"> Full Color Only</label>
            </div>
            <div class="filter-group">
                <div class="filter-group-label">NTR</div>
                <label class="filter-option"><input type="radio" name="ntrFilter" value="" checked> Any</label>
                <label class="filter-option"><input type="radio" name="ntrFilter" value="yes"> NTR Yes</label>
                <label class="filter-option"><input type="radio" name="ntrFilter" value="no"> NTR No</label>
            </div>
        </div>
        <div class="works-grid" id="worksGrid"></div>
        <div class="no-results" id="noResults" style="display:none;">No comics match your search or filters.</div>
        <div class="pagination" id="pagination"></div>
        {{% include follow_us.html %}}
        <div class="footer">
            <p style="margin-top:8px">Daily updates</p>
        </div>
    </div>
    <script>
        {PAGINATION_JS}
        const works = [
            {{% for post in site.works %}}
            {{
                title: "{{{{ post.title | escape }}}}",
                author: "{{{{ post.author }}}}",
                code: "{{{{ post.code }}}}",
                cover: "{{{{ post.cover }}}}",
                rating: "{{{{ post.rating }}}}",
                date: "{{{{ post.date }}}}",
                tags: {{{{ post.tags | jsonify }}}},
                fullColor: {{{{ post.full_color | default: false }}}},
                ntr: {{{{ post.ntr | default: false }}}},
                url: "{{{{ post.url }}}}"
            }}{{% unless forloop.last %}},{{% endunless %}}
            {{% endfor %}}
        ];
        const POSTS_PER_PAGE = {{{{ site.paginate | default: 16 }}}};

        function applyFilters(list) {{
            const fullColorOnly = document.getElementById('filterFullColor').checked;
            const ntrValue = document.querySelector('input[name="ntrFilter"]:checked').value;
            return list.filter(w => {{
                if (fullColorOnly && !w.fullColor) return false;
                if (ntrValue === 'yes' && !w.ntr) return false;
                if (ntrValue === 'no' && w.ntr) return false;
                return true;
            }});
        }}

        function applySort(list, mode) {{
            const sorted = [...list];
            switch (mode) {{
                case 'recent':
                    return sorted.sort((a,b) => new Date(b.date) - new Date(a.date));
                case 'oldest':
                    return sorted.sort((a,b) => new Date(a.date) - new Date(b.date));
                case 'popular_today':
                case 'popular_weekly':
                case 'popular_monthly':
                case 'popular_yearly':
                    // Rating is the best available popularity proxy until
                    // a real view-window metric is tracked server-side.
                    return sorted.sort((a,b) => parseFloat(b.rating) - parseFloat(a.rating));
                default:
                    // "Sort: Default" (empty value) should behave the same
                    // as explicitly picking Most Recent, not pass the
                    // collection through in Jekyll's own (non-chronological)
                    // iteration order — that was the "not most recent by
                    // default" bug.
                    return sorted.sort((a,b) => new Date(b.date) - new Date(a.date));
            }}
        }}

        function renderPopular() {{
            const popular = [...works].sort((a,b) => parseFloat(b.rating) - parseFloat(a.rating)).slice(0,4);
            document.getElementById('popularGrid').innerHTML = popular.map(w => `
                <a href="${{w.url}}" class="popular-card">
                    <div class="cover"><img src="${{w.cover}}" alt="${{w.title}}"></div>
                    <div class="info"><h3>${{w.title}}</h3><div class="meta">⭐ ${{w.rating}} • ${{w.author}}</div></div>
                </a>
            `).join('');
        }}

        function buildWorkCards(pageWorks) {{
            return pageWorks.map(w => `
                <a href="${{w.url}}" class="work-card">
                    <div class="cover"><img src="${{w.cover}}" alt="${{w.title}}"></div>
                    <div class="info"><h3>${{w.title}}</h3><div class="meta">${{w.author}} • ⭐ ${{w.rating}}</div></div>
                </a>
            `).join('');
        }}

        function renderWorks(page = 1) {{
            const sortMode = document.getElementById('sortSelect').value;
            let list = applyFilters(works);
            // Filters section defaults to Most Recent when no explicit sort chosen
            const effectiveSort = sortMode || (isFilterPanelOpen() ? 'recent' : '');
            list = applySort(list, effectiveSort);

            // Popular Today is a homepage-only feature — hide it entirely
            // once the user pages past page 1.
            document.getElementById('popularSection').style.display = (page === 1) ? '' : 'none';

            const noResults = document.getElementById('noResults');
            const grid = document.getElementById('worksGrid');
            if (list.length === 0) {{
                grid.innerHTML = '';
                noResults.style.display = 'block';
                document.getElementById('pagination').innerHTML = '';
                return;
            }}
            noResults.style.display = 'none';

            const start = (page - 1) * POSTS_PER_PAGE;
            const pageWorks = list.slice(start, start + POSTS_PER_PAGE);
            grid.innerHTML = buildWorkCards(pageWorks);
            const total = Math.ceil(list.length / POSTS_PER_PAGE);
            document.getElementById('pagination').innerHTML = buildPaginationHtml(page, total, 'renderWorks');
        }}

        function isFilterPanelOpen() {{
            return document.getElementById('filterPanel').classList.contains('open');
        }}

        document.getElementById('sortSelect').addEventListener('change', () => renderWorks(1));
        document.getElementById('filterToggleBtn').addEventListener('click', () => {{
            document.getElementById('filterPanel').classList.toggle('open');
            renderWorks(1);
        }});
        document.getElementById('filterFullColor').addEventListener('change', () => renderWorks(1));
        document.querySelectorAll('input[name="ntrFilter"]').forEach(el => {{
            el.addEventListener('change', () => renderWorks(1));
        }});

        // Search redirects to the dedicated /search/ page (which has its own
        // URL, own title, and shows only results — no Popular Today section)
        // instead of filtering in place on the homepage.
        function goSearch() {{
            const q = document.getElementById('searchInput').value.trim();
            if (q) window.location.href = '/search/?q=' + encodeURIComponent(q);
        }}
        document.getElementById('searchBtn').addEventListener('click', goSearch);
        document.getElementById('searchInput').addEventListener('keydown', (e) => {{
            if (e.key === 'Enter') goSearch();
        }});

        renderPopular();
        renderWorks(1);
    </script>
</body>
</html>
"""

def is_valid_jekyll_front_matter(content):
    """
    Returns True if `content` starts with a well-formed Jekyll front
    matter block (--- ... ---) whose YAML parses to either nothing
    (empty/comment-only front matter, which is what index.html
    intentionally uses) or a mapping (dict) — the only two shapes Jekyll
    actually accepts for front matter.

    This is stricter than "does it parse as valid YAML at all" on
    purpose. A real corruption was found where text got spliced into the
    front-matter comment block (see update_verification_tag's old bug):
    the resulting block was *technically* parseable YAML — PyYAML read
    the stray unindented text as a bare multi-line string scalar rather
    than raising an error — so a plain try/except yaml.safe_load() check
    incorrectly reported it as fine, while Jekyll's own parser (which
    requires front matter to be a mapping) correctly rejected it and
    failed the build. Checking the parsed type, not just "did it raise",
    is what catches this.
    """
    if not content.startswith("---"):
        return False
    fm_match = re.match(r"\A---\s*\n(.*?\n)?---\s*\n", content, re.DOTALL)
    if not fm_match:
        return False
    try:
        parsed = yaml.safe_load(fm_match.group(1) or "")
    except yaml.YAMLError:
        return False
    return parsed is None or isinstance(parsed, dict)

def ensure_index_html():
    """
    Self-healing homepage. The bot fully owns this file and always keeps
    it current — see ensure_post_layout() for why "protect unmarked
    files" was removed. This matters especially here: index.html was
    originally created once by setup.sh before any version-marker system
    existed, so the old "no marker = don't touch" logic was permanently
    blocking every homepage upgrade from ever taking effect.
    """
    if os.path.exists(INDEX_HTML_PATH):
        with open(INDEX_HTML_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r"arc-comic-index-version:\s*(\d+)", content)
        current_version = int(match.group(1)) if match else 0

        # Don't trust the version marker alone. A file can carry a
        # current-looking marker while its actual front matter is broken
        # (merge-conflict markers left behind, a truncated/interrupted
        # write, manual corruption, etc.) — GitHub Pages then fails every
        # build with "Invalid YAML front matter" while the bot keeps
        # logging "already up to date" and skipping the rewrite. This bit
        # Master before (see handoff bug #9) in a different shape, so
        # ensure_index_html() now also checks that the file *starts* with
        # a well-formed, parseable front matter block before trusting the
        # version marker.
        front_matter_ok = is_valid_jekyll_front_matter(content)

        if current_version >= INDEX_HTML_VERSION and front_matter_ok:
            return False

        if current_version >= INDEX_HTML_VERSION and not front_matter_ok:
            print("⚠️ index.html had a current version marker but broken/missing "
                  "front matter — rewriting anyway to fix the build")

    with open(INDEX_HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(INDEX_HTML_TEMPLATE)
    print(f"🔧 index.html updated to v{INDEX_HTML_VERSION} "
          f"(favicon, sort/filter toolbar, Latest Upload, working search)")
    return True

# ============== TAG SYSTEM (Stage 3) ==============
TAGS_DIR = os.path.join(WORK_DIR, "_tags")
TAG_LAYOUT_PATH = os.path.join(WORK_DIR, "_layouts", "tag.html")
TAG_LAYOUT_VERSION = 8
TAGS_INDEX_PATH = os.path.join(WORK_DIR, "tags", "index.html")
TAGS_INDEX_VERSION = 1

TAG_LAYOUT_TEMPLATE = f"""<!-- arc-comic-layout-version: {TAG_LAYOUT_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <title>{{{{ page.tag_name }}}} Comics - Arc Comic</title>
    <meta name="description" content="Browse {{{{ page.tag_name }}}} manga and doujinshi on Arc Comic">
    <script async src="https://ss.mrmnd.com/banner.js"></script>
    <style>
        :root {{
            --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230;
            --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0; --border: #2a2a3a;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .logo-link {{ display: inline-flex; text-decoration: none; color: var(--accent); font-weight: 800; font-size: 18px; }}
        .tagline {{ color: var(--text-muted); font-size: 12px; margin: 4px 0 16px; text-align: left; }}
        .breadcrumb {{ color: var(--text-muted); font-size: 14px; margin-bottom: 20px; }}
        .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
        h1 {{ font-size: 28px; font-weight: 800; margin-bottom: 8px; }}
        .count {{ color: var(--text-muted); margin-bottom: 24px; }}
        .toolbar {{ margin-bottom: 20px; }}
        .toolbar select {{
            background: var(--bg-card); color: var(--text); border: 1px solid var(--border);
            border-radius: 10px; padding: 10px 14px; font-size: 13px; cursor: pointer;
        }}
        .works-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }}
        .work-card {{
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border);
            overflow: hidden; text-decoration: none; color: var(--text); transition: transform 0.2s;
        }}
        .work-card:hover {{ transform: translateY(-4px); }}
        .work-card .cover {{ width: 100%; padding-top: 140%; position: relative; background: var(--bg-elevated); }}
        .work-card .cover img {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; }}
        .work-card .info {{ padding: 10px; }}
        .work-card .info h3 {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .work-card .info .meta {{ font-size: 11px; color: var(--text-muted); margin-top: 3px; }}
        .ad-slot {{
            margin: 20px 0; display: flex; justify-content: center; align-items: center;
            min-height: 100px; max-height: 300px; width: 100%;
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden;
        }}
        .ad-slot > * {{ max-width: 100% !important; max-height: 100% !important;
            width: auto !important; height: auto !important; }}
        .ad-slot img, .ad-slot iframe {{ object-fit: contain; }}
        .pagination {{ display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--border); }}
        .pagination a, .pagination span {{ padding: 8px 14px; border-radius: 8px; font-size: 14px; font-weight: 600; text-decoration: none; }}
        .pagination a {{ background: var(--bg-card); color: var(--text); border: 1px solid var(--border); }}
        .pagination a:hover {{ background: var(--accent); color: #000; }}
        .pagination span {{ background: var(--accent); color: #000; }}
        .pagination-ellipsis {{ padding: 8px 6px; color: var(--text-muted); font-weight: 600; user-select: none; }}
        @media (max-width: 768px) {{ .works-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="logo-link">✨ Arc Comic</a>
        <div class="tagline">{{{{ site.data.site_meta.tagline_html }}}}</div>
        <div class="breadcrumb"><a href="/">Home</a> › <a href="/tags/">Tags</a> › {{{{ page.tag_name }}}}</div>
        <div class="ad-slot" id="topAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        <h1>💥 {{{{ page.tag_name }}}}</h1>
        <div class="count">{{{{ page.work_count }}}} comic{{% if page.work_count != 1 %}}s{{% endif %}}</div>
        <div class="toolbar">
            <select id="sortSelect">
                <option value="recent">Most Recent</option>
                <option value="oldest">Oldest</option>
                <option value="rating">Highest Rated</option>
            </select>
        </div>
        <div class="works-grid" id="worksGrid"></div>
        <div class="pagination" id="pagination"></div>
        <div class="ad-slot" id="bottomAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        {{% include follow_us.html %}}
    </div>
    <script>
        {PAGINATION_JS}
        const works = [
            {{% for w in page.works %}}
            {{ title: "{{{{ w.title | escape }}}}", author: "{{{{ w.author }}}}", cover: "{{{{ w.cover }}}}",
               rating: "{{{{ w.rating }}}}", date: "{{{{ w.date }}}}", url: "{{{{ w.url }}}}" }}{{% unless forloop.last %}},{{% endunless %}}
            {{% endfor %}}
        ];
        const PER_PAGE = 16;
        function buildCards(pageWorks) {{
            return pageWorks.map(w => `
                <a href="${{w.url}}" class="work-card">
                    <div class="cover"><img src="${{w.cover}}" alt="${{w.title}}"></div>
                    <div class="info"><h3>${{w.title}}</h3><div class="meta">${{w.author}} • ⭐ ${{w.rating}}</div></div>
                </a>
            `).join('');
        }}
        function render(page) {{
            page = page || 1;
            const mode = document.getElementById('sortSelect').value;
            let sorted = [...works];
            if (mode === 'oldest') sorted.sort((a,b) => new Date(a.date) - new Date(b.date));
            else if (mode === 'rating') sorted.sort((a,b) => parseFloat(b.rating) - parseFloat(a.rating));
            else sorted.sort((a,b) => new Date(b.date) - new Date(a.date)); // recent (default)

            const start = (page - 1) * PER_PAGE;
            const pageWorks = sorted.slice(start, start + PER_PAGE);
            document.getElementById('worksGrid').innerHTML = buildCards(pageWorks);

            // Banner ads only from page 2 onward — never on the first
            // screen someone lands on for this tag.
            const showAds = page > 1;
            document.getElementById('topAdSlot').style.display = showAds ? '' : 'none';
            document.getElementById('bottomAdSlot').style.display = showAds ? '' : 'none';

            const total = Math.ceil(sorted.length / PER_PAGE);
            document.getElementById('pagination').innerHTML = buildPaginationHtml(page, total, 'render');
        }}
        document.getElementById('sortSelect').addEventListener('change', () => render(1));
        render(1);
    </script>
</body>
</html>
"""

def ensure_tag_layout():
    if os.path.exists(TAG_LAYOUT_PATH):
        with open(TAG_LAYOUT_PATH, 'r', encoding='utf-8') as f:
            first_line = f.readline()
        match = re.search(r"arc-comic-layout-version:\s*(\d+)", first_line)
        if (int(match.group(1)) if match else 0) >= TAG_LAYOUT_VERSION:
            return False
    os.makedirs(os.path.dirname(TAG_LAYOUT_PATH), exist_ok=True)
    with open(TAG_LAYOUT_PATH, 'w', encoding='utf-8') as f:
        f.write(TAG_LAYOUT_TEMPLATE)
    print(f"🔧 _layouts/tag.html updated to v{TAG_LAYOUT_VERSION}")
    return True

def slugify(text):
    """Matches Jekyll's slugify filter closely enough for filenames/URLs:
    lowercase, spaces to hyphens, strip anything not alphanumeric/hyphen."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")

# ============== SEARCH RESULTS PAGE ==============
SEARCH_PAGE_PATH = os.path.join(WORK_DIR, "search", "index.html")
SEARCH_PAGE_VERSION = 9

SEARCH_PAGE_TEMPLATE = f"""---
---
<!-- arc-comic-index-version: {SEARCH_PAGE_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <title id="pageTitle">Search - Arc Comic</title>
    <meta name="description" content="Search manga and doujinshi on Arc Comic">
    <script async src="https://ss.mrmnd.com/banner.js"></script>
    <style>
        :root {{
            --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230;
            --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0; --border: #2a2a3a;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .header {{ text-align: center; padding: 30px 20px; border-bottom: 1px solid var(--border); margin-bottom: 30px; }}
        .header a.logo-link {{ text-decoration: none; }}
        .header h1 {{
            font-size: 32px; font-weight: 800;
            background: linear-gradient(135deg, var(--accent), #f97316);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }}
        .tagline {{ color: var(--text-muted); font-size: 12px; margin-top: 4px; text-align: center; }}
        .search-box {{ max-width: 500px; margin: 20px auto 0; display: flex; gap: 8px; position: relative; }}
        .search-box input {{
            flex: 1; padding: 14px 20px 14px 48px; background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 12px; color: var(--text); font-size: 15px; outline: none;
        }}
        .search-box input:focus {{ border-color: var(--accent); }}
        .search-box .search-icon {{ position: absolute; left: 16px; top: 50%; transform: translateY(-50%); font-size: 18px; }}
        .search-box button {{ background: var(--accent); color: #000; border: none; border-radius: 12px; padding: 0 22px; font-weight: 700; font-size: 14px; cursor: pointer; }}
        .results-title {{ font-size: 20px; font-weight: 700; margin-bottom: 20px; }}
        .results-title span {{ color: var(--accent); }}
        .works-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }}
        .work-card {{
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border);
            overflow: hidden; text-decoration: none; color: var(--text); transition: transform 0.2s;
        }}
        .work-card:hover {{ transform: translateY(-4px); }}
        .work-card .cover {{ width: 100%; padding-top: 140%; position: relative; background: var(--bg-elevated); }}
        .work-card .cover img {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; }}
        .work-card .info {{ padding: 10px; }}
        .work-card .info h3 {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .work-card .info .meta {{ font-size: 11px; color: var(--text-muted); margin-top: 3px; }}
        .ad-slot {{
            margin: 20px 0; display: flex; justify-content: center; align-items: center;
            min-height: 100px; max-height: 300px; width: 100%;
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden;
        }}
        .ad-slot > * {{ max-width: 100% !important; max-height: 100% !important;
            width: auto !important; height: auto !important; }}
        .ad-slot img, .ad-slot iframe {{ object-fit: contain; }}
        .no-results {{ text-align: center; padding: 60px 20px; color: var(--text-muted); }}
        .pagination {{ display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--border); }}
        .pagination a, .pagination span {{ padding: 8px 14px; border-radius: 8px; font-size: 14px; font-weight: 600; text-decoration: none; }}
        .pagination a {{ background: var(--bg-card); color: var(--text); border: 1px solid var(--border); }}
        .pagination a:hover {{ background: var(--accent); color: #000; }}
        .pagination span {{ background: var(--accent); color: #000; }}
        .pagination-ellipsis {{ padding: 8px 6px; color: var(--text-muted); font-weight: 600; user-select: none; }}
        @media (max-width: 768px) {{ .works-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <a href="/" class="logo-link"><h1>✨ Arc Comic</h1></a>
            <div class="tagline">{{{{ site.data.site_meta.tagline_html }}}}</div>
            <div class="search-box">
                <span class="search-icon">🔍</span>
                <input type="text" placeholder="Search by title, author, or tag..." id="searchInput">
                <button id="searchBtn">Search</button>
            </div>
        </div>
        <div class="ad-slot" id="topAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        <h2 class="results-title" id="resultsTitle">Search Results</h2>
        <div class="works-grid" id="worksGrid"></div>
        <div class="no-results" id="noResults" style="display:none;">No comics match your search.</div>
        <div class="pagination" id="pagination"></div>
        <div class="ad-slot" id="bottomAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        {{% include follow_us.html %}}
    </div>
    <script>
        {PAGINATION_JS}
        const works = [
            {{% for post in site.works %}}
            {{
                title: "{{{{ post.title | escape }}}}",
                author: "{{{{ post.author }}}}",
                cover: "{{{{ post.cover }}}}",
                rating: "{{{{ post.rating }}}}",
                tags: {{{{ post.tags | jsonify }}}},
                code: "{{{{ post.code }}}}",
                date: "{{{{ post.date }}}}",
                url: "{{{{ post.url }}}}"
            }}{{% unless forloop.last %}},{{% endunless %}}
            {{% endfor %}}
        ];
        const PER_PAGE = 16;
        let currentResults = [];

        function getQueryParam(name) {{
            return new URLSearchParams(window.location.search).get(name) || "";
        }}

        function buildCards(pageWorks) {{
            return pageWorks.map(w => `
                <a href="${{w.url}}" class="work-card">
                    <div class="cover"><img src="${{w.cover}}" alt="${{w.title}}"></div>
                    <div class="info"><h3>${{w.title}}</h3><div class="meta">${{w.author}} • ⭐ ${{w.rating}}</div></div>
                </a>
            `).join('');
        }}

        function renderPage(page) {{
            page = page || 1;
            const start = (page - 1) * PER_PAGE;
            const pageWorks = currentResults.slice(start, start + PER_PAGE);
            document.getElementById('worksGrid').innerHTML = buildCards(pageWorks);

            // Banner ads only from page 2 onward, never on the first
            // screen of results.
            const showAds = page > 1;
            document.getElementById('topAdSlot').style.display = showAds ? '' : 'none';
            document.getElementById('bottomAdSlot').style.display = showAds ? '' : 'none';

            const total = Math.ceil(currentResults.length / PER_PAGE);
            document.getElementById('pagination').innerHTML = buildPaginationHtml(page, total, 'renderPage');
        }}

        function runSearch(q) {{
            document.getElementById('searchInput').value = q;
            document.getElementById('pageTitle').textContent = q ? `"${{q}}" - Search Results - Arc Comic` : "Search - Arc Comic";
            document.getElementById('resultsTitle').innerHTML = q
                ? `Search results for <span>"${{q}}"</span>`
                : "Search Results";

            if (!q) {{
                document.getElementById('worksGrid').innerHTML = '';
                document.getElementById('pagination').innerHTML = '';
                document.getElementById('topAdSlot').style.display = 'none';
                document.getElementById('bottomAdSlot').style.display = 'none';
                document.getElementById('noResults').style.display = 'block';
                document.getElementById('noResults').textContent = 'Type something in the search box above.';
                return;
            }}

            // Sort most-recent-first by default, same as every other grid page.
            const query = q.toLowerCase();
            currentResults = works.filter(w =>
                w.title.toLowerCase().includes(query) ||
                w.author.toLowerCase().includes(query) ||
                (w.code || '').toLowerCase().includes(query) ||
                (w.tags || []).some(t => t.toLowerCase().includes(query))
            ).sort((a, b) => new Date(b.date) - new Date(a.date));

            const noResults = document.getElementById('noResults');
            if (currentResults.length === 0) {{
                document.getElementById('worksGrid').innerHTML = '';
                document.getElementById('pagination').innerHTML = '';
                document.getElementById('topAdSlot').style.display = 'none';
                document.getElementById('bottomAdSlot').style.display = 'none';
                noResults.style.display = 'block';
                noResults.textContent = 'No comics match your search.';
            }} else {{
                noResults.style.display = 'none';
                renderPage(1);
            }}
        }}

        function goSearch() {{
            const q = document.getElementById('searchInput').value.trim();
            window.location.href = '/search/?q=' + encodeURIComponent(q);
        }}
        document.getElementById('searchBtn').addEventListener('click', goSearch);
        document.getElementById('searchInput').addEventListener('keydown', function(e) {{
            if (e.key === 'Enter') goSearch();
        }});

        runSearch(getQueryParam('q'));
    </script>
</body>
</html>
"""

def ensure_search_page():
    if os.path.exists(SEARCH_PAGE_PATH):
        with open(SEARCH_PAGE_PATH, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r"arc-comic-index-version:\s*(\d+)", content)
        if (int(match.group(1)) if match else 0) >= SEARCH_PAGE_VERSION:
            return False
    os.makedirs(os.path.dirname(SEARCH_PAGE_PATH), exist_ok=True)
    with open(SEARCH_PAGE_PATH, 'w', encoding='utf-8') as f:
        f.write(SEARCH_PAGE_TEMPLATE)
    print(f"🔧 search/index.html updated to v{SEARCH_PAGE_VERSION}")
    return True

def regenerate_tag_pages():
    """
    Scans every work's front matter and regenerates one _tags/<slug>.md
    stub per unique tag, each carrying the full list of matching works
    directly in its own front matter (avoids needing a Jekyll plugin to
    do cross-collection joins at build time — plain Liquid can't easily
    query "all works with tag X", so we precompute it here instead).
    Also regenerates tags/index.html (the browse-all-tags page).
    Called after every batch flush, since tag membership changes whenever
    posts are added or removed. Returns True only if the tag data
    actually changed (via content hash), so callers can skip pushing
    when nothing changed — e.g. on every routine startup.
    """
    os.makedirs(TAGS_DIR, exist_ok=True)

    tag_map = {}  # slug -> {"name": original casing, "works": [...]}
    for fname in sorted(os.listdir(WORKS_DIR)):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(WORKS_DIR, fname), 'r', encoding='utf-8') as f:
            content = f.read()
        try:
            front = yaml.safe_load(content.split("---")[1])
        except Exception:
            continue
        if not front:
            continue
        work_entry = {
            "title": front.get("title", ""),
            "author": front.get("author", ""),
            "cover": front.get("cover", ""),
            "rating": front.get("rating", "0"),
            "date": str(front.get("date", "")),
            "code": front.get("code", ""),
            "url": f"/works/{front.get('code', '')}/",
        }
        for tag in front.get("tags", []):
            slug = slugify(tag)
            if not slug:
                continue
            if slug not in tag_map:
                tag_map[slug] = {"name": tag, "works": []}
            tag_map[slug]["works"].append(work_entry)

    # Compare against a hash of the previous run's tag data before writing
    # anything, so a no-op regeneration (nothing changed) doesn't trigger
    # an unnecessary commit/push every single startup.
    fingerprint = hashlib.sha256(
        json.dumps(tag_map, sort_keys=True, default=str).encode()
    ).hexdigest()
    fingerprint_file = os.path.join(TAGS_DIR, ".fingerprint")
    if os.path.exists(fingerprint_file):
        with open(fingerprint_file, 'r') as f:
            if f.read().strip() == fingerprint:
                return False  # no change since last regeneration

    # Clear old tag stubs so removed/renamed tags don't leave orphan pages
    for fname in os.listdir(TAGS_DIR):
        if fname.endswith(".md"):
            os.remove(os.path.join(TAGS_DIR, fname))

    for slug, data in tag_map.items():
        works_yaml = yaml.dump(data["works"], default_flow_style=False, allow_unicode=True, sort_keys=False)
        # indent for nesting under the 'works:' front-matter key
        indented = "\n".join("  " + line if line.strip() else line for line in works_yaml.split("\n"))
        stub = (
            "---\n"
            "layout: tag\n"
            f"tag_name: \"{data['name']}\"\n"
            f"work_count: {len(data['works'])}\n"
            "works:\n"
            f"{indented}"
            "---\n"
        )
        with open(os.path.join(TAGS_DIR, f"{slug}.md"), 'w', encoding='utf-8') as f:
            f.write(stub)

    with open(fingerprint_file, 'w') as f:
        f.write(fingerprint)

    print(f"🔧 Regenerated {len(tag_map)} tag pages")
    _write_tags_index(tag_map)
    return True

def _write_tags_index(tag_map):
    """Writes tags/index.html: popular tags + full A-Z list with counts,
    and a search box to filter the list client-side."""
    sorted_by_count = sorted(tag_map.items(), key=lambda kv: len(kv[1]["works"]), reverse=True)
    popular = sorted_by_count[:20]
    all_sorted = sorted(tag_map.items(), key=lambda kv: kv[1]["name"].lower())

    def tag_chip(slug, data):
        return (f'<a href="/tags/{slug}/" class="tag-chip">'
                f'{data["name"]} <span class="tag-count">{len(data["works"])}</span></a>')

    popular_html = "\n".join(tag_chip(s, d) for s, d in popular)
    all_html = "\n".join(tag_chip(s, d) for s, d in all_sorted)

    html = f"""<!-- arc-comic-layout-version: {TAGS_INDEX_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <title>Browse Tags - Arc Comic</title>
    <meta name="description" content="Browse all manga and doujinshi tags on Arc Comic">
    <style>
        :root {{ --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230; --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0; --border: #2a2a3a; }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
        .logo-link {{ display: inline-flex; text-decoration: none; color: var(--accent); font-weight: 800; font-size: 18px; margin-bottom: 16px; }}
        .breadcrumb {{ color: var(--text-muted); font-size: 14px; margin-bottom: 20px; }}
        .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
        h1 {{ font-size: 26px; font-weight: 800; margin-bottom: 20px; }}
        .search-box input {{
            width: 100%; padding: 12px 16px; background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; color: var(--text); font-size: 14px; outline: none; margin-bottom: 24px;
        }}
        .search-box input:focus {{ border-color: var(--accent); }}
        h2 {{ font-size: 16px; margin: 24px 0 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
        .tag-cloud {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .tag-chip {{
            background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
            padding: 8px 14px; border-radius: 20px; font-size: 13px; text-decoration: none; display: inline-flex; gap: 6px; align-items: center;
        }}
        .tag-chip:hover {{ border-color: var(--accent); color: var(--accent); }}
        .tag-count {{ color: var(--text-muted); font-size: 11px; }}
        .tag-chip.hidden {{ display: none; }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="logo-link">✨ Arc Comic</a>
        <div class="breadcrumb"><a href="/">Home</a> › Tags</div>
        <h1>💥 Browse Tags</h1>
        <div class="search-box">
            <input type="text" id="tagSearch" placeholder="Search tags...">
        </div>
        <h2>🔥 Popular Tags</h2>
        <div class="tag-cloud" id="popularTags">
            {popular_html}
        </div>
        <h2>A–Z All Tags</h2>
        <div class="tag-cloud" id="allTags">
            {all_html}
        </div>
    </div>
    <script>
        document.getElementById('tagSearch').addEventListener('input', function(e) {{
            const q = e.target.value.toLowerCase();
            document.querySelectorAll('#allTags .tag-chip, #popularTags .tag-chip').forEach(function(chip) {{
                const text = chip.textContent.toLowerCase();
                chip.classList.toggle('hidden', q.length > 0 && !text.includes(q));
            }});
        }});
    </script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(TAGS_INDEX_PATH), exist_ok=True)
    with open(TAGS_INDEX_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

# ============== ARTIST SYSTEM ==============
# Mirrors the tag system exactly: one collection, one layout, one directory
# index, regenerated together with tags after every batch flush/delete.
ARTISTS_DIR = os.path.join(WORK_DIR, "_artists")
ARTIST_LAYOUT_PATH = os.path.join(WORK_DIR, "_layouts", "artist.html")
ARTIST_LAYOUT_VERSION = 7
ARTISTS_INDEX_PATH = os.path.join(WORK_DIR, "artists", "index.html")
ARTISTS_INDEX_VERSION = 1

ARTIST_LAYOUT_TEMPLATE = f"""<!-- arc-comic-layout-version: {ARTIST_LAYOUT_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <title>{{{{ page.artist_name }}}} - Arc Comic</title>
    <meta name="description" content="Browse all manga and doujinshi by {{{{ page.artist_name }}}} on Arc Comic">
    <script async src="https://ss.mrmnd.com/banner.js"></script>
    <style>
        :root {{
            --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230;
            --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0; --border: #2a2a3a;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
        .logo-link {{ display: inline-flex; text-decoration: none; color: var(--accent); font-weight: 800; font-size: 18px; }}
        .tagline {{ color: var(--text-muted); font-size: 12px; margin: 4px 0 16px; text-align: left; }}
        .breadcrumb {{ color: var(--text-muted); font-size: 14px; margin-bottom: 20px; }}
        .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
        h1 {{ font-size: 28px; font-weight: 800; margin-bottom: 8px; }}
        .count {{ color: var(--text-muted); margin-bottom: 24px; }}
        .toolbar {{ margin-bottom: 20px; }}
        .toolbar select {{
            background: var(--bg-card); color: var(--text); border: 1px solid var(--border);
            border-radius: 10px; padding: 10px 14px; font-size: 13px; cursor: pointer;
        }}
        .works-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 16px; }}
        .work-card {{
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border);
            overflow: hidden; text-decoration: none; color: var(--text); transition: transform 0.2s;
        }}
        .work-card:hover {{ transform: translateY(-4px); }}
        .work-card .cover {{ width: 100%; padding-top: 140%; position: relative; background: var(--bg-elevated); }}
        .work-card .cover img {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: cover; }}
        .work-card .info {{ padding: 10px; }}
        .work-card .info h3 {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .work-card .info .meta {{ font-size: 11px; color: var(--text-muted); margin-top: 3px; }}
        .ad-slot {{
            margin: 20px 0; display: flex; justify-content: center; align-items: center;
            min-height: 100px; max-height: 300px; width: 100%;
            background: var(--bg-card); border-radius: 12px; border: 1px solid var(--border); overflow: hidden;
        }}
        .ad-slot > * {{ max-width: 100% !important; max-height: 100% !important;
            width: auto !important; height: auto !important; }}
        .ad-slot img, .ad-slot iframe {{ object-fit: contain; }}
        .pagination {{ display: flex; justify-content: center; gap: 8px; flex-wrap: wrap; margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--border); }}
        .pagination a, .pagination span {{ padding: 8px 14px; border-radius: 8px; font-size: 14px; font-weight: 600; text-decoration: none; }}
        .pagination a {{ background: var(--bg-card); color: var(--text); border: 1px solid var(--border); }}
        .pagination a:hover {{ background: var(--accent); color: #000; }}
        .pagination span {{ background: var(--accent); color: #000; }}
        .pagination-ellipsis {{ padding: 8px 6px; color: var(--text-muted); font-weight: 600; user-select: none; }}
        @media (max-width: 768px) {{ .works-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="logo-link">✨ Arc Comic</a>
        <div class="tagline">{{{{ site.data.site_meta.tagline_html }}}}</div>
        <div class="breadcrumb"><a href="/">Home</a> › <a href="/artists/">Artists</a> › {{{{ page.artist_name }}}}</div>
        <div class="ad-slot" id="topAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        <h1>✨ {{{{ page.artist_name }}}}</h1>
        <div class="count">{{{{ page.work_count }}}} comic{{% if page.work_count != 1 %}}s{{% endif %}}</div>
        <div class="toolbar">
            <select id="sortSelect">
                <option value="recent">Most Recent</option>
                <option value="oldest">Oldest</option>
                <option value="rating">Highest Rated</option>
            </select>
        </div>
        <div class="works-grid" id="worksGrid"></div>
        <div class="pagination" id="pagination"></div>
        <div class="ad-slot" id="bottomAdSlot" data-mndbanid="{BANNER_AD_ZONE_ID}" style="display:none;"></div>
        {{% include follow_us.html %}}
    </div>
    <script>
        {PAGINATION_JS}
        const works = [
            {{% for w in page.works %}}
            {{ title: "{{{{ w.title | escape }}}}", cover: "{{{{ w.cover }}}}",
               rating: "{{{{ w.rating }}}}", date: "{{{{ w.date }}}}", url: "{{{{ w.url }}}}" }}{{% unless forloop.last %}},{{% endunless %}}
            {{% endfor %}}
        ];
        const PER_PAGE = 16;
        function buildCards(pageWorks) {{
            return pageWorks.map(w => `
                <a href="${{w.url}}" class="work-card">
                    <div class="cover"><img src="${{w.cover}}" alt="${{w.title}}"></div>
                    <div class="info"><h3>${{w.title}}</h3><div class="meta">⭐ ${{w.rating}}</div></div>
                </a>
            `).join('');
        }}
        function render(page) {{
            page = page || 1;
            const mode = document.getElementById('sortSelect').value;
            let sorted = [...works];
            if (mode === 'oldest') sorted.sort((a,b) => new Date(a.date) - new Date(b.date));
            else if (mode === 'rating') sorted.sort((a,b) => parseFloat(b.rating) - parseFloat(a.rating));
            else sorted.sort((a,b) => new Date(b.date) - new Date(a.date)); // recent (default)

            const start = (page - 1) * PER_PAGE;
            const pageWorks = sorted.slice(start, start + PER_PAGE);
            document.getElementById('worksGrid').innerHTML = buildCards(pageWorks);

            const showAds = page > 1;
            document.getElementById('topAdSlot').style.display = showAds ? '' : 'none';
            document.getElementById('bottomAdSlot').style.display = showAds ? '' : 'none';

            const total = Math.ceil(sorted.length / PER_PAGE);
            document.getElementById('pagination').innerHTML = buildPaginationHtml(page, total, 'render');
        }}
        document.getElementById('sortSelect').addEventListener('change', () => render(1));
        render(1);
    </script>
</body>
</html>
"""

def ensure_artist_layout():
    if os.path.exists(ARTIST_LAYOUT_PATH):
        with open(ARTIST_LAYOUT_PATH, 'r', encoding='utf-8') as f:
            first_line = f.readline()
        match = re.search(r"arc-comic-layout-version:\s*(\d+)", first_line)
        if (int(match.group(1)) if match else 0) >= ARTIST_LAYOUT_VERSION:
            return False
    os.makedirs(os.path.dirname(ARTIST_LAYOUT_PATH), exist_ok=True)
    with open(ARTIST_LAYOUT_PATH, 'w', encoding='utf-8') as f:
        f.write(ARTIST_LAYOUT_TEMPLATE)
    print(f"🔧 _layouts/artist.html updated to v{ARTIST_LAYOUT_VERSION}")
    return True

def regenerate_artist_pages():
    """
    Same pattern as regenerate_tag_pages(): scans every work, groups by
    author, writes one _artists/<slug>.md stub per unique artist with the
    full list of their works precomputed in front matter. Also writes
    artists/index.html (the browse-all-artists directory).
    """
    os.makedirs(ARTISTS_DIR, exist_ok=True)

    artist_map = {}  # slug -> {"name": original casing, "works": [...]}
    for fname in sorted(os.listdir(WORKS_DIR)):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(WORKS_DIR, fname), 'r', encoding='utf-8') as f:
            content = f.read()
        try:
            front = yaml.safe_load(content.split("---")[1])
        except Exception:
            continue
        if not front:
            continue
        author = front.get("author", "")
        if not author:
            continue
        work_entry = {
            "title": front.get("title", ""),
            "cover": front.get("cover", ""),
            "rating": front.get("rating", "0"),
            "date": str(front.get("date", "")),
            "code": front.get("code", ""),
            "url": f"/works/{front.get('code', '')}/",
        }
        slug = slugify(author)
        if not slug:
            continue
        if slug not in artist_map:
            artist_map[slug] = {"name": author, "works": []}
        artist_map[slug]["works"].append(work_entry)

    fingerprint = hashlib.sha256(
        json.dumps(artist_map, sort_keys=True, default=str).encode()
    ).hexdigest()
    fingerprint_file = os.path.join(ARTISTS_DIR, ".fingerprint")
    if os.path.exists(fingerprint_file):
        with open(fingerprint_file, 'r') as f:
            if f.read().strip() == fingerprint:
                return False

    for fname in os.listdir(ARTISTS_DIR):
        if fname.endswith(".md"):
            os.remove(os.path.join(ARTISTS_DIR, fname))

    for slug, data in artist_map.items():
        works_yaml = yaml.dump(data["works"], default_flow_style=False, allow_unicode=True, sort_keys=False)
        indented = "\n".join("  " + line if line.strip() else line for line in works_yaml.split("\n"))
        stub = (
            "---\n"
            "layout: artist\n"
            f"artist_name: \"{data['name']}\"\n"
            f"work_count: {len(data['works'])}\n"
            "works:\n"
            f"{indented}"
            "---\n"
        )
        with open(os.path.join(ARTISTS_DIR, f"{slug}.md"), 'w', encoding='utf-8') as f:
            f.write(stub)

    with open(fingerprint_file, 'w') as f:
        f.write(fingerprint)

    print(f"🔧 Regenerated {len(artist_map)} artist pages")
    _write_artists_index(artist_map)
    return True

def _write_artists_index(artist_map):
    """Writes artists/index.html: popular artists (by comic count for now
    — will switch to by-views once the analytics system tracks per-artist
    views) + full A-Z list with comic counts, plus a search box."""
    sorted_by_count = sorted(artist_map.items(), key=lambda kv: len(kv[1]["works"]), reverse=True)
    popular = sorted_by_count[:20]
    all_sorted = sorted(artist_map.items(), key=lambda kv: kv[1]["name"].lower())

    def artist_chip(slug, data):
        return (f'<a href="/artists/{slug}/" class="tag-chip">'
                f'{data["name"]} <span class="tag-count">{len(data["works"])}</span></a>')

    popular_html = "\n".join(artist_chip(s, d) for s, d in popular)
    all_html = "\n".join(artist_chip(s, d) for s, d in all_sorted)

    html = f"""<!-- arc-comic-layout-version: {ARTISTS_INDEX_VERSION} -->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <title>Browse Artists - Arc Comic</title>
    <meta name="description" content="Browse all manga and doujinshi artists on Arc Comic">
    <style>
        :root {{ --bg: #0f0f13; --bg-card: #1a1a24; --bg-elevated: #222230; --accent: #f59e0b; --text: #e2e2e8; --text-muted: #8888a0; --border: #2a2a3a; }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
        .logo-link {{ display: inline-flex; text-decoration: none; color: var(--accent); font-weight: 800; font-size: 18px; margin-bottom: 16px; }}
        .breadcrumb {{ color: var(--text-muted); font-size: 14px; margin-bottom: 20px; }}
        .breadcrumb a {{ color: var(--accent); text-decoration: none; }}
        h1 {{ font-size: 26px; font-weight: 800; margin-bottom: 20px; }}
        .search-box input {{
            width: 100%; padding: 12px 16px; background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; color: var(--text); font-size: 14px; outline: none; margin-bottom: 24px;
        }}
        .search-box input:focus {{ border-color: var(--accent); }}
        h2 {{ font-size: 16px; margin: 24px 0 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }}
        .tag-cloud {{ display: flex; flex-wrap: wrap; gap: 8px; }}
        .tag-chip {{
            background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
            padding: 8px 14px; border-radius: 20px; font-size: 13px; text-decoration: none; display: inline-flex; gap: 6px; align-items: center;
        }}
        .tag-chip:hover {{ border-color: var(--accent); color: var(--accent); }}
        .tag-count {{ color: var(--text-muted); font-size: 11px; }}
        .tag-chip.hidden {{ display: none; }}
    </style>
</head>
<body>
    <div class="container">
        <a href="/" class="logo-link">✨ Arc Comic</a>
        <div class="breadcrumb"><a href="/">Home</a> › Artists</div>
        <h1>✨ Browse Artists</h1>
        <div class="search-box">
            <input type="text" id="artistSearch" placeholder="Search artists...">
        </div>
        <h2>🔥 Popular Artists</h2>
        <div class="tag-cloud" id="popularArtists">
            {popular_html}
        </div>
        <h2>A–Z All Artists</h2>
        <div class="tag-cloud" id="allArtists">
            {all_html}
        </div>
    </div>
    <script>
        document.getElementById('artistSearch').addEventListener('input', function(e) {{
            const q = e.target.value.toLowerCase();
            document.querySelectorAll('#allArtists .tag-chip, #popularArtists .tag-chip').forEach(function(chip) {{
                const text = chip.textContent.toLowerCase();
                chip.classList.toggle('hidden', q.length > 0 && !text.includes(q));
            }});
        }});
    </script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(ARTISTS_INDEX_PATH), exist_ok=True)
    with open(ARTISTS_INDEX_PATH, 'w', encoding='utf-8') as f:
        f.write(html)

# ============== GOOGLE SEO FUNCTIONS ==============
def regenerate_sitemap(site_url):
    """
    Rebuilds sitemap.xml from scratch every time it's called, listing
    every real page on the site — not just the homepage. This replaces
    the old touch_sitemap_lastmod(), which only ever updated the
    homepage's own <lastmod> and never listed comic/tag/artist pages at
    all (a real gap flagged by Master — see handoff for the incident).

    URL sources (all read fresh from disk, same ground truth every other
    part of the site already uses — see is_code_already_posted(),
    regenerate_tags(), regenerate_artists()):
      - homepage: always included, lastmod = today (it changes on every
        post since Latest Upload/Popular sections are dynamic)
      - /works/<code>/  — one per _works/*.md, lastmod = that file's own
        `date:` front-matter field (the real post date, not "today" —
        an old comic's page didn't change just because a new comic was
        posted elsewhere)
      - /tags/<slug>/   — one per _tags/*.md, lastmod = the MOST RECENT
        date among that tag's own works (read from the work entries
        already embedded in the tag stub's `works:` list — see
        regenerate_tags()). A tag page's content only actually changes
        when a work is added to/removed from it, so this is the honest
        answer, not "today" for every tag on every run.
      - /artists/<slug>/ — same idea, from _artists/*.md
      - /tags/ and /artists/ index pages — included, lastmod = today
        (Popular Tags / A-Z counts are dynamic, same reasoning as home)

    Returns True if sitemap.xml was written (content actually changed
    from what's on disk), False otherwise — same True/False contract
    the old touch_sitemap_lastmod() had, since callers use this to
    decide whether to report "🟢 Google" pinged in the dashboard.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    urls = []  # list of (loc, lastmod) tuples

    urls.append((f"{site_url}/", today))
    urls.append((f"{site_url}/tags/", today))
    urls.append((f"{site_url}/artists/", today))

    if os.path.isdir(WORKS_DIR):
        for fname in sorted(os.listdir(WORKS_DIR)):
            if not fname.endswith(".md"):
                continue
            code = fname[:-3]
            try:
                with open(os.path.join(WORKS_DIR, fname), 'r', encoding='utf-8') as f:
                    content = f.read()
                front = yaml.safe_load(content.split("---")[1])
            except Exception:
                continue
            if not front:
                continue
            date_str = str(front.get("date", today)).split(" ")[0].split("T")[0]
            urls.append((f"{site_url}/works/{code}/", date_str or today))

    def _lastmod_from_stub(dir_path, url_prefix):
        if not os.path.isdir(dir_path):
            return
        for fname in sorted(os.listdir(dir_path)):
            if not fname.endswith(".md"):
                continue
            slug = fname[:-3]
            try:
                with open(os.path.join(dir_path, fname), 'r', encoding='utf-8') as f:
                    content = f.read()
                front = yaml.safe_load(content.split("---")[1])
            except Exception:
                continue
            if not front:
                continue
            work_dates = [
                str(w.get("date", "")).split(" ")[0].split("T")[0]
                for w in front.get("works", []) if w.get("date")
            ]
            latest = max(work_dates) if work_dates else today
            urls.append((f"{site_url}/{url_prefix}/{slug}/", latest))

    _lastmod_from_stub(TAGS_DIR, "tags")
    _lastmod_from_stub(ARTISTS_DIR, "artists")

    xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        xml_lines.append(f"  <url><loc>{loc}</loc><lastmod>{lastmod}</lastmod></url>")
    xml_lines.append('</urlset>')
    new_content = "\n".join(xml_lines) + "\n"

    sitemap_path = os.path.join(WORK_DIR, "sitemap.xml")
    old_content = None
    if os.path.exists(sitemap_path):
        try:
            with open(sitemap_path, 'r', encoding='utf-8') as f:
                old_content = f.read()
        except Exception:
            old_content = None

    if old_content == new_content:
        return False  # no-op, nothing actually changed

    try:
        with open(sitemap_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"🗺️ sitemap.xml regenerated: {len(urls)} URLs "
              f"({len(urls) - 3} works/tags/artists + 3 index pages)")
        return True
    except Exception as e:
        print(f"⚠️ sitemap regeneration failed: {e}")
        return False

def submit_indexnow(url, site_url, api_key):
    try:
        data = {
            "host": site_url.replace("https://", "").replace("http://", ""),
            "key": api_key,
            "keyLocation": f"{site_url}/{api_key}.txt",
            "urlList": [url]
        }
        r = requests.post("https://api.indexnow.org/IndexNow", json=data, timeout=15)
        if r.status_code in [200, 202]:
            print(f"🚀 IndexNow submitted: {url}")
            return True
    except Exception as e:
        print(f"⚠️ IndexNow failed: {e}")
    return False

def generate_indexnow_key():
    import secrets
    return secrets.token_hex(32)

def update_verification_tag(tag_html):
    """Insert or replace the Google verification meta tag inside index.html's
    real <head> tag (the HTML one, in the document body — not the word
    "<head>" that happens to appear inside the front-matter comment at the
    top of the file, which is what this used to match against).
    """
    index_path = os.path.join(WORK_DIR, "index.html")
    if not os.path.exists(index_path):
        print("⚠️ index.html not found, cannot inject verification tag")
        return False

    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()

    tag_html = tag_html.strip()

    # Split off the YAML front matter block (--- ... ---) so every
    # subsequent search/replace only ever touches the real HTML body below
    # it, never the comment text inside the front matter itself. This is
    # the fix for a real corruption bug: the old code searched the whole
    # raw file for the literal substring "<head>", which also matches the
    # front-matter comment "# page (own <head>, <style>, <body>)..." and
    # spliced the meta tag into the middle of that comment, breaking the
    # YAML and failing every GitHub Pages build.
    fm_match = re.match(r"\A(---\s*\n.*?\n---\s*\n)", html, re.DOTALL)
    if fm_match:
        front_matter = fm_match.group(1)
        body = html[len(front_matter):]
    else:
        front_matter = ""
        body = html

    # Remove any existing google-site-verification meta tag first
    # (belt-and-suspenders: also strip one if it got left behind inside the
    # front matter by the old buggy code, so re-saving repairs it).
    front_matter = re.sub(
        r'\s*<meta[^>]+name=["\']google-site-verification["\'][^>]*>\s*',
        '\n', front_matter, flags=re.IGNORECASE
    )
    body = re.sub(
        r'\s*<meta[^>]+name=["\']google-site-verification["\'][^>]*>\s*',
        '\n', body, flags=re.IGNORECASE
    )

    if tag_html:
        if "<head>" in body:
            body = body.replace("<head>", f"<head>\n    {tag_html}", 1)
        else:
            print("⚠️ No <head> tag found in index.html body")
            return False

    # Final safety net: if the front matter we're about to write doesn't
    # actually parse as valid YAML — most likely because the file was
    # already corrupted by the old version of this function before this
    # fix existed, and simply stripping the meta tag back out isn't enough
    # to un-corrupt a comment line it was spliced into — don't write
    # broken front matter again. Fall back to regenerating index.html from
    # the known-good template, then inject the tag into that clean copy
    # instead.
    front_matter_ok = is_valid_jekyll_front_matter(front_matter) if front_matter else True

    if not front_matter_ok:
        print("⚠️ index.html front matter was corrupted (likely by the old "
              "verification-tag bug) — regenerating index.html from the "
              "template before re-applying the verification tag")
        clean = INDEX_HTML_TEMPLATE
        clean_fm_match = re.match(r"\A(---\s*\n.*?\n---\s*\n)", clean, re.DOTALL)
        clean_front_matter = clean_fm_match.group(1) if clean_fm_match else ""
        clean_body = clean[len(clean_front_matter):]
        if tag_html and "<head>" in clean_body:
            clean_body = clean_body.replace("<head>", f"<head>\n    {tag_html}", 1)
        html = clean_front_matter + clean_body
    else:
        html = front_matter + body

    with open(index_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print("✅ Verification tag updated in index.html")
    return True

# ============== SITE SCRAPER (nhentai.net) ==============
def scrape_site(code, domain="https://nhentai.net"):
    """
    Scrapes title and tags from nhentai.net for a given work code.
    Selectors confirmed for nhentai's gallery page structure:
      Title: h1.title .pretty
      Tags:  section#tags a.tagchip .name
    """
    try:
        url = f"{domain.rstrip('/')}/g/{code}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
        }
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        # Use r.content (raw bytes), not r.text. requests guesses encoding
        # from HTTP headers which are often wrong/missing charset info;
        # BeautifulSoup reading raw bytes detects encoding from the page's
        # own <meta charset> tag instead, which is far more reliable and
        # fixes mangled special characters (e.g. "IDOLM@STER" turning into
        # "IDOLMï¼STER").
        soup = BeautifulSoup(r.content, 'html.parser')

        title_elem = soup.select_one("h1.title .pretty")
        if not title_elem:
            title_elem = soup.select_one("h1.title")
        title = title_elem.get_text(strip=True) if title_elem else f"Work {code}"

        tag_elems = soup.select("section#tags a.tagchip .name")
        tags = [t.get_text(strip=True) for t in tag_elems if t.get_text(strip=True)]

        print(f"🔍 Scraped '{title}' with {len(tags)} tags for code {code}")
        return title, tags
    except requests.exceptions.HTTPError as e:
        print(f"⚠️ Scrape HTTP error for {code}: {e}")
        return f"Work {code}", []
    except Exception as e:
        print(f"⚠️ Scrape error for {code}: {e}")
        return f"Work {code}", []

# ============== MARKDOWN GENERATOR ==============
def generate_md(code, title, author, categories, full_color, cheating, 
                language, rating, tags, cover_path, telegram_post_url, date_str):

    tags_yaml = json.dumps(tags)
    fc_bool = "true" if full_color.lower() == "yes" else "false"
    fc_label = "Yes" if full_color.lower() == "yes" else "No"
    ntr_bool = "true" if cheating.lower() == "yes" else "false"
    ntr_label = "Yes" if cheating.lower() == "yes" else "No"

    # Cover URL: R2 (external, no repo storage cost) once configured,
    # else the old in-repo /covers/<code>.jpg path as a fallback so
    # posting still works before R2 settings are filled in.
    cfg = load_config()
    if r2_upload.is_configured(cfg):
        cover_url = r2_upload.get_cover_url(cfg, code)
    else:
        cover_url = f"/covers/{code}.jpg"

    md_lines = [
        "---",
        "layout: post",
        f"code: {code}",
        f'title: "{title.replace(chr(34), chr(92)+chr(34))}"',
        f'author: "{author}"',
        f'categories: ["{categories}"]',
        f"tags: {tags_yaml}",
        f"full_color: {fc_bool}",
        f"ntr: {ntr_bool}",
        f'language: "{language}"',
        f"rating: {rating}",
        f'cover: "{cover_url}"',
        f'telegram_post: "{telegram_post_url}"',
        f"date: {date_str}",
        "views: 0",
        "---",
        "",
        f"# {title}",
        "",
        f"![Cover]({cover_url})",
        "",
        f"**Author:** {author} | **Code:** {code} | **Rating:** ⭐ {rating}  ",
        f"**Full Color:** {fc_label} | **NTR:** {ntr_label} | **Language:** {language.title()}  ",
        f"**Tags:** {', '.join(tags) if tags else 'N/A'}",
        "",
        "---",
        "",
        f"[📖 Read on Telegram]({telegram_post_url})"
    ]
    return "\n".join(md_lines)

def patch_work_rating(code, new_rating):
    """Repairs just the rating on an already-published .md file, in
    place, without touching any other field. Used by the dashboard's
    one-click 'Fix Rating' button for posts that got stuck at 0.0 from
    the bold-label/monospace-value parsing bug (fixed in normalize_text/
    parse_post_fields going forward, but that fix can't retroactively
    correct files that already got the wrong value written to disk).
    Returns True if a change was made, False if the file didn't exist or
    already had this rating."""
    md_path = os.path.join(WORKS_DIR, f"{code}.md")
    if not os.path.exists(md_path):
        return False, "File not found"
    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    new_content = re.sub(
        r"^rating:\s*[\d.]+\s*$", f"rating: {new_rating}",
        content, count=1, flags=re.MULTILINE
    )
    # Also fix the human-readable "**Rating:** ⭐ X" line in the post body
    # so the two don't disagree if anyone opens the raw file.
    new_content = re.sub(
        r"(\*\*Rating:\*\*\s*⭐\s*)[\d.]+",
        rf"\g<1>{new_rating}",
        new_content
    )

    if new_content == content:
        return False, "Rating already correct or field not found"

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    return True, None

# ============== STATS TRACKING ==============
def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    return {"total_posts": 0, "posts": [], "last_error": None, "last_error_time": None}

def save_stats(stats):
    # Keep only the most recent 50 entries so the file doesn't grow forever
    stats["posts"] = stats.get("posts", [])[-50:]
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=2)

def record_post(code, title, success, error=None, google_pinged=False, indexnow_pinged=False, post_url=None):
    stats = load_stats()
    entry = {
        "code": code,
        "title": title,
        "success": success,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
        "google_pinged": google_pinged,
        "indexnow_pinged": indexnow_pinged,
        "post_url": post_url,
    }
    stats["posts"].append(entry)
    if success:
        stats["total_posts"] = stats.get("total_posts", 0) + 1
    else:
        stats["last_error"] = error
        stats["last_error_time"] = entry["time"]
    save_stats(stats)

def delete_post_record(code, time):
    """Removes a post from stats history and deletes its underlying files
    (md + cover) from disk, then pushes the deletion to GitHub."""
    stats = load_stats()
    stats["posts"] = [p for p in stats["posts"] if not (p["code"] == code and p["time"] == time)]
    save_stats(stats)

    md_path = os.path.join(WORKS_DIR, f"{code}.md")
    cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
    for path in (md_path, cover_path):
        if os.path.exists(path):
            os.remove(path)

    # Cover may live on R2 instead of (or in addition to) disk — clean
    # that up too so deleted posts don't leave orphaned images behind.
    cfg = load_config()
    if r2_upload.is_configured(cfg):
        r2_upload.delete_cover(cfg, code)

# ============== R2 MIGRATION (existing posts) ==============
_r2_migration_lock = threading.Lock()
_r2_migration_running = False
_r2_migration_last_result = None

def _migrate_one_post_to_r2(cfg, code):
    """Uploads a single existing cover to R2 and rewrites that post's
    .md front matter + image tag to point at the new URL. Returns True
    if it changed anything (so the caller knows to push), False if
    already migrated or nothing to do."""
    cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
    md_path = os.path.join(WORKS_DIR, f"{code}.md")
    if not os.path.exists(cover_path) or not os.path.exists(md_path):
        return False

    r2_url = r2_upload.upload_cover(cfg, cover_path, code)
    if not r2_url:
        return False  # upload failed, leave this post untouched, try again later

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    old_local = f"/covers/{code}.jpg"
    if old_local not in content:
        # Nothing to rewrite (already pointing elsewhere) — but the
        # upload above still happened, so still clean up the local file.
        try:
            os.remove(cover_path)
        except Exception:
            pass
        return False

    new_content = content.replace(old_local, r2_url)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    try:
        os.remove(cover_path)
    except Exception:
        pass
    return True

def run_r2_migration():
    """
    Walks every existing post, uploads its local cover to R2 (skipping
    any that are already migrated or have no local cover), rewrites the
    .md front matter, and pushes in small batches so a single failed
    push doesn't lose progress on everything already done.
    Safe to call repeatedly — already-migrated posts are skipped fast
    since their cover file no longer exists locally.
    """
    cfg = load_config()
    if not r2_upload.is_configured(cfg):
        return {"status": "error", "message": "R2 is not configured yet"}

    if not os.path.isdir(WORKS_DIR):
        return {"status": "ok", "migrated": 0, "message": "No posts found"}

    codes = [fn[:-3] for fn in os.listdir(WORKS_DIR) if fn.endswith(".md")]
    migrated = []
    failed = []
    BATCH_SIZE = 20

    batch_paths = []
    for i, code in enumerate(codes):
        try:
            changed = _migrate_one_post_to_r2(cfg, code)
            if changed:
                migrated.append(code)
                batch_paths.append(os.path.join("_works", f"{code}.md"))
                batch_paths.append(os.path.join("covers", f"{code}.jpg"))
        except Exception as e:
            print(f"⚠️ R2 migration failed for {code}: {e}")
            failed.append(code)

        # Push every BATCH_SIZE migrated posts so progress is never lost
        # to a single crashed/interrupted run, and so the repo shrinks
        # incrementally instead of one giant commit at the very end.
        if len(batch_paths) >= BATCH_SIZE * 2 or i == len(codes) - 1:
            if batch_paths:
                success, err = git_push(cfg, "r2-migration",
                                         f"Migrate {len(batch_paths)//2} cover(s) to R2", batch_paths)
                if not success:
                    print(f"⚠️ R2 migration push failed: {err}")
                batch_paths = []

    return {
        "status": "ok",
        "migrated": len(migrated),
        "failed": len(failed),
        "failed_codes": failed[:20],  # cap so the response doesn't balloon
        "total_checked": len(codes),
    }

def _run_r2_migration_thread():
    global _r2_migration_running, _r2_migration_last_result
    try:
        result = run_r2_migration()
    except Exception as e:
        msg = str(e) or repr(e) or type(e).__name__
        result = {"status": "error", "message": msg}
        print(f"❌ R2 migration crashed: {msg}")
    with _r2_migration_lock:
        _r2_migration_last_result = result
        _r2_migration_running = False

# ============== GIT OPERATIONS ==============
def ensure_origin_remote(cfg):
    """Keeps the 'origin' remote's URL in sync with the current token/repo,
    and always operates through the named remote (never an ad-hoc URL
    string). Using a throwaway URL for pull/push instead of 'origin' was
    the root cause of a bug where git's local tracking branch (origin/main)
    never updated, making `git status` permanently claim the branch was
    "ahead" and causing manual `git push` to fail with non-fast-forward
    even though the bot's own pushes were succeeding."""
    repo = cfg.get("github_repo", "ArcComic/arccomic.github.io")
    token = cfg.get("github_token", "")
    if not token:
        return False
    remote_url = f"https://{token}@github.com/{repo}.git"

    remotes = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout
    if "origin" in remotes.split():
        subprocess.run(["git", "remote", "set-url", "origin", remote_url],
                       check=False, capture_output=True)
    else:
        subprocess.run(["git", "remote", "add", "origin", remote_url],
                       check=False, capture_output=True)
    return True

def git_push(cfg, code, title, batch_paths=None):
    """Returns (success: bool, error_detail: str|None).

    batch_paths: relative paths (from WORK_DIR) that THIS batch actually
    created/changed — e.g. _works/<code>.md, _tags/*.md, covers/<code>.jpg
    for each item in the batch, plus regenerated index/tag pages. When
    given, only these paths are `git add`ed, never a blanket `git add .`.

    This matters because of the cover-deletion bug: a blanket `git add .`
    stages EVERY difference between the working tree and the last commit,
    including files that went missing for reasons that have nothing to do
    with this batch. Specifically: cleanup_pushed_covers() deletes local
    covers/<code>.jpg files in a background thread sometime after a
    previous push, once it's confirmed they're live on GitHub Pages —
    that deletion is meant to be LOCAL-ONLY (phone storage cleanup); the
    cover should stay in the git repo and on the live site forever. But
    a blanket `git add .` on the next batch's push saw those covers
    missing from disk and committed that as a deletion, removing them
    from the repo and the live site too — the opposite of what was
    intended. Scoping the add to only this batch's own paths makes that
    class of bug structurally impossible: a file cleanup deletes locally
    is simply never in batch_paths, so it can never be staged as removed.
    Falls back to `git add .` only if batch_paths isn't provided (should
    not happen on the normal push path after this fix).
    """
    try:
        os.chdir(WORK_DIR)
        if batch_paths:
            existing = [p for p in batch_paths if os.path.exists(os.path.join(WORK_DIR, p))]
            if existing:
                subprocess.run(["git", "add", "--"] + existing, check=True, capture_output=True)
        else:
            subprocess.run(["git", "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Add work #{code}: {title}"], 
                      check=False, capture_output=True)

        if not ensure_origin_remote(cfg):
            msg = "No GitHub token configured"
            print(f"⚠️ {msg}")
            return False, msg

        # Pull first so a diverged remote (e.g. edits made on github.com,
        # or a previous manual push) doesn't cause a rejected push.
        # --no-rebase makes the merge strategy explicit so this never
        # depends on the phone's global git config (pull.rebase) being
        # set — an unset config causes git to refuse to pull at all on
        # divergent branches, with no clean recovery, which was the root
        # cause of every push silently failing after any manual git action.
        pull_result = subprocess.run(
            ["git", "pull", "--no-rebase", "--no-edit", "origin", "main"],
            check=False, capture_output=True, text=True
        )
        if pull_result.returncode != 0:
            print(f"⚠️ Git pull warning: {pull_result.stderr.strip()}")

        push_result = subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            check=False, capture_output=True, text=True
        )
        if push_result.returncode == 0:
            print(f"✅ Pushed work #{code}")
            return True, None
        else:
            err = push_result.stderr.strip()
            print(f"❌ Git push failed: {err}")
            return False, f"Git push failed: {err[:200]}"
    except Exception as e:
        msg = str(e) or repr(e) or type(e).__name__
        print(f"❌ Git push error: {msg}")
        return False, msg

# ============== TELEGRAM MESSAGE PARSING ==============
def normalize_text(text):
    """Collapse Telegram's non-breaking spaces and other invisible
    formatting-boundary characters (which appear when mixing bold and
    monospace styles, e.g. a bold label right before a monospace value —
    see the Rating-showing-as-0.0 bug) down to plain ASCII spaces or
    nothing. Also strips visible Markdown syntax (**bold**, __bold__,
    `code`) as a safety net — Telethon's client is set to parse_mode=None
    so this shouldn't normally be needed (see Bug 11), but this keeps the
    parser robust even if that ever regresses or a message somehow
    contains literal markdown source.

    Rather than list specific zero-width characters one at a time (which
    only ever covers the ones we've already noticed break something),
    this strips the whole Unicode "format" category (Cf) — bidi marks,
    joiners, and other invisible formatting-boundary characters all live
    in that category, so this catches variants we haven't seen yet too."""
    text = text.replace("\xa0", " ")  # non-breaking space -> real space
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'__(.+?)__', r'\1', text)      # __bold__
    text = re.sub(r'`(.+?)`', r'\1', text)        # `code`
    return text

def extract_field(text, *labels, pattern=r"(.+)"):
    """Try each label variant until one matches. Case-insensitive,
    tolerant of extra/odd whitespace after normalization."""
    for label in labels:
        m = re.search(re.escape(label) + r"\s*:\s*" + pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None

def parse_post_fields(raw_text):
    """Parses a channel post into structured fields. Returns a dict, or
    None if this doesn't look like a real comic post (e.g. it's a sponsor
    post, announcement, or other non-comic content in the same channel).

    A real comic post must have Code + Author + Categories all present —
    that's the specific fingerprint of your comic format. Requiring only
    "Code" was too loose and risked matching stray numbers in unrelated
    posts; requiring all three virtually guarantees we only ever act on
    genuine comic submissions.
    """
    text = normalize_text(raw_text)

    code = extract_field(text, "🌟Code", "Code", pattern=r"(\d+)")
    author = extract_field(text, "✨Author", "Author")
    categories = extract_field(text, "⚡Categories", "Categories")

    if not (code and author and categories):
        return None  # not a comic post — silently skip (sponsor/update/etc)

    fields = {
        "code": code,
        "author": author,
        "categories": categories,
        "full_color": extract_field(text, "💫Full color", "Full color") or "no",
        "cheating": extract_field(text, "🌙Cheating", "Cheating") or "no",
        "language": extract_field(text, "⚡Language", "Language") or "english",
        "rating": extract_field(text, "⭐Rating", "Rating", pattern=r"\(?([\d.]+)\)?") or "0.0",
    }
    return fields

def is_code_already_posted(code):
    """
    Single source of truth for duplicate detection, used by both the live
    Telegram handler and the backlog scanner. Checks the actual _works/
    directory on disk (not just in-memory state), so it's correct even
    across bot restarts. A code counts as posted if its .md file exists,
    OR if it's currently sitting in the pending queue (staged but not yet
    pushed) — this second check matters for the backlog scanner, since a
    huge batch could otherwise re-stage the same code multiple times
    before the first one actually reaches disk as a permanent file.
    """
    md_path = os.path.join(WORKS_DIR, f"{code}.md")
    if os.path.exists(md_path):
        return True
    with _pending_lock:
        if any(item["code"] == code for item in _pending_queue):
            return True
    return False

# ============== POSTING QUEUE (batch + debounce) ==============
# Artists submit comics in bursts (20-25 at once, ~1-2 min apart). Pushing
# git + pinging Google/IndexNow separately for each one is wasteful and
# spams the ping APIs. Instead we stage each valid post's files locally,
# then wait for a quiet period (no new post for PENDING_TIMEOUT_SECONDS)
# before committing everything as one batch and pinging once. The
# dashboard's "Push Now" button can force an immediate flush at any time.
PENDING_TIMEOUT_SECONDS = 10 * 60  # 10 minutes
_pending_lock = threading.Lock()
_pending_queue = []          # list of dicts: {code, title, post_url}
_pending_timer = None        # threading.Timer handle

def _flush_pending_queue_sync():
    """Runs the actual batch push. Called either by the debounce timer
    firing or by the manual /api/push_now endpoint."""
    global _pending_timer
    with _pending_lock:
        if not _pending_queue:
            _pending_timer = None
            return
        batch = list(_pending_queue)
        _pending_queue.clear()
        _pending_timer = None

    cfg = load_config()
    codes = ", ".join(item["code"] for item in batch)
    print(f"🚚 Flushing batch of {len(batch)} post(s): {codes}")

    # Regenerate tag/artist pages before pushing so the new/updated
    # membership is included in the same commit as the new works.
    try:
        regenerate_tag_pages()
    except Exception as e:
        print(f"⚠️ Tag page regeneration failed: {e}")
    try:
        regenerate_artist_pages()
    except Exception as e:
        print(f"⚠️ Artist page regeneration failed: {e}")

    # Build an explicit list of paths this batch actually touched, so
    # git_push() never has to fall back to a blanket `git add .` (see
    # git_push's docstring for why that blanket add was the root cause
    # of covers getting deleted from GitHub). Each batch item's own .md
    # and cover are included by exact path; _tags/ and _artists/ are
    # entirely bot-generated/regenerated above and never touched by the
    # local-only cover cleanup, so it's safe to include them in full.
    batch_paths = []
    for item in batch:
        batch_paths.append(os.path.join("_works", f"{item['code']}.md"))
        batch_paths.append(os.path.join("covers", f"{item['code']}.jpg"))
    for d in (TAGS_DIR, ARTISTS_DIR):
        if os.path.isdir(d):
            for fname in os.listdir(d):
                batch_paths.append(os.path.relpath(os.path.join(d, fname), WORK_DIR))
    # sitemap.xml is regenerated below (before the push, so its own
    # changes are included in this same commit) — must be in
    # batch_paths or the scoped git add from Bug 13's fix would silently
    # exclude it, leaving the live sitemap stale forever.
    batch_paths.append("sitemap.xml")

    site_url = "https://arccomic.github.io"
    sitemap_updated = False
    if cfg.get("auto_ping_google", True):
        sitemap_updated = regenerate_sitemap(site_url)

    pushed, push_error = git_push(cfg, "batch", f"Add {len(batch)} work(s): {codes}",
                                   batch_paths=batch_paths)

    indexnow_pinged_count = 0
    if pushed:
        if cfg.get("use_indexnow", True):
            api_key = cfg.get("indexnow_key", "")
            if api_key:
                for item in batch:
                    if submit_indexnow(item["post_url"], site_url, api_key):
                        indexnow_pinged_count += 1

    for item in batch:
        record_post(item["code"], item["title"], success=pushed, error=push_error,
                     google_pinged=sitemap_updated,
                     indexnow_pinged=indexnow_pinged_count > 0,
                     post_url=item["post_url"])

    print(f"✅ Batch flush complete: {len(batch)} post(s), pushed={pushed}")

    # Auto-cleanup: once this batch is confirmed pushed AND live on the
    # actual site (not just committed — see Bug #10, a successful git
    # push does not guarantee GitHub Pages' build succeeded), delete the
    # local cover images for these codes. The _works/*.md files are
    # NEVER touched by this — those stay forever, they're what
    # is_code_already_posted() checks to prevent duplicate posts, and
    # they're tiny (~1KB each) so there's no storage reason to remove
    # them. Only covers/ (the large files) get cleaned, and only after
    # verifying the cover is actually reachable on the live site.
    if pushed:
        codes_to_check = [item["code"] for item in batch]
        threading.Thread(
            target=cleanup_pushed_covers,
            args=(codes_to_check,),
            daemon=True
        ).start()

def cleanup_pushed_covers(codes, max_wait_seconds=300, poll_interval=15):
    """
    Waits for GitHub Pages to actually finish building and serving each
    cover, then deletes the local copy — never before confirming it's
    live. A successful `git push` only means the commit reached GitHub;
    the Pages build/deploy that actually makes the cover reachable at
    its public URL happens afterward and can fail (see Bug #10's
    "Invalid YAML front matter" build failure, which broke the site for
    an entire session despite every push succeeding). Deleting on push
    success alone would risk deleting a cover before it's really live,
    losing it if the build then fails. Polls each cover's public URL
    with a HEAD request (cheap, no auth, no API rate limit — this is
    the static Pages site, not the GitHub API) until it's reachable or
    max_wait_seconds elapses, then deletes only the ones confirmed live.
    Any code that never comes back reachable within the wait window is
    left alone on disk — safe default, just means it's cleaned up on
    the next successful batch's check instead (this function runs again
    every batch, so nothing is permanently stuck).
    """
    site_url = "https://arccomic.github.io"
    remaining = set(codes)
    confirmed = []
    deadline = time.time() + max_wait_seconds

    while remaining and time.time() < deadline:
        for code in list(remaining):
            cover_url = f"{site_url}/covers/{code}.jpg"
            try:
                resp = requests.head(cover_url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    confirmed.append(code)
                    remaining.discard(code)
            except Exception:
                pass  # not live yet, or transient network issue — retry next poll
        if remaining:
            time.sleep(poll_interval)

    deleted_count = 0
    freed_bytes = 0
    for code in confirmed:
        cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
        if os.path.exists(cover_path):
            try:
                freed_bytes += os.path.getsize(cover_path)
                os.remove(cover_path)
                deleted_count += 1
            except Exception as e:
                print(f"⚠️ Could not delete cover for #{code}: {e}")

    if deleted_count:
        print(f"🧹 Auto-cleanup: {deleted_count} confirmed-live cover(s) removed, "
              f"{freed_bytes / 1024:.0f} KB freed")
    if remaining:
        print(f"⏳ Auto-cleanup: {len(remaining)} cover(s) not yet confirmed live "
              f"after {max_wait_seconds}s, left on disk (will retry on next batch)")

def queue_pending_flush():
    """(Re)starts the debounce timer. Any new post arriving resets the
    10-minute countdown, so the batch only flushes once submissions
    actually stop coming in."""
    global _pending_timer
    with _pending_lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
        _pending_timer = threading.Timer(PENDING_TIMEOUT_SECONDS, _flush_pending_queue_sync)
        _pending_timer.daemon = True
        _pending_timer.start()

def force_flush_now():
    """Used by the dashboard's 'Push Now' button to bypass the timer."""
    global _pending_timer
    with _pending_lock:
        if _pending_timer is not None:
            _pending_timer.cancel()
            _pending_timer = None
    _flush_pending_queue_sync()

def get_pending_count():
    with _pending_lock:
        return len(_pending_queue)

# ============== SHARED STAGING LOGIC ==============
async def stage_comic(code, fields, message_date, message_id, cover_bytes_source):
    """
    The actual "scrape + download cover + write .md + queue for push" work,
    shared by both the live Telegram handler and the backlog processor so
    they can never silently drift apart into two different behaviors.

    cover_bytes_source: an async callable that downloads the cover image
    to cover_path when called (photo.get_file()... for live posts, or a
    Telethon download for backlog posts) — kept as a callback so this
    function doesn't need to know which Telegram library produced it.
    Returns True if staged successfully, False on error (already logged).
    """
    cfg = load_config()
    title = f"Work {code}"
    try:
        date_str = message_date.strftime("%Y-%m-%d")
        channel = cfg.get('channel_username', '@ArcComic').replace('@', '')
        telegram_url = f"https://t.me/{channel}/{message_id}"
        site_url = "https://arccomic.github.io"
        post_url = f"{site_url}/works/{code}/"

        print(f"📥 Staging code {code}...")

        domain = cfg.get("site_domain", "https://nhentai.net")
        title, tags = scrape_site(code, domain)

        cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
        if cover_bytes_source:
            await cover_bytes_source(cover_path)
            print(f"📸 Cover saved: {cover_path}")

            # Upload to R2 if configured. This runs for both live posts
            # and backlog processing since they both go through this
            # shared function. On failure we just log it and keep the
            # local file — generate_md() already fell back to the old
            # /covers/ path in that case, so posting never breaks over
            # an R2 hiccup.
            if r2_upload.is_configured(cfg):
                r2_url = r2_upload.upload_cover(cfg, cover_path, code)
                if r2_url:
                    print(f"☁️ Cover uploaded to R2: {r2_url}")
                    # Local copy no longer needed once it's safely on R2 —
                    # this is what keeps the git repo out of the 1GB zone.
                    try:
                        os.remove(cover_path)
                    except Exception:
                        pass
                else:
                    print(f"⚠️ R2 upload failed for {code}, keeping local copy as fallback")
        else:
            print("⚠️ No photo attached to this post")

        md_content = generate_md(
            code, title, fields["author"], fields["categories"],
            fields["full_color"], fields["cheating"], fields["language"],
            fields["rating"], tags, cover_path, telegram_url, date_str
        )

        md_path = os.path.join(WORKS_DIR, f"{code}.md")
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)

        cfg["last_message_id"] = max(cfg.get("last_message_id", 0), message_id)
        save_config(cfg)

        with _pending_lock:
            _pending_queue.append({"code": code, "title": title, "post_url": post_url})
            pending_count = len(_pending_queue)

        queue_pending_flush()
        print(f"⏳ Staged: {title} (Code: {code}) — {pending_count} pending")
        return True

    except Exception as e:
        print(f"❌ Error processing code {code}: {e}")
        record_post(code, title, success=False, error=str(e))
        return False

# ============== BACKLOG SCANNER (Telegram history) ==============
# The Bot API (used everywhere else in this file) cannot read channel
# history — bots only ever see messages that arrive while they're
# running. Scanning old posts requires Telethon, logged into Master's own
# personal Telegram account (a one-time login, session saved to disk).
# This is a materially different, more sensitive credential than the bot
# token, so its session file lives in SECRETS_DIR like everything else
# sensitive, and this scanner is READ-ONLY: it only ever calls
# iter_messages(), never sends/edits/deletes anything.
TELETHON_SESSION_PATH = os.path.join(SECRETS_DIR, "telethon_session")
SCAN_BATCH_SIZE = 50            # comics staged per processing batch
SCAN_BATCH_DELAY_SECONDS = 60   # pause between batches — avoid hammering nhentai

def load_backlog_state():
    if os.path.exists(BACKLOG_FILE):
        with open(BACKLOG_FILE, 'r') as f:
            return json.load(f)
    return {
        "status": "idle",  # idle | scanning | scan_complete | processing | error
        "found_codes": [],       # codes discovered by the scan, not yet processed
        "processed_codes": [],   # codes already staged/pushed by the backlog processor
        "skipped_duplicate": 0,
        "error": None,
        "last_scan_time": None,
        "total_messages_scanned": 0,
    }

def save_backlog_state(state):
    with open(BACKLOG_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# Telethon's client is bound to whichever asyncio event loop it was
# connected on. Flask can (and does) service different requests on
# different worker threads, each with its own event loop — so a client
# created during /request_code and then touched again during /submit_code
# on a *different* thread crashes with "asyncio event loop must not
# change after connection". The fix: run ALL Telethon work (login steps,
# scanning, batch processing) inside one single dedicated background
# thread that owns one persistent event loop for the client's entire
# lifetime. Flask routes never touch the client directly — they only
# push small job requests onto a thread-safe queue and read results back
# from another queue.
import queue as _queue_module

_telethon_job_queue = _queue_module.Queue()
_telethon_result_queue = _queue_module.Queue()
_telethon_worker_started = False
_telethon_worker_lock = threading.Lock()

def _telethon_worker_loop():
    """
    The one and only thread that ever creates or touches a Telethon
    client. Runs its own asyncio event loop for the lifetime of the
    process and processes jobs one at a time from the queue.
    """
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        print("📦 Installing Telethon (needed for backlog scanning)...")
        subprocess.run([sys.executable, "-m", "pip", "install", "telethon",
                        "--break-system-packages", "-q"], check=False)
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

    client = None  # created lazily once we know api_id/api_hash

    async def ensure_client(api_id, api_hash):
        nonlocal client
        if client is None:
            client = TelegramClient(TELETHON_SESSION_PATH, int(api_id), api_hash, loop=loop)
            # Bug 11 root cause: by default Telethon's .text returns the
            # message re-rendered as Markdown source (e.g. "**Code:**"
            # instead of "Code:"), because Telethon's default parse_mode
            # is "markdown". The LIVE bot (python-telegram-bot) never
            # showed this because it hands us already-stripped plain
            # text — so the same regex that works live silently failed
            # on every single history message scanned via Telethon,
            # since "**Code:**" doesn't match the parser's "Code:"
            # pattern. Setting parse_mode=None makes Telethon's .text
            # return plain text with formatting entities stripped,
            # matching what the live pipeline already sees, so the same
            # parse_post_fields() works identically for both paths.
            client.parse_mode = None
        if not client.is_connected():
            await client.connect()
        return client

    async def handle_job(job):
        action = job["action"]
        try:
            if action == "check_authorized":
                c = await ensure_client(job["api_id"], job["api_hash"])
                return {"status": "ok", "authorized": await c.is_user_authorized()}

            elif action == "request_code":
                c = await ensure_client(job["api_id"], job["api_hash"])
                if await c.is_user_authorized():
                    return {"status": "already_authorized"}
                await c.send_code_request(job["phone"])
                return {"status": "code_sent"}

            elif action == "submit_code":
                c = await ensure_client(job["api_id"], job["api_hash"])
                try:
                    await c.sign_in(job["phone"], job["code"])
                except SessionPasswordNeededError:
                    if not job.get("password"):
                        return {"status": "needs_password"}
                    await c.sign_in(password=job["password"])
                return {"status": "ok"}

            elif action == "scan":
                c = await ensure_client(job["api_id"], job["api_hash"])
                state = load_backlog_state()
                found = []
                scanned = 0
                logged_samples = 0
                async for message in c.iter_messages(job["channel"]):
                    scanned += 1
                    text = message.text or message.message or ""
                    # Temporary diagnostic: the scan has been finding 0
                    # matches across hundreds of real comic posts, despite
                    # the same format parsing correctly when copy-pasted
                    # cleanly. Log the raw text (repr'd, so any invisible
                    # formatting characters are visible) for the first few
                    # messages that contain "Code" but still fail to
                    # parse, so we can see exactly what Telethon is
                    # actually returning for old messages — remove once
                    # the root cause is confirmed.
                    fields = parse_post_fields(text)
                    if not fields and logged_samples < 3 and "code" in text.lower():
                        print(f"🔬 Sample non-matching message (id={message.id}): {text!r}")
                        logged_samples += 1
                    if not fields:
                        continue
                    code = fields["code"]
                    if is_code_already_posted(code) or code in found:
                        state["skipped_duplicate"] = state.get("skipped_duplicate", 0) + 1
                        continue
                    found.append(code)
                    if scanned % 200 == 0:
                        print(f"🔍 Scanned {scanned} messages, found {len(found)} new comics so far...")
                state["found_codes"] = found
                state["total_messages_scanned"] = scanned
                state["status"] = "scan_complete"
                state["last_scan_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_backlog_state(state)
                print(f"✅ Backlog scan complete: {scanned} messages scanned, {len(found)} new comics found")
                return {"status": "ok"}

            elif action == "process_batch":
                c = await ensure_client(job["api_id"], job["api_hash"])
                state = load_backlog_state()
                batch = state["found_codes"][:SCAN_BATCH_SIZE]
                processed_this_run = 0
                for code in batch:
                    if is_code_already_posted(code):
                        state["found_codes"].remove(code)
                        continue
                    target_message = None
                    async for message in c.iter_messages(job["channel"], search=f"Code: {code}", limit=5):
                        fields = parse_post_fields(message.text or message.message or "")
                        if fields and fields["code"] == code:
                            target_message = message
                            break
                    if not target_message:
                        print(f"⚠️ Could not re-locate message for code {code}, skipping")
                        state["found_codes"].remove(code)
                        continue
                    fields = parse_post_fields(target_message.text or target_message.message or "")

                    async def download_cover_via_telethon(cover_path, msg=target_message, tclient=c):
                        if msg.photo:
                            await tclient.download_media(msg.photo, file=cover_path)

                    has_photo = bool(target_message.photo)
                    success = await stage_comic(
                        code, fields, target_message.date, target_message.id,
                        download_cover_via_telethon if has_photo else None
                    )
                    state["found_codes"].remove(code)
                    if success:
                        state["processed_codes"].append(code)
                        processed_this_run += 1
                    save_backlog_state(state)
                    await _asyncio.sleep(2)
                print(f"✅ Backlog batch complete: {processed_this_run} comics staged")
                return {"status": "ok", "processed": processed_this_run}

            elif action == "recover_covers":
                # Repair job for the cover-deletion bug: re-downloads just
                # the cover image for any code whose _works/<code>.md
                # already exists but whose cover is missing both locally
                # AND on the live site. Never re-scrapes nhentai, never
                # touches the .md, never risks a duplicate post — this is
                # strictly "find the original Telegram post for this code
                # again and pull the photo".
                c = await ensure_client(job["api_id"], job["api_hash"])
                codes = job["codes"]
                channel = job["channel"]
                recovered = []
                still_missing = []
                for code in codes:
                    target_message = None
                    async for message in c.iter_messages(channel, search=f"Code: {code}", limit=5):
                        fields = parse_post_fields(message.text or message.message or "")
                        if fields and fields["code"] == code:
                            target_message = message
                            break
                    if not target_message or not target_message.photo:
                        print(f"⚠️ Recovery: could not find original post/photo for code {code}")
                        still_missing.append(code)
                        continue
                    cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
                    try:
                        await c.download_media(target_message.photo, file=cover_path)
                        recovered.append(code)
                        print(f"📸 Recovered cover for #{code}")
                    except Exception as e:
                        print(f"❌ Recovery download failed for #{code}: {e}")
                        still_missing.append(code)
                    await _asyncio.sleep(2)
                return {"status": "ok", "recovered": recovered, "still_missing": still_missing}

            elif action == "fix_rating":
                # One-click dashboard repair for posts affected by the
                # bold-label/monospace-value parsing bug (rating stuck at
                # 0.0 even though the original post had a real rating).
                # Re-locates the original Telegram post by its code and
                # re-runs the now-fixed parser against it, then reports
                # back the correct rating for the caller to write into
                # the .md file — this job only reads from Telegram, it
                # doesn't touch any files itself, matching how the rest
                # of this worker stays file-I/O-free.
                c = await ensure_client(job["api_id"], job["api_hash"])
                code = job["code"]
                channel = job["channel"]
                target_message = None
                async for message in c.iter_messages(channel, search=f"Code: {code}", limit=5):
                    fields = parse_post_fields(message.text or message.message or "")
                    if fields and fields["code"] == code:
                        target_message = message
                        break
                if not target_message:
                    return {"status": "error",
                            "message": f"Could not find the original Telegram post for code {code}"}
                fields = parse_post_fields(target_message.text or target_message.message or "")
                return {"status": "ok", "code": code, "rating": fields["rating"]}

            return {"status": "error", "message": f"Unknown action: {action}"}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def run():
        while True:
            job = await loop.run_in_executor(None, _telethon_job_queue.get)
            result = await handle_job(job)
            _telethon_result_queue.put(result)

    loop.run_until_complete(run())

def _ensure_telethon_worker():
    global _telethon_worker_started
    with _telethon_worker_lock:
        if not _telethon_worker_started:
            threading.Thread(target=_telethon_worker_loop, daemon=True).start()
            _telethon_worker_started = True

def _telethon_call(action, timeout=120, **kwargs):
    """Sends one job to the dedicated Telethon thread and blocks (this
    calling thread only, not the whole app) until it responds. Safe to
    call from any Flask request thread since the actual client work
    always happens on the one dedicated worker thread.

    timeout: seconds to wait for a result before giving up. Defaults to
    120s for quick actions (login, single scans). Long-running actions
    (recover_covers downloading up to SCAN_BATCH_SIZE photos, each with
    a 2s pacing sleep plus real download time) need a much larger budget
    — a too-short timeout here doesn't stop the worker thread's actual
    work, it just makes THIS call raise queue.Empty while the worker
    keeps running in the background, which both surfaces as a
    confusing blank-message crash (str(queue.Empty()) == "") and can
    desync caller-side bookkeeping from what the worker actually did.
    """
    _ensure_telethon_worker()
    job = {"action": action, **kwargs}
    _telethon_job_queue.put(job)
    try:
        return _telethon_result_queue.get(timeout=timeout)
    except _queue_module.Empty:
        raise TimeoutError(
            f"Telethon action '{action}' did not respond within {timeout}s "
            f"(it may still be running in the background)"
        )

def is_telethon_authorized(api_id, api_hash):
    if not (api_id and api_hash):
        return False
    if not os.path.exists(TELETHON_SESSION_PATH + ".session"):
        return False
    try:
        result = _telethon_call("check_authorized", api_id=api_id, api_hash=api_hash)
        return result.get("authorized", False)
    except Exception:
        return False

def telethon_request_code(api_id, api_hash, phone):
    return _telethon_call("request_code", api_id=api_id, api_hash=api_hash, phone=phone)

def telethon_submit_code(api_id, api_hash, phone, code, password=None):
    return _telethon_call("submit_code", api_id=api_id, api_hash=api_hash,
                           phone=phone, code=code, password=password)

def run_backlog_scan(api_id, api_hash, phone, channel_username):
    """
    Kicks off the scan job on the dedicated Telethon worker thread. This
    function itself is called from its own throwaway thread (started by
    the /api/backlog/start_scan route) so the Flask request returns
    immediately — the actual scan can take minutes for a large channel.
    """
    state = load_backlog_state()

    if not is_telethon_authorized(api_id, api_hash):
        state["status"] = "error"
        state["error"] = "Not logged in yet. Use 'Login to Telegram' in the dashboard first."
        save_backlog_state(state)
        print("❌ Backlog scan failed: Telethon session not authorized")
        return

    state["status"] = "scanning"
    state["error"] = None
    save_backlog_state(state)

    result = _telethon_call("scan", api_id=api_id, api_hash=api_hash, channel=channel_username)
    if result.get("status") != "ok":
        state = load_backlog_state()
        state["status"] = "error"
        state["error"] = result.get("message", "Unknown error")
        save_backlog_state(state)
        print(f"❌ Backlog scan failed: {result.get('message')}")

def push_untracked_covers():
    """
    Finds any covers/*.jpg on disk that git doesn't know about yet and
    pushes them. This exists because a cover can legitimately land on
    disk without being pushed — e.g. a recovery run that downloaded the
    file but crashed/timed out before its push step, or any other
    interruption between download and commit. find_codes_missing_covers()
    only checks "does the file exist", not "is it committed" — so those
    orphaned files were invisible to every future recovery run too,
    since a file just sitting on disk looks identical to a properly
    pushed one from that check's point of view. This function closes
    that gap directly: ask git itself what's untracked, and push it.
    Safe to run anytime — if nothing is untracked, it's a no-op.
    """
    os.chdir(WORK_DIR)
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "covers/"],
        capture_output=True, text=True, check=False
    )
    untracked = []
    for line in result.stdout.splitlines():
        # Porcelain format: "XY path" — untracked files are "?? path"
        if line.startswith("??"):
            path = line[3:].strip()
            if path.startswith("covers/") and path.endswith(".jpg"):
                untracked.append(path)

    if not untracked:
        print("✅ No untracked covers found — nothing to push")
        return {"status": "ok", "pushed": []}

    print(f"🔎 Found {len(untracked)} untracked cover(s) on disk, pushing...")
    cfg = load_config()
    pushed_all = []
    # Push in chunks so one huge git add/commit doesn't become an
    # all-or-nothing operation on a flaky connection.
    for i in range(0, len(untracked), SCAN_BATCH_SIZE):
        chunk = untracked[i:i + SCAN_BATCH_SIZE]
        pushed, err = git_push(cfg, "recovery",
                                f"Push {len(chunk)} previously-untracked cover(s)",
                                batch_paths=chunk)
        if pushed:
            pushed_all.extend(chunk)
            print(f"✅ Pushed {len(chunk)} untracked cover(s)")
        else:
            print(f"⚠️ Push failed for this chunk of untracked covers: {err}")

    return {"status": "ok", "pushed": pushed_all}

def find_codes_missing_covers():
    """Returns codes that have a _works/<code>.md but no local cover AND
    no cover reachable on the live site — i.e. genuinely lost, not just
    locally cleaned up (those are fine, they're still live on GitHub)."""
    site_url = "https://arccomic.github.io"
    missing = []
    for fname in os.listdir(WORKS_DIR):
        if not fname.endswith(".md"):
            continue
        code = fname[:-3]
        local_cover = os.path.join(COVERS_DIR, f"{code}.jpg")
        if os.path.exists(local_cover):
            continue  # already fine on disk
        try:
            resp = requests.head(f"{site_url}/covers/{code}.jpg", timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                continue  # already fine, still live on GitHub — don't touch
        except Exception:
            pass
        missing.append(code)
    return missing

def run_cover_recovery(api_id, api_hash, channel_username):
    """
    Finds every code whose cover is gone from BOTH disk and the live
    site, re-fetches just those cover images from the original Telegram
    posts, and pushes them back — using the same scoped git_push() so
    this recovery run itself can never accidentally delete anything
    else. Safe to run repeatedly; codes already recovered are skipped
    automatically since find_codes_missing_covers() re-checks live
    status each time.
    """
    if not is_telethon_authorized(api_id, api_hash):
        print("❌ Cover recovery failed: Telethon session not authorized")
        return {"status": "error", "message": "Not logged in to Telegram yet."}

    missing = find_codes_missing_covers()
    print(f"🔎 Cover recovery: {len(missing)} code(s) missing on disk and live")
    if not missing:
        return {"status": "ok", "recovered": [], "still_missing": []}

    all_recovered = []
    all_still_missing = []
    cfg = load_config()

    # Process in chunks so each chunk gets pushed as its own small batch
    # rather than holding hundreds of recovered files unpushed in one go.
    # Each chunk is wrapped in its own try/except: a slow/hung chunk
    # (flaky mobile network, slow git push) times out WITHOUT killing the
    # rest of the run — it's just left in all_still_missing and will be
    # picked up again automatically next time recovery is run, since
    # find_codes_missing_covers() re-checks live status fresh each call.
    for i in range(0, len(missing), SCAN_BATCH_SIZE):
        chunk = missing[i:i + SCAN_BATCH_SIZE]
        # Budget: ~2s pacing sleep per code (matches the worker's own
        # per-code sleep) plus real per-photo download time, with
        # generous headroom — a timeout here must never be tighter than
        # the work it's timing.
        chunk_timeout = max(180, len(chunk) * 8)
        try:
            result = _telethon_call("recover_covers", timeout=chunk_timeout,
                                     api_id=api_id, api_hash=api_hash,
                                     channel=channel_username, codes=chunk)
        except Exception as e:
            print(f"❌ Cover recovery chunk timed out/errored: {e or type(e).__name__}")
            all_still_missing.extend(chunk)
            continue
        if result.get("status") != "ok":
            print(f"❌ Cover recovery chunk failed: {result.get('message') or 'unknown error'}")
            all_still_missing.extend(chunk)
            continue

        recovered = result.get("recovered", [])
        all_recovered.extend(recovered)
        all_still_missing.extend(result.get("still_missing", []))

        if recovered:
            batch_paths = [os.path.join("covers", f"{code}.jpg") for code in recovered]
            pushed, err = git_push(cfg, "recovery",
                                    f"Recover {len(recovered)} missing cover(s)",
                                    batch_paths=batch_paths)
            if pushed:
                print(f"✅ Pushed {len(recovered)} recovered cover(s)")
            else:
                print(f"⚠️ Recovery push failed for this chunk: {err}")

    print(f"🏁 Cover recovery complete: {len(all_recovered)} recovered, "
          f"{len(all_still_missing)} still missing (no original post/photo found)")
    return {"status": "ok", "recovered": all_recovered, "still_missing": all_still_missing}

def process_backlog_batch():
    """
    Kicks off batch processing on the dedicated Telethon worker thread —
    scrapes each code from nhentai, downloads its cover, stages it into
    the normal posting queue (same batching/push system as live posts).
    Meant to be called repeatedly until found_codes is empty. Each call
    is self-contained and safe to interrupt — the worker removes codes
    from found_codes and appends to processed_codes as they complete, so
    a crash mid-batch only loses at most the single in-flight item.
    """
    state = load_backlog_state()
    if not state["found_codes"]:
        return 0

    cfg = load_config()
    secrets_cfg = load_secrets()
    api_id = secrets_cfg.get("telegram_api_id", "")
    api_hash = secrets_cfg.get("telegram_api_hash", "")
    channel = cfg.get("channel_username", "@ArcComic")

    if not is_telethon_authorized(api_id, api_hash):
        print("❌ Telethon session not authorized, cannot process backlog")
        return 0

    result = _telethon_call("process_batch", api_id=api_id, api_hash=api_hash, channel=channel)
    if result.get("status") != "ok":
        print(f"❌ Backlog batch failed: {result.get('message')}")
        return 0
    return result.get("processed", 0)

_autobacklog_thread = None
_autobacklog_lock = threading.Lock()
_autobacklog_stop = threading.Event()

def _autobacklog_loop():
    """
    Runs process_backlog_batch() repeatedly — SCAN_BATCH_SIZE (50) comics
    per run, back-to-back with no artificial pause between batches — and
    stops itself the moment found_codes is empty, so there's nothing to
    manually turn off.

    Staging (scraping nhentai, downloading covers, writing .md files) is
    fully offline-capable and doesn't touch GitHub at all, so there's no
    reason to slow it down batch-to-batch — the real nhentai-friendly
    pacing already happens per-comic, inside process_batch itself (a
    2-second pause between each of the 50 comics in a batch). Adding a
    second wait here on top of that would only slow down staging for no
    protective benefit.

    The GitHub push side is completely separate and already self-limits
    correctly: _flush_pending_queue_sync() only fires after
    PENDING_TIMEOUT_SECONDS (10 min) of quiet, via the existing debounce
    timer in queue_pending_flush() — same one the live posting pipeline
    already uses. This loop never touches that timer directly; it just
    keeps stage_comic() (called inside process_batch) feeding the same
    _pending_queue, so pushes stay batched and GitHub Actions never gets
    triggered more often than once every 10 minutes, no matter how fast
    staging runs.

    Safe to interrupt: same guarantee as process_backlog_batch itself —
    at most one in-flight comic lost if stopped mid-batch.
    """
    print("🚀 Auto-processing backlog started")
    total_done = 0
    while not _autobacklog_stop.is_set():
        state = load_backlog_state()
        if not state.get("found_codes"):
            break
        done = process_backlog_batch()
        total_done += done
        print(f"🚀 Auto-processing: {done} staged this batch, {total_done} total this run")
        if _autobacklog_stop.is_set():
            break
    print(f"✅ Auto-processing backlog finished — {total_done} comics staged this run, queue empty"
          if not _autobacklog_stop.is_set()
          else "🛑 Auto-processing backlog stopped manually")

def start_autobacklog():
    global _autobacklog_thread
    with _autobacklog_lock:
        if _autobacklog_thread and _autobacklog_thread.is_alive():
            return False  # already running
        _autobacklog_stop.clear()
        _autobacklog_thread = threading.Thread(target=_autobacklog_loop, daemon=True)
        _autobacklog_thread.start()
        return True

def stop_autobacklog():
    _autobacklog_stop.set()

def is_autobacklog_running():
    return bool(_autobacklog_thread and _autobacklog_thread.is_alive())


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = update.channel_post
    if not msg:
        return

    raw_text = msg.caption or msg.text or ""

    # Temp                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       