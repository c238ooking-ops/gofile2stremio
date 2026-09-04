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

SEQUEL_TAGS = {"2", "3", "4", "5", "6", "ii", "iii", "iv", "v", "part", "chapter", "returns", "reloaded"}

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
                print(f"Browser navigation notice: {e}")
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

# ================= STRING CLEANING & EDITION PARSING =================

def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def extract_edition(raw_name):
    lower = raw_name.lower()
    tags = []
    if "open matte" in lower or "open.matte" in lower: tags.append("Open Matte")
    if "imax" in lower: tags.append("IMAX")
    if "extended" in lower: tags.append("Extended Cut")
    if "director" in lower and "cut" in lower: tags.append("Director's Cut")
    if "theatrical" in lower: tags.append("Theatrical")
    if "workprint" in lower: tags.append("Workprint")
    if "35mm" in lower: tags.append("35mm Scan")
    if "remux" in lower: tags.append("REMUX")
    if "unrated" in lower: tags.append("Unrated")
    return " • ".join(tags) if tags else ""

def extract_quality(raw_name):
    m = re.search(r"\b(2160p|4k|1440p|1080p|720p|480p)\b", raw_name, re.I)
    return m.group(1).upper() if m else "1080P"

def clean_title_string(s):
    s = re.sub(r"^(\d{1,3}[\.\-\s_]+|\[\d{1,3}\][\.\-\s_]*)", "", s)
    s = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", s)
    s = re.sub(r"[\~|\-]\s*[\w\.\-]+$", "", s)
    s = re.sub(r"\b(msubs|subs|esub|dual audio|hindi|english|atmos|ddp5\.1|dd5\.1|5\.1|7\.1|truehd|dts\-hd|dts|aac|ac3)\b", "", s, flags=re.I)
    s = re.sub(r"\b(10bit|8bit|bluray|bdrip|brrip|webrip|web\-dl|hdrip|dvdrip|remux|x265|x264|hevc|h264|h265|avc)\b", "", s, flags=re.I)
    s = re.sub(r"\b(2160p|4k|1440p|1080p|720p|480p|uhd)\b", "", s, flags=re.I)
    s = re.sub(r"\b(open matte|imax|extended|director\'?s cut|unrated|theatrical)\b", "", s, flags=re.I)
    s = re.sub(r"[\._\-~+:]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def parse_filename(filename):
    clean = re.sub(r"\.[^/.]+$", "", filename)

    series_match = (
        re.search(r"(.*?)\s*[sS](\d{1,2})[eE](\d{1,2})", clean, re.I) or
        re.search(r"(.*?)\s*(\d{1,2})x(\d{1,2})", clean, re.I) or
        re.search(r"(.*?)\s*Season\s*(\d{1,2})\s*Episode\s*(\d{1,2})", clean, re.I)
    )
    if series_match:
        return {
            "type": "series",
            "title": clean_title_string(series_match.group(1)),
            "year": None,
            "season": int(series_match.group(2)),
            "episode": int(series_match.group(3))
        }

    year = None
    year_match = re.search(r"\b(19\d\d|20\d\d)\b", clean)
    if year_match:
        year = int(year_match.group(1))
        title_raw = clean[:year_match.start()].strip()
    else:
        title_raw = clean

    return {
        "type": "movie",
        "title": clean_title_string(title_raw),
        "year": year
    }

# ================= SCORING & RESOLVER =================

def score_candidate(cand_title, cand_year, target_title, target_year):
    try:
        c_year = int(cand_year) if cand_year else None
    except:
        c_year = None
    t_year = int(target_year) if target_year else None

    if t_year and c_year:
        if abs(c_year - t_year) > 1:
            return -1

    norm_cand = normalize(cand_title)
    norm_target = normalize(target_title)

    cand_words = set(re.findall(r"\w+", cand_title.lower()))
    target_words = set(re.findall(r"\w+", target_title.lower()))
    for tag in SEQUEL_TAGS:
        if tag in cand_words and tag not in target_words:
            return -1

    score = 0
    if norm_cand == norm_target:
        score += 100
    elif norm_cand.startswith(norm_target):
        score += 50
    elif norm_target in norm_cand:
        score += 20
    else:
        return -1

    if t_year and c_year:
        if c_year == t_year:
            score += 60
        elif abs(c_year - t_year) == 1:
            score += 30

    return score

def search_cinemeta(title, year, m_type):
    catalog_type = "series" if m_type == "series" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(title)}.json"
    try:
        res = requests.get(url, timeout=7).json()
        metas = res.get("metas", [])
        best_item, highest_score = None, 0
        for m in metas:
            cand_year = m.get("year") or m.get("releaseInfo")
            score = score_candidate(m.get("name", ""), cand_year, title, year)
            if score > highest_score:
                highest_score = score
                best_item = m
        return best_item
    except:
        return None

def search_imdb(title, year):
    norm_q = normalize(title)
    if not norm_q:
        return None
    url = f"https://v3.sg.media-imdb.com/suggestion/{norm_q[0]}/{requests.utils.quote(title)}.json"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=7).json()
        items = res.get("d", [])
        best_item, highest_score = None, 0
        for item in items:
            iid = item.get("id", "")
            if not iid.startswith("tt"):
                continue
            q_type = item.get("q", "")
            if q_type not in ["feature", "TV series", "TV mini-series", "movie"]:
                continue
            score = score_candidate(item.get("l", ""), item.get("y"), title, year)
            if score > highest_score:
                highest_score = score
                best_item = {
                    "id": iid,
                    "name": item.get("l"),
                    "year": item.get("y"),
                    "poster": item.get("i", {}).get("imageUrl", "")
                }
        return best_item
    except:
        return None

def resolve_metadata(parsed):
    if not parsed["title"]:
        return None
    
    match = search_cinemeta(parsed["title"], parsed["year"], parsed["type"])
    if match:
        return {"id": match["id"], "name": match["name"], "poster": match.get("poster", "")}

    if parsed["type"] == "movie":
        match = search_imdb(parsed["title"], parsed["year"])
        if match:
            return match

    return None

# ================= RUNNER & DATABASE SYNC =================

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
        except:
            time.sleep(3)
    return None

def main():
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                for item in json.load(f):
                    fid = item.get("file_id")
                    if fid:
                        existing_catalog[fid] = item
            print(f"📦 Loaded {len(existing_catalog)} baseline entries from local data.json")
        except Exception as e:
            print(f"⚠️ Could not read data.json: {e}")

    session_mgr = SessionManager(ROOT_URL)
    folders_queue = deque([(ROOT_FOLDER_ID, "Root")])
    visited_folders = set()
    all_live_files = {}

    print("🚀 Crawling Gofile directory tree...")
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
                if item.get("type") == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, item.get("name", sub_code)))
                else:
                    link = item.get("link") or item.get("directDownload") or item.get("downloadPage")
                    if link and item_id not in all_live_files:
                        all_live_files[item_id] = item
                        folder_files += 1

            if len(children) < 50:
                break
            page_num += 1
            time.sleep(0.5)

        print(f"📂 Scanned [{current_folder_name}]: {folder_files} files")

    print(f"\n🔎 Total live files currently on Gofile: {len(all_live_files)}")

    # Prune deleted files & detect renamed files
    pruned_catalog = {}
    pruned_count = 0
    renamed_count = 0

    for fid, entry in existing_catalog.items():
        if fid in all_live_files:
            fresh_item = all_live_files[fid]
            fresh_name = fresh_item.get("name", fid)
            fresh_link = fresh_item.get("link") or fresh_item.get("directDownload") or fresh_item.get("downloadPage")

            if entry.get("name") != fresh_name:
                print(f"🔄 Detected rename: '{entry.get('name')}' ➜ '{fresh_name}'. Queuing for re-index...")
                renamed_count += 1
                continue

            entry["link"] = fresh_link
            pruned_catalog[fid] = entry
        else:
            pruned_count += 1
            print(f"🗑️ Pruned deleted file: {entry.get('name')}")

    missing_ids = [fid for fid in all_live_files if fid not in pruned_catalog]
    print(f"⚡ Preserved: {len(pruned_catalog)} | Pruned: {pruned_count} | Renamed/New to Index: {len(missing_ids)}\n")

    added_count = 0
    meta_cache = {}

    for fid in missing_ids:
        item = all_live_files[fid]
        fname = item.get("name", fid)
        link = item.get("link") or item.get("directDownload") or item.get("downloadPage")
        size = item.get("size", 0)
        size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

        edition = extract_edition(fname)
        quality = extract_quality(fname)
        parsed = parse_filename(fname)

        cache_key = f"{parsed['type']}:{parsed['title']}:{parsed['year']}"
        if cache_key not in meta_cache:
            meta_cache[cache_key] = resolve_metadata(parsed)
        
        meta = meta_cache[cache_key]
        imdb_id = meta["id"] if meta else f"gf:{fid}"
        display_title = meta["name"] if meta else parsed["title"]
        poster = meta["poster"] if meta and meta.get("poster") else "https://gofile.io/dist/img/logo-small.png"

        if parsed["type"] == "series":
            pruned_catalog[fid] = {
                "file_id": fid,
                "type": "series",
                "imdb_id": imdb_id,
                "title": display_title,
                "name": fname,
                "season": parsed["season"],
                "episode": parsed["episode"],
                "stream_id": f"{imdb_id}:{parsed['season']}:{parsed['episode']}",
                "poster": poster,
                "edition": edition,
                "quality": quality,
                "size": size_mb,
                "link": link
            }
        else:
            pruned_catalog[fid] = {
                "file_id": fid,
                "type": "movie",
                "imdb_id": imdb_id,
                "title": display_title,
                "name": fname,
                "stream_id": imdb_id,
                "poster": poster,
                "edition": edition,
                "quality": quality,
                "size": size_mb,
                "link": link
            }

        added_count += 1
        edition_str = f" [{edition}]" if edition else ""
        print(f"➕ Matched: '{fname}' ➜ '{display_title}' ({imdb_id}){edition_str}")

    # Minified JSON output
    final_list = list(pruned_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, separators=(",", ":"))

    print(f"\n🎉 Finished! Total entries: {len(final_list)} (Added/Updated: {added_count}, Removed: {pruned_count})")

if __name__ == "__main__":
    main()
