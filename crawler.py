import os
import sys
import json
import time
import re
from collections import deque
import requests
from playwright.sync_api import sync_playwright

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

class SessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("🌐 Launching Chromium to capture session credentials...")
        captured = {"headers": {}}

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720}
            )
            page = context.new_page()

            def intercept_request(request):
                if "contents/" in request.url:
                    captured["headers"] = dict(request.headers)

            page.on("request", intercept_request)
            try:
                page.goto(self.root_url, wait_until="networkidle", timeout=45000)
                time.sleep(2)
            except Exception as e:
                print(f"Browser notice: {e}")
            finally:
                browser.close()

        if not captured["headers"]:
            print("❌ Failed to intercept headers from browser session.")
            sys.exit(1)

        self.session.headers.clear()
        self.session.headers.update(captured["headers"])
        self.last_auth_time = time.time()
        print("✅ Intercepted session headers.")

    def ensure_fresh(self):
        if time.time() - self.last_auth_time > 900:
            self.refresh_credentials()

def extract_edition(raw_name):
    name_lower = raw_name.lower()
    editions = []
    if "open matte" in name_lower or "open.matte" in name_lower:
        editions.append("Open Matte")
    if "imax" in name_lower:
        editions.append("IMAX")
    if "extended" in name_lower:
        editions.append("Extended")
    if "director" in name_lower and "cut" in name_lower:
        editions.append("Director's Cut")
    if "remux" in name_lower:
        editions.append("REMUX")
    if "unrated" in name_lower:
        editions.append("Unrated")
    return " / ".join(editions) if editions else ""

def extract_quality(raw_name):
    match = re.search(r"\b(2160p|4k|1080p|720p|480p)\b", raw_name, re.I)
    return match.group(1).upper() if match else "1080P"

def clean_title(raw_name):
    name = re.sub(r"\.(mkv|mp4|avi|mov)$", "", raw_name, flags=re.I)
    name = name.replace(".", " ").replace("_", " ").replace("-", " ")
    name = re.sub(r"\b(open matte|imax|extended|unrated|director\'?s cut|remux)\b", "", name, flags=re.I)
    name = re.sub(r"\b(2160p|4k|1080p|720p|480p|uhd|webrip|web-dl|bluray|x264|x265|hevc|aac|dts)\b.*", "", name, flags=re.I)
    return name.strip()

def resolve_imdb(query, is_series=False):
    media_type = "series" if is_series else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{media_type}/top/search={requests.utils.quote(query)}.json"
    try:
        res = requests.get(url, timeout=6).json()
        metas = res.get("metas", [])
        if metas:
            return metas[0].get("id"), metas[0].get("name"), metas[0].get("poster")
    except Exception:
        pass
    return None, None, None

def fetch_folder_page(session_mgr, folder_id, page_num=1, max_retries=4):
    api_url = f"https://api.gofile.io/contents/{folder_id}?page={page_num}&pageSize=100&sortField=createTime&sortDirection=-1"
    for attempt in range(max_retries):
        session_mgr.ensure_fresh()
        try:
            res = session_mgr.session.get(api_url, timeout=25).json()
            status = res.get("status")
            if status == "ok":
                return res
            elif status in ["error-rateLimit", "error-auth", "error-token"]:
                time.sleep((attempt + 1) * 6)
                if status in ["error-auth", "error-token"]:
                    session_mgr.refresh_credentials()
            else:
                return None
        except Exception:
            time.sleep(3)
    return None

def main():
    # 1. Load active data.json
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                for item in data:
                    fid = item.get("file_id")
                    if fid:
                        existing_catalog[fid] = item
            print(f"📦 Loaded {len(existing_catalog)} existing records from data.json")
        except Exception as e:
            print(f"⚠️ Could not read data.json: {e}")

    session_mgr = SessionManager(ROOT_URL)
    folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
    visited_folders = set()
    all_discovered_files = {}

    print("🚀 Crawling Gofile directory tree (every page and subfolder)...")

    # 2. Complete crawl of every folder and page
    while folders_queue:
        current_folder_id, current_folder_name = folders_queue.popleft()
        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        page_num = 1
        folder_files = 0

        while True:
            res = fetch_folder_page(session_mgr, current_folder_id, page_num)
            if not res or res.get("status") != "ok":
                break

            data = res.get("data", {})
            children = data.get("children", {})
            if not children:
                break

            for item_id, item in children.items():
                item_type = item.get("type", "")

                if item_type == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, item.get("name", sub_code)))
                else:
                    link = item.get("link") or item.get("directDownload") or item.get("downloadPage")
                    if link and item_id not in all_discovered_files:
                        all_discovered_files[item_id] = item
                        folder_files += 1

            # Stop when fewer items than pageSize are returned (reached last page)
            if len(children) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        print(f"📂 Scanned [{current_folder_name}]: {folder_files} files found across {page_num} page(s)")

    print(f"\n🔎 Total files currently live on Gofile: {len(all_discovered_files)}")

    # 3. Match against data.json: process only missing files
    missing_ids = [fid for fid in all_discovered_files if fid not in existing_catalog]
    print(f"⚡ Files already in data.json: {len(all_discovered_files) - len(missing_ids)}")
    print(f"🆕 New files to add: {len(missing_ids)}\n")

    series_regex = re.compile(r"[sS](\d{1,2})[eE](\d{1,2})|(\d{1,2})x(\d{1,2})", re.I)
    imdb_search_cache = {}
    added_count = 0

    for fid in missing_ids:
        item = all_discovered_files[fid]
        fname = item.get("name", fid)
        link = item.get("link") or item.get("directDownload") or item.get("downloadPage")
        size = item.get("size", 0)
        size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"
        edition = extract_edition(fname)
        quality = extract_quality(fname)

        match = series_regex.search(fname)
        if match:
            s = int(match.group(1) or match.group(3))
            e = int(match.group(2) or match.group(4))
            series_raw = fname[:match.start()]
            title_query = clean_title(series_raw)

            if title_query not in imdb_search_cache:
                imdb_search_cache[title_query] = resolve_imdb(title_query, is_series=True)

            imdb_id, show_name, poster = imdb_search_cache[title_query]
            existing_catalog[fid] = {
                "file_id": fid,
                "type": "series",
                "imdb_id": imdb_id or f"gf:{fid}",
                "title": show_name or title_query,
                "name": fname,
                "season": s,
                "episode": e,
                "stream_id": f"{imdb_id}:{s}:{e}" if imdb_id else f"gf:{fid}:{s}:{e}",
                "poster": poster or "https://gofile.io/dist/img/logo-small.png",
                "edition": edition,
                "quality": quality,
                "size": size_mb,
                "link": link
            }
        else:
            title_query = clean_title(fname)
            if title_query not in imdb_search_cache:
                imdb_search_cache[title_query] = resolve_imdb(title_query, is_series=False)

            imdb_id, movie_name, poster = imdb_search_cache[title_query]
            existing_catalog[fid] = {
                "file_id": fid,
                "type": "movie",
                "imdb_id": imdb_id or f"gf:{fid}",
                "title": movie_name or title_query,
                "name": fname,
                "stream_id": imdb_id or f"gf:{fid}",
                "poster": poster or "https://gofile.io/dist/img/logo-small.png",
                "edition": edition,
                "quality": quality,
                "size": size_mb,
                "link": link
            }

        added_count += 1
        print(f"➕ Added: {fname}")

    # 4. Save the combined list
    final_list = list(existing_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=2)

    print(f"\n🎉 Done. Added {added_count} new file(s). Total entries in data.json: {len(final_list)}")

if __name__ == "__main__":
    main()

