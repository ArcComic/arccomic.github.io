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

# Ensure dirs exist
os.makedirs(COVERS_DIR, exist_ok=True)
os.makedirs(WORKS_DIR, exist_ok=True)

# Config template
DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "github_token": "",
    "channel_username": "@ArcComic",
    "site_domain": "https://example.com",
    "posts_per_page": 15,
    "github_repo": "ArcComic/arccomic.github.io",
    "last_message_id": 0,
    "google_verification_tag": "",
    "auto_ping_google": True,
    "use_indexnow": True,
    "indexnow_key": ""
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

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

# ============== SITE SCRAPER (PLACEHOLDER) ==============
def scrape_site(code, domain):
    try:
        url = f"{domain}/g/{code}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36"
        }
        r = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')

        title_elem = soup.select_one("h1.title .pretty") or soup.select_one("h1")
        title = title_elem.text.strip() if title_elem else f"Work {code}"

        tag_elems = soup.select("section#tags a.tagchip .name") or soup.select("a[href^='/tag/']")
        tags = [t.text.strip() for t in tag_elems if t.text.strip()]

        return title, tags
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
    except Exception as e:
        print(f"❌ Git push error: {e}")
    return False

# ============== TELEGRAM BOT HANDLER ==============
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    msg = update.channel_post
    if not msg:
        return

    text = msg.caption or msg.text or ""

    code_match = re.search(r"🌟Code:\s*(\d+)", text)
    if not code_match:
        print("⚠️ No code found in post")
        return

    code = code_match.group(1)

    author = re.search(r"✨Author:\s*(.+)", text)
    author = author.group(1).strip() if author else "Unknown"

    categories = re.search(r"⚡Categories:\s*(.+)", text)
    categories = categories.group(1).strip() if categories else "manga"

    full_color = re.search(r"💫Full color:\s*(.+)", text)
    full_color = full_color.group(1).strip() if full_color else "no"

    cheating = re.search(r"🌙Cheating:\s*(.+)", text)
    cheating = cheating.group(1).strip() if cheating else "no"

    language = re.search(r"⚡Language:\s*(.+)", text)
    language = language.group(1).strip() if language else "english"

    rating = re.search(r"⭐Rating:\s*\(([\d.]+)\)", text)
    rating = rating.group(1) if rating else "0.0"

    date_str = msg.date.strftime("%Y-%m-%d")
    telegram_url = f"https://t.me/{cfg['channel_username'].replace('@','')}/{msg.message_id}"
    site_url = f"https://arccomic.github.io"
    post_url = f"{site_url}/works/{code}/"

    print(f"📥 Processing code {code}...")

    domain = cfg.get("site_domain", "https://example.com")
    title, tags = scrape_site(code, domain)

    # Download cover from Telegram (raw download, no Pillow)
    cover_path = os.path.join(COVERS_DIR, f"{code}.jpg")
    if msg.photo:
        photo = msg.photo[-1]
        photo_file = await photo.get_file()
        await photo_file.download_to_drive(cover_path)
        print(f"📸 Cover saved: {cover_path}")

    md_content = generate_md(code, title, author, categories, full_color,
                            cheating, language, rating, tags, cover_path, 
                            telegram_url, date_str)

    md_path = os.path.join(WORKS_DIR, f"{code}.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md_content)

    if git_push(cfg, code, title):
        if cfg.get("auto_ping_google", True):
            ping_google_sitemap(site_url)
        if cfg.get("use_indexnow", True):
            api_key = cfg.get("indexnow_key", "")
            if api_key:
                submit_indexnow(post_url, site_url, api_key)

    cfg["last_message_id"] = msg.message_id
    save_config(cfg)
    print(f"✅ Done: {title} (Code: {code})")

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
                Last Message ID: <strong style="color:#f59e0b;">{{ last_message_id }}</strong><br>
                Config File: <code>/storage/emulated/0/arccomic.github.io/config.json</code><br>
                Works Dir: <code>/storage/emulated/0/arccomic.github.io/_works/</code><br>
                IndexNow Key: <code>{{ indexnow_key[:16] + '...' if indexnow_key else 'Not set' }}</code>
            </p>
        </div>

        <div class="footer">
            Arc Comic Bot v2.0 • Running on Termux
        </div>
    </div>

    <script>
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
    return render_template_string(DASHBOARD_HTML, **cfg)

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

    save_config(cfg)

    # Inject the verification tag into index.html and push immediately,
    # so Google can verify without waiting for the next comic post.
    if update_verification_tag(cfg.get("google_verification_tag", "")):
        git_push(cfg, "seo", "Update Google verification tag")

    return jsonify({"status": "ok", "indexnow_key": cfg.get("indexnow_key", "")})

# ============== MAIN ==============
def run_bot():
    cfg = load_config()
    token = cfg.get("telegram_bot_token", "")
    if not token:
        print("⚠️ No bot token configured. Open http://localhost:6767 to set up.")
        return
    print("🤖 Starting Telegram bot...")
    app_tg = Application.builder().token(token).build()
    app_tg.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_channel_post))
    # stop_signals=None: signal handlers can only be installed on the main
    # thread. Since the bot runs in a background thread (dashboard owns the
    # main thread), we must skip signal handler registration entirely.
    app_tg.run_polling(stop_signals=None)

def run_dashboard():
    print("🌐 Dashboard running at http://localhost:6767")
    app.run(host="0.0.0.0", port=6767, debug=False)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bot-only":
        run_bot()
    else:
        threading.Thread(target=run_bot, daemon=True).start()
        run_dashboard()
