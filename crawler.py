import os
import sys
import json
import time
import re
from collections import deque
from difflib import SequenceMatcher
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from playwright.sync_api import sync_playwright
import PTN

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

VALID_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".xvid", ".rmvb", ".asf", ".f4v", ".wtv", ".iso"
}

def create_pooled_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.6,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=retries)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
    return s

HTTP_CLIENT = create_pooled_session()

# ==========================================
# BROWSER SESSION MANAGER
# ==========================================

class BrowserSessionManager:
    def __init__(self, root_url):
        self.root_url = root_url
        self.session = requests.Session()
        self.last_auth_time = 0
        self.refresh_credentials()

    def refresh_credentials(self):
        print("⚡ Capturing browser session headers via Chromium...")
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
            print("❌ Failed to intercept browser session headers.")
            sys.exit(1)

        self.session.headers.clear()
        self.session.headers.update(captured["headers"])
        self.last_auth_time = time.time()
        print("✅ Session credentials captured.")

    def ensure_fresh(self):
        if time.time() - self.last_auth_time > 900:
            self.refresh_credentials()

def is_video_file(filename):
    if not filename or "." not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower()
    return ext in VALID_VIDEO_EXTENSIONS

def extract_direct_stream_link(item, fid):
    raw_link = item.get("directDownload") or item.get("link")
    server = item.get("server")
    fname = item.get("name", fid)

    if raw_link and "/d/" in raw_link and server:
        return f"https://{server}.gofile.io/download/web/{fid}/{requests.utils.quote(fname)}"
    if raw_link and not raw_link.startswith("https://gofile.io/d/"):
        return raw_link
    if server:
        return f"https://{server}.gofile.io/download/web/{fid}/{requests.utils.quote(fname)}"
    return raw_link or item.get("downloadPage")

# ==========================================
# CRAWLER ENGINE
# ==========================================

def fetch_folder_page(session_mgr, folder_code, page_num=1, max_retries=5):
    api_url = f"https://api.gofile.io/contents/{folder_code}?page={page_num}&pageSize=50"

    for attempt in range(max_retries):
        session_mgr.ensure_fresh()
        try:
            res = session_mgr.session.get(api_url, timeout=20).json()
            status = res.get("status")

            if status == "ok":
                return res
            elif status in ["error-rateLimit", "429"]:
                cool_off = 10 + (attempt * 8)
                print(f"   ⏳ Rate limited on [{folder_code}]. Pausing {cool_off}s...")
                time.sleep(cool_off)
            elif status in ["error-auth", "error-token"]:
                session_mgr.refresh_credentials()
                time.sleep(2)
            else:
                return res
        except Exception:
            time.sleep(3)

    return None

def crawl_tree(session_mgr, root_id):
    folders_queue = deque([(root_id, "Root", ["Root"])])
    visited_folders = set()
    all_live_files = {}

    while folders_queue:
        current_folder_id, current_folder_name, current_path = folders_queue.popleft()

        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        page_num = 1
        folder_files = 0
        seen_in_folder = set()

        while True:
            res = fetch_folder_page(session_mgr, current_folder_id, page_num)
            if not res or res.get("status") != "ok":
                break

            data = res.get("data", {})
            children = data.get("children", {})
            if not children:
                break

            children_items = children.items() if isinstance(children, dict) else [(c.get("id") or c.get("file_id"), c) for c in children]
            new_items_on_page = 0

            for item_id, item in children_items:
                if not item or item_id in seen_in_folder:
                    continue
                seen_in_folder.add(item_id)
                new_items_on_page += 1

                if item.get("type") == "folder":
                    sub_code = item.get("code") or item.get("id") or item_id
                    sub_name = item.get("name", sub_code)
                    if sub_code not in visited_folders and all(sub_code != f[0] for f in folders_queue):
                        folders_queue.append((sub_code, sub_name, current_path + [sub_name]))
                else:
                    fname = item.get("name", "")
                    if not is_video_file(fname):
                        continue
                    direct_link = extract_direct_stream_link(item, item_id)
                    if direct_link:
                        item["_resolved_link"] = direct_link
                        item["_parent_folder"] = current_folder_name
                        item["_folder_path"] = current_path
                        all_live_files[item_id] = item
                        folder_files += 1

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        print(f"📁 Scanned [{current_folder_name}]: {folder_files} files")
        time.sleep(0.8)

    return all_live_files

# ==========================================
# OPEN / KEYLESS MEDIA IDENTIFIERS
# ==========================================

def translate_foreign_title(text):
    """Uses Wikipedia Open API to translate foreign titles like 'Форсаж 5' -> 'Fast Five'."""
    has_non_ascii = any(ord(c) > 127 for c in text)
    if not has_non_ascii:
        return text

    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", text)
    clean = re.sub(r"\b(1080p|720p|hdtvrip|bluray|x264|x265|avc)\b", "", clean, flags=re.I).strip(" ._-")

    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={requests.utils.quote(clean)}&format=json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        search_hits = res.get("query", {}).get("search", [])
        if search_hits:
            translated = search_hits[0].get("title", "")
            # Filter out disambiguation pages
            clean_trans = re.sub(r"\(.*?\)", "", translated).strip()
            if clean_trans:
                print(f"🌐 Translated '{clean}' ➔ '{clean_trans}' via Wikipedia")
                return clean_trans
    except Exception:
        pass
    return text

def search_tvmaze_show(show_name):
    """Free, open TV show search with IMDb ID output."""
    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", show_name)
    clean = re.sub(r"\b(season|series|complete|pack|collection|cinemascope)\b.*", "", clean, flags=re.I).strip(" ._-")
    if not clean or clean.lower() in ["root", "downloads", "movies", "tv shows"]:
        return None

    url = f"https://api.tvmaze.com/singlesearch/shows?q={requests.utils.quote(clean)}&embed=episodes"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        if res and "id" in res:
            imdb_id = res.get("externals", {}).get("imdb")
            episodes = res.get("_embedded", {}).get("episodes", [])
            ep_map = []
            for ep in episodes:
                ep_map.append({
                    "season": ep.get("season", 1),
                    "episode": ep.get("number", 1),
                    "name": (ep.get("name") or "").lower()
                })
            return {
                "title": res.get("name", clean),
                "imdb_id": imdb_id,
                "poster": res.get("image", {}).get("original", ""),
                "episodes": ep_map
            }
    except Exception:
        pass
    return None

def search_cinemeta_movie(title, year):
    """Searches Cinemeta Movie catalog with title matching and year verification."""
    clean = re.sub(r"[^\w\s]", " ", title).strip()
    url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={requests.utils.quote(clean)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])

        # Priority 1: Year match within 1 year
        for m in metas:
            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            if year and cand_year and abs(int(cand_year[:4]) - int(year)) <= 1:
                return m

        # Priority 2: Direct name equality
        for m in metas:
            if m.get("name", "").lower() == clean.lower():
                return m

        # Fallback to top result if ratio is reasonable
        if metas:
            ratio = SequenceMatcher(None, metas[0].get("name", "").lower(), clean.lower()).ratio()
            if ratio >= 0.70:
                return metas[0]
    except Exception:
        pass
    return None

def match_episode_in_show(fname, ep_map):
    se_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})\b", fname)
    if se_match:
        return int(se_match.group(1)), int(se_match.group(2))

    clean_f = re.sub(r"[\(\[\{].*?[\)\]\}]", "", fname)
    clean_f = re.sub(r"[^\w\s]", " ", clean_f).strip().lower()

    best_ratio = 0
    best_ep = None
    for ep in ep_map:
        ep_title = ep.get("name", "")
        if not ep_title or len(ep_title) < 3:
            continue
        if ep_title in clean_f:
            ratio = 0.90
        else:
            ratio = SequenceMatcher(None, clean_f, ep_title).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_ep = ep

    if best_ep and best_ratio >= 0.60:
        return best_ep["season"], best_ep["episode"]

    return None, None

def make_stream_entry(fid, item, m_type, imdb_id, title, poster, season=1, episode=1, edition="", quality="1080P"):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    if m_type == "series":
        stream_id = f"{imdb_id}:{season}:{episode}"
        return {
            "file_id": fid,
            "type": "series",
            "imdb_id": imdb_id,
            "title": title,
            "name": fname,
            "season": season,
            "episode": episode,
            "stream_id": stream_id,
            "stream_ids": [stream_id],
            "poster": poster or "https://gofile.io/dist/img/logo-small.png",
            "edition": edition,
            "quality": quality,
            "size": size_mb,
            "link": link
        }
    else:
        return {
            "file_id": fid,
            "type": "movie",
            "imdb_id": imdb_id,
            "title": title,
            "name": fname,
            "stream_id": imdb_id,
            "stream_ids": [imdb_id],
            "poster": poster or "https://gofile.io/dist/img/logo-small.png",
            "edition": edition,
            "quality": quality,
            "size": size_mb,
            "link": link
        }

# ==========================================
# MAIN EXECUTION
# ==========================================

def main():
    existing_catalog = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    fid = entry.get("file_id")
                    if fid:
                        existing_catalog[fid] = entry
            print(f"📦 Loaded {len(existing_catalog)} entries from local data.json")
        except Exception as e:
            print(f"⚠️ data.json read notice: {e}")

    session_mgr = BrowserSessionManager(ROOT_URL)
    all_live_files = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    print(f"\n📊 Discovered {len(all_live_files)} live video files.")
    if not all_live_files:
        print("❌ 0 files retrieved. Preserving data.json.")
        sys.exit(1)

    final_catalog = {}
    missing_ids = []

    for fid, item in all_live_files.items():
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                final_catalog[fid] = cached
                continue
        missing_ids.append(fid)

    print(f"📌 Cached matches: {len(final_catalog)} | Items to resolve: {len(missing_ids)}\n")

    # Group unindexed items by parent folder
    folder_groups = {}
    for fid in missing_ids:
        item = all_live_files[fid]
        parent = item.get("_parent_folder", "Root")
        folder_groups.setdefault(parent, []).append((fid, item))

    for folder_name, items in folder_groups.items():
        # Check if parent folder matches an authentic show on TVMaze
        show_match = None
        if folder_name.lower() not in ["root", "downloads", "movies"]:
            show_match = search_tvmaze_show(folder_name)

        if show_match and show_match.get("imdb_id"):
            series_id = show_match["imdb_id"]
            series_title = show_match["title"]
            poster = show_match["poster"]
            ep_map = show_match["episodes"]
            print(f"📺 TV Series Group Identified: [{folder_name}] ➔ {series_title} ({series_id})")

            extra_seq = 1
            for fid, item in items:
                raw_name = item.get("name", "")
                parsed = PTN.parse(raw_name)
                is_extra = any(tag in raw_name.lower() for tag in ["extra", "promo", "interview", "featurette", "bonus"])

                if is_extra:
                    final_catalog[fid] = make_stream_entry(
                        fid, item, "series", series_id, series_title, poster,
                        season=0, episode=extra_seq, edition="Special / Extra"
                    )
                    extra_seq += 1
                else:
                    s_num, e_num = match_episode_in_show(raw_name, ep_map)
                    if s_num is not None and e_num is not None:
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", series_id, series_title, poster,
                            season=s_num, episode=e_num
                        )
                    else:
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", series_id, series_title, poster,
                            season=0, episode=extra_seq, edition=f"Short: {raw_name[:25]}"
                        )
                        extra_seq += 1
            continue

        # Individual File Resolution (Movies & Standalone Files)
        for fid, item in items:
            raw_name = item.get("name", "")
            
            # Step 1: Translate non-English titles (e.g. Форсаж 5)
            clean_search_str = translate_foreign_title(raw_name)

            # Step 2: Extract clean title + year via PTN
            parsed = PTN.parse(clean_search_str)
            title = parsed.get("title") or clean_search_str
            year = parsed.get("year")
            quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

            # Step 3: Match Movie via Cinemeta
            match = search_cinemeta_movie(title, year)
            if match:
                movie_id = match.get("imdb_id") or match.get("id")
                movie_title = match.get("name", title)
                poster = match.get("poster", "")
                final_catalog[fid] = make_stream_entry(
                    fid, item, "movie", movie_id, movie_title, poster, quality=quality
                )
                print(f"🍿 Movie: {raw_name} ➔ {movie_title} ({movie_id})")
            else:
                # Raw Fallback
                clean_raw = re.sub(r"[\(\[\{].*?[\)\]\}]", "", raw_name)
                clean_title = os.path.splitext(clean_raw)[0].replace(".", " ").strip()
                final_catalog[fid] = make_stream_entry(
                    fid, item, "movie", f"gf:{fid}", clean_title, "", quality=quality
                )
                print(f"⚠️ Unmatched Fallback: {raw_name} (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
