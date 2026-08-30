#!/data/data/com.termux/files/usr/bin/env python3
"""
Arc Comic Bot - Auto-poster for GitHub Pages
Minimal dependencies: no lxml, no Pillow
"""

import os
import sys
import json
import re
import time
import subprocess
import threading
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from flask import Flask, render_template_string, request, jsonify

# Paths
WORK_DIR = "/storage/emulated/0/arccomic.github.io"
CONFIG_FILE = os.path.join(WORK_DIR, "config.json")
COVERS_DIR = os.path.join(WORK_DIR, "covers")
WORKS_DIR = os.path.join(WORK_DIR, "_works")
STATS_FILE = os.path.join(WORK_DIR, "stats.json")

# SECURITY: tokens live OUTSIDE the git repo folder entirely, in a sibling
# directory. This makes it structurally impossible for `git add .` inside
# WORK_DIR to ever pick them up, even if .gitignore rules are ever missing,
# forgotten, or edited by mistake. This is the root cause fix for the
# earlier incident where a GitHub token was committed and had to be purged
# from history.
SECRETS_DIR = "/storage/emulated/0/ArcComicSecrets"
SECRETS_FILE = os.path.join(SECRETS_DIR, "secrets.json")
os.makedirs(SECRETS_DIR, exist_ok=True)

DEFAULT_SECRETS = {
    "telegram_bot_token": "",
    "github_token": "",
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
    }
    save_secrets(secrets)

    settings = {k: v for k, v in cfg.items() if k not in secrets}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

# ============== GOOGLE SEO FUNCTIONS ==============
def ping_google_sitemap(site_url):
    try:
        sitemap_url = f"{site_url}/sitemap.xml"
        ping_url = f"https://www.google.com/ping?sitemap={sitemap_url}"
        r = requests.get(ping_url, timeout=15)
        if r.status_code == 200:
            print(f"📡 Pinged Google: {sitemap_url}")
            return True
    except Exception as e:
        print(f"⚠️ Google ping failed: {e}")
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
    """Insert or replace the Google verification meta tag inside index.html's <head>."""
    index_path = os.path.join(WORK_DIR, "index.html")
    if not os.path.exists(index_path):
        print("⚠️ index.html not found, cannot inject verification tag")
        return False

    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()

    tag_html = tag_html.strip()

    # Remove any existing google-site-verification meta tag first
    html = re.sub(
        r'\s*<meta[^>]+name=["\']google-site-verification["\'][^>]*>\s*',
        '\n', html, flags=re.IGNORECASE
    )

    if tag_html:
        if "<head>" in html:
            html = html.replace("<head>", f"<head>\n    {tag_html}", 1)
        else:
            print("⚠️ No <head> tag found in index.html")
            return False

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
        soup = BeautifulSoup(r.text, 'html.parser')

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
    ch_bool = "true" if cheating.lower() == "yes" else "false"

    md_lines = [
        "---",
        "layout: post",
        f"code: {code}",
        f'title: "{title.replace(chr(34), chr(92)+chr(34))}"',
        f'author: "{author}"',
        f'categories: ["{categories}"]',
        f"tags: {tags_yaml}",
        f"full_color: {fc_bool}",
        f"cheating: {ch_bool}",
        f'language: "{language}"',
        f"rating: {rating}",
        f'cover: "/covers/{code}.jpg"',
        f'telegram_post: "{telegram_post_url}"',
        f"date: {date_str}",
        "views: 0",
        "---",
        "",
        f"# {title}",
        "",
        f"![Cover](/covers/{code}.jpg)",
        "",
        f"**Author:** {author} | **Code:** {code} | **Rating:** ⭐ {rating}  ",
        f"**Tags:** {', '.join(tags) if tags else 'N/A'} | **Language:** {language.title()}",
        "",
        "---",
        "",
        f"[📖 Read on Telegram]({telegram_post_url})"
    ]
    return "\n".join(md_lines)

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

def record_post(code, title, success, error=None):
    stats = load_stats()
    entry = {
        "code": code,
        "title": title,
        "success": success,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error
    }
    stats["posts"].append(entry)
    if success:
        stats["total_posts"] = stats.get("total_posts", 0) + 1
    else:
        stats["last_error"] = error
        stats["last_error_time"] = entry["time"]
    save_stats(stats)

# ============== GIT OPERATIONS ==============
def git_push(cfg, code, title):
    try:
        os.chdir(WORK_DIR)
        subprocess.run(["git", "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"Add work #{code}: {title}"], 
                      check=False, capture_output=True)
        repo = cfg.get("github_repo", "ArcComic/arccomic.github.io")
        token = cfg.get("github_token", "")
        if token:
            remote_url = f"https://{token}@github.com/{repo}.git"
            # Pull first so a diverged remote (e.g. edits made on github.com)
            # doesn't cause the push to be rejected as non-fast-forward.
            pull_result = subprocess.run(
                ["git", "pull", remote_url, "main", "--no-edit"],
                check=False, capture_output=True, text=True
            )
            if pull_result.returncode != 0:
                print(f"⚠️ Git pull warning: {pull_result.stderr.strip()}")
            push_result = subprocess.run(
                ["git", "push", remote_url, "main"],
                check=False, capture_output=True, text=True
            )
            if push_result.returncode == 0:
                print(f"✅ Pushed work #{code}")
                return True
            else:
                print(f"❌ Git push failed: {push_result.stderr.strip()}")
        else:
            print("⚠️ No GitHub token configured, skipping push")
    except Exception as e:
        print(f"❌ Git push error: {e}")
    return False

# ============== TELEGRAM MESSAGE PARSING ==============
def normalize_text(text):
    """Collapse Telegram's non-breaking spaces and other invisible
    formatting-boundary characters (which appear when mixing bold and
    monospace styles) down to plain ASCII spaces."""
    replacements = {
        "\xa0": " ",   # non-breaking space
        "\u200b": "",  # zero-width space
        "\u200c": "",  # zero-width non-joiner
        "\u200d": "",  # zero-width joiner
        "\ufeff": "",  # BOM / zero-width no-break space
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
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
    None (with a debug print) if no code could be found at all."""
    text = normalize_text(raw_text)

    code = extract_field(text, "🌟Code", "Code", pattern=r"(\d+)")
    if not code:
        print(f"⚠️ No code found in post. Raw text was: {repr(raw_text)}")
        return None

    fields = {
        "code": code,
        "author": extract_field(text, "✨Author", "Author") or "Unknown",
        "categories": extract_field(text, "⚡Categories", "Categories") or "manga",
        "full_color": extract_field(text, "💫Full color", "Full color") or "no",
        "cheating": extract_field(text, "🌙Cheating", "Cheating") or "no",
        "language": extract_field(text, "⚡Language", "Language") or "english",
        "rating": extract_field(text, "⭐Rating", "Rating", pattern=r"\(?([\d.]+)\)?") or "0.0",
    }
    return fields

# ============== TELEGRAM BOT HANDLER ==============
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = update.channel_post
    if not msg:
        return

    raw_text = msg.caption or msg.text or ""
    fields = parse_post_fields(raw_text)
    if not fields:
        record_post(code="?", title="(unparsed post)", success=False,
                     error="Could not find a Code field in the post")
        return

    code = fields["code"]
    title = f"Work {code}"  # placeholder until scrape completes

    try:
        date_str = msg.date.strftime("%Y-%m-%d")
        channel = cfg.get('channel_username', '@ArcComic').replace('@', '')
        telegram_url = f"https://t.me/{channel}/{msg.message_id}"
        site_url = "https://arccomic.github.io"
        post_url = f"{site_url}/works/{code}/"

        print(f"📥 Processing code {code}...")

        domain = cfg.get("site_domain", "https://nhentai.net")
        title, tags = scrape_site(code, domain)

        # Download cover from Telegram (raw download, no Pillow)
        cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
        if msg.photo:
            photo = msg.photo[-1]
            photo_file = await photo.get_file()
            await photo_file.download_to_drive(cover_path)
            print(f"📸 Cover saved: {cover_path}")
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

        pushed = git_push(cfg, code, title)
        if pushed:
            if cfg.get("auto_ping_google", True):
                ping_google_sitemap(site_url)
            if cfg.get("use_indexnow", True):
                api_key = cfg.get("indexnow_key", "")
                if api_key:
                    submit_indexnow(post_url, site_url, api_key)

        cfg["last_message_id"] = msg.message_id
        save_config(cfg)

        record_post(code, title, success=pushed,
                     error=None if pushed else "Git push failed")
        print(f"✅ Done: {title} (Code: {code})")

    except Exception as e:
        print(f"❌ Error processing code {code}: {e}")
        record_post(code, title, success=False, error=str(e))

# ============== FLASK DASHBOARD ==============
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>✨ Arc Comic Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: #0f0f13;
            color: #e2e2e8;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            width: 100%;
            max-width: 480px;
        }
        .logo {
            text-align: center;
            margin-bottom: 30px;
        }
        .logo h1 {
            font-size: 28px;
            background: linear-gradient(135deg, #f59e0b, #f97316);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .logo p { color: #8888a0; font-size: 14px; margin-top: 5px; }
        .card {
            background: #1a1a24;
            border: 1px solid #2a2a3a;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 16px;
        }
        .card h2 {
            font-size: 16px;
            color: #f59e0b;
            margin-bottom: 16px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .form-group { margin-bottom: 16px; }
        .form-group label {
            display: block;
            font-size: 13px;
            color: #8888a0;
            margin-bottom: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .form-group input, .form-group textarea {
            width: 100%;
            padding: 12px 14px;
            background: #0f0f13;
            border: 1px solid #2a2a3a;
            border-radius: 10px;
            color: #e2e2e8;
            font-size: 14px;
            outline: none;
            transition: border-color 0.2s;
            font-family: inherit;
        }
        .form-group input:focus, .form-group textarea:focus { border-color: #f59e0b; }
        .form-group input::placeholder, .form-group textarea::placeholder { color: #555; }
        .form-group textarea {
            min-height: 80px;
            resize: vertical;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 12px;
        }
        .checkbox-group input[type="checkbox"] {
            width: 20px; height: 20px;
            accent-color: #f59e0b;
        }
        .checkbox-group label {
            color: #b8b8d0;
            font-size: 14px;
        }
        .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #f59e0b, #f97316);
            color: #000;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 24px rgba(245,158,11,0.3);
        }
        .status {
            text-align: center;
            padding: 12px;
            border-radius: 10px;
            margin-top: 12px;
            font-size: 14px;
            display: none;
        }
        .status.success { background: rgba(34,197,94,0.15); color: #22c55e; display: block; }
        .status.error { background: rgba(239,68,68,0.15); color: #ef4444; display: block; }
        .info-box {
            background: rgba(245,158,11,0.08);
            border-left: 3px solid #f59e0b;
            padding: 12px 16px;
            border-radius: 0 10px 10px 0;
            margin-bottom: 20px;
            font-size: 13px;
            color: #b8b8d0;
            line-height: 1.6;
        }
        .info-box code {
            background: #2a2a3a;
            padding: 2px 6px;
            border-radius: 4px;
            color: #f59e0b;
            font-size: 12px;
        }
        .footer {
            text-align: center;
            color: #555;
            font-size: 12px;
            margin-top: 20px;
        }
        .section-divider {
            border-top: 1px solid #2a2a3a;
            margin: 20px 0;
            padding-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="logo">
            <h1>✨ Arc Comic</h1>
            <p>Bot Dashboard & Configuration</p>
        </div>

        <div class="info-box">
            <strong>First time setup?</strong><br>
            1. Get Bot Token from <code>@BotFather</code><br>
            2. Get GitHub Token from Settings → Developer settings<br>
            3. Fill below and click Save
        </div>

        <div class="card">
            <h2>🔑 API Configuration</h2>
            <form id="configForm">
                <div class="form-group">
                    <label>Telegram Bot Token</label>
                    <input type="password" name="telegram_bot_token" 
                           placeholder="123456:ABC..." 
                           value="{{ telegram_bot_token }}" required>
                </div>
                <div class="form-group">
                    <label>GitHub Personal Access Token</label>
                    <input type="password" name="github_token" 
                           placeholder="ghp_..." 
                           value="{{ github_token }}" required>
                </div>
                <div class="form-group">
                    <label>Telegram Channel</label>
                    <input type="text" name="channel_username" 
                           placeholder="@ArcComic" 
                           value="{{ channel_username }}">
                </div>
                <div class="form-group">
                    <label>Site Domain (for scraper)</label>
                    <input type="text" name="site_domain" 
                           placeholder="https://example.com" 
                           value="{{ site_domain }}">
                </div>
                <div class="form-group">
                    <label>GitHub Repo</label>
                    <input type="text" name="github_repo" 
                           placeholder="ArcComic/arccomic.github.io" 
                           value="{{ github_repo }}">
                </div>
                <div class="form-group">
                    <label>Posts Per Page</label>
                    <input type="number" name="posts_per_page" 
                           value="{{ posts_per_page }}" min="5" max="50">
                </div>

                <div class="section-divider"></div>

                <h2 style="font-size:16px;color:#f59e0b;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px;">
                    🔍 Google SEO Setup
                </h2>

                <div class="form-group">
                    <label>Google Verification Meta Tag</label>
                    <textarea name="google_verification_tag" 
                              placeholder='<meta name="google-site-verification" content="YOUR_CODE" />'
                              style="min-height:60px;font-size:12px;">{{ google_verification_tag }}</textarea>
                    <p style="color:#666;font-size:11px;margin-top:6px;">
                        Paste the full meta tag from Google Search Console
                    </p>
                </div>

                <div class="checkbox-group">
                    <input type="checkbox" name="auto_ping_google" id="auto_ping" 
                           {{ 'checked' if auto_ping_google else '' }}>
                    <label for="auto_ping">Auto-ping Google on new post</label>
                </div>

                <div class="checkbox-group">
                    <input type="checkbox" name="use_indexnow" id="use_indexnow" 
                           {{ 'checked' if use_indexnow else '' }}>
                    <label for="use_indexnow">Use IndexNow for instant indexing</label>
                </div>

                <div class="form-group">
                    <label>IndexNow API Key (auto-generated if empty)</label>
                    <input type="text" name="indexnow_key" 
                           placeholder="Auto-generated" 
                           value="{{ indexnow_key }}" readonly>
                </div>

                <button type="submit" class="btn">💾 Save & Start Bot</button>
                <div class="status" id="status"></div>
            </form>
        </div>

        <div class="card">
            <h2>📊 Bot Status</h2>
            <p style="color:#8888a0;font-size:14px;">
                Status: <strong id="botStatus" style="color:#f59e0b;">{{ bot_status }}</strong><br>
                Last heartbeat: <strong>{{ bot_last_update or 'N/A' }}</strong><br>
                Restarts since launch: <strong>{{ bot_restart_count }}</strong><br>
                Last Message ID: <strong style="color:#f59e0b;">{{ last_message_id }}</strong><br>
                Works Dir: <code>/storage/emulated/0/arccomic.github.io/_works/</code><br>
                IndexNow Key: <code>{{ indexnow_key[:16] + '...' if indexnow_key else 'Not set' }}</code>
            </p>
        </div>

        <div class="card">
            <h2>📈 Posting Statistics</h2>
            <p style="color:#8888a0;font-size:14px;margin-bottom:12px;">
                Total posts published: <strong style="color:#22c55e;font-size:20px;">{{ total_posts }}</strong>
            </p>
            {% if last_error %}
            <p style="color:#ef4444;font-size:13px;margin-bottom:12px;">
                ⚠️ Last error ({{ last_error_time }}): {{ last_error }}
            </p>
            {% endif %}
            <div style="max-height:240px;overflow-y:auto;">
                {% if recent_posts %}
                {% for post in recent_posts %}
                <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #2a2a3a;font-size:13px;">
                    <span>{{ '✅' if post.success else '❌' }} {{ post.title }} <code style="font-size:11px;">#{{ post.code }}</code></span>
                    <span style="color:#666;">{{ post.time }}</span>
                </div>
                {% endfor %}
                {% else %}
                <p style="color:#666;font-size:13px;">No posts yet. Post something in your Telegram channel to get started.</p>
                {% endif %}
            </div>
        </div>

        <div class="footer">
            Arc Comic Bot v3.0 • Running on Termux
        </div>
    </div>

    <script>
        // Live status refresh every 10s so the dashboard reflects bot health
        // without needing a manual page reload.
        async function refreshStatus() {
            try {
                const res = await fetch('/api/health');
                const health = await res.json();
                document.getElementById('botStatus').textContent = health.status;
            } catch (e) { /* dashboard server itself would have to be down */ }
        }
        setInterval(refreshStatus, 10000);

        document.getElementById('configForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = Object.fromEntries(formData);

            data.auto_ping_google = document.getElementById('auto_ping').checked;
            data.use_indexnow = document.getElementById('use_indexnow').checked;

            const res = await fetch('/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const status = document.getElementById('status');
            if (res.ok) {
                status.className = 'status success';
                status.textContent = '✅ Config saved! Bot is starting...';
            } else {
                status.className = 'status error';
                status.textContent = '❌ Error saving config';
            }
        });
    </script>
</body>
</html>
"""

@app.route("/")
def dashboard():
    cfg = load_config()
    stats = load_stats()
    return render_template_string(
        DASHBOARD_HTML, **cfg,
        total_posts=stats.get("total_posts", 0),
        recent_posts=list(reversed(stats.get("posts", [])))[:10],
        last_error=stats.get("last_error"),
        last_error_time=stats.get("last_error_time"),
        bot_status=BOT_HEALTH.get("status"),
        bot_last_update=BOT_HEALTH.get("last_update"),
        bot_restart_count=BOT_HEALTH.get("restart_count", 0),
    )

@app.route("/save", methods=["POST"])
def save():
    data = request.get_json()
    cfg = load_config()
    cfg.update(data)
    cfg["posts_per_page"] = int(data.get("posts_per_page", 15))
    cfg["auto_ping_google"] = data.get("auto_ping_google", True)
    cfg["use_indexnow"] = data.get("use_indexnow", True)

    if cfg.get("use_indexnow") and not cfg.get("indexnow_key"):
        cfg["indexnow_key"] = generate_indexnow_key()
        key_file = os.path.join(WORK_DIR, f"{cfg['indexnow_key']}.txt")
        with open(key_file, 'w') as f:
            f.write(cfg["indexnow_key"])

    save_config(cfg)  # automatically splits tokens into SECRETS_FILE

    # Inject the verification tag into index.html and push immediately,
    # so Google can verify without waiting for the next comic post.
    if update_verification_tag(cfg.get("google_verification_tag", "")):
        git_push(cfg, "seo", "Update Google verification tag")

    return jsonify({"status": "ok", "indexnow_key": cfg.get("indexnow_key", "")})

@app.route("/api/stats")
def api_stats():
    stats = load_stats()
    return jsonify({
        "total_posts": stats.get("total_posts", 0),
        "recent_posts": list(reversed(stats.get("posts", [])))[:10],
        "last_error": stats.get("last_error"),
        "last_error_time": stats.get("last_error_time"),
    })

@app.route("/api/health")
def api_health():
    return jsonify(BOT_HEALTH)

# ============== MAIN ==============
BOT_HEALTH = {"status": "starting", "last_update": None, "restart_count": 0}

def run_bot():
    """Runs the Telegram bot with automatic restart on crash. A crash
    (network blip, Telegram API hiccup, etc.) no longer silently kills
    posting forever — it retries with a short backoff instead."""
    backoff = 5
    while True:
        cfg = load_config()
        token = cfg.get("telegram_bot_token", "")
        if not token:
            print("⚠️ No bot token configured. Open http://localhost:6767 to set up.")
            BOT_HEALTH["status"] = "no_token"
            time.sleep(10)
            continue

        try:
            print("🤖 Starting Telegram bot...")
            BOT_HEALTH["status"] = "running"
            BOT_HEALTH["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            app_tg = Application.builder().token(token).build()
            app_tg.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
            # stop_signals=None: signal handlers can only be installed on the
            # main thread. Since the bot runs in a background thread (the
            # dashboard owns the main thread), we must skip signal handler
            # registration entirely, or Telegram bot startup crashes with
            # "set_wakeup_fd only works in main thread of the main interpreter".
            app_tg.run_polling(stop_signals=None)
        except Exception as e:
            BOT_HEALTH["status"] = "crashed"
            BOT_HEALTH["restart_count"] += 1
            print(f"❌ Bot crashed: {e}")
            print(f"🔄 Restarting in {backoff}s (restart #{BOT_HEALTH['restart_count']})...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)  # exponential backoff, capped at 5 min
            continue
        # run_polling returned normally (shouldn't usually happen) — restart anyway
        backoff = 5

def run_dashboard():
    print("🌐 Dashboard running at http://localhost:6767")
    app.run(host="0.0.0.0", port=6767, debug=False)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bot-only":
        run_bot()
    else:
        threading.Thread(target=run_bot, daemon=True).start()
        run_dashboard()
