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

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"

VALID_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".xvid", ".rmvb", ".asf", ".f4v", ".wtv", ".iso"
}

GENERIC_FOLDERS = {
    "extras", "featurettes", "bonus", "specials", "behind the scenes",
    "season", "root", "all items", "downloads", "movies", "tv shows", "unknown"
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
# ATOMIC SIMILARITY & TMDB VERIFIER
# ==========================================

def clean_noise(text):
    if not text:
        return ""
    # Strip release noise, codecs, tags
    cleaned = re.sub(r"\[.*?\]", "", text)
    cleaned = re.sub(r"\(.*?\)", "", cleaned)
    cleaned = re.sub(r"\b(1080p|720p|2160p|4k|bluray|webrip|webdl|hdtvrip|x264|x265|hevc|aac5?\.?1?|open\s+matte|repack)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[._]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned

def string_dice_similarity(a, b):
    """Computes Sørensen–Dice coefficient on character bigrams for whole-string equality."""
    a_clean = re.sub(r"[^\w]", "", a.lower())
    b_clean = re.sub(r"[^\w]", "", b.lower())
    if not a_clean or not b_clean:
        return 0.0
    if a_clean == b_clean:
        return 1.0
    return SequenceMatcher(None, a_clean, b_clean).ratio()

def search_tmdb_atomic(clean_title, year=None, target_media_type=None):
    """Searches TMDB and gates candidates using strict whole-string similarity & year checks."""
    if not TMDB_API_KEY:
        return None

    if not clean_title or len(clean_title) < 2:
        return None

    # Endpoint selection
    if target_media_type == "movie":
        url = "https://api.themoviedb.org/3/search/movie"
    elif target_media_type == "tv":
        url = "https://api.themoviedb.org/3/search/tv"
    else:
        url = "https://api.themoviedb.org/3/search/multi"

    params = {
        "api_key": TMDB_API_KEY,
        "query": clean_title,
        "include_adult": "false"
    }
    if year and target_media_type == "movie":
        params["year"] = str(year)
    elif year and target_media_type == "tv":
        params["first_air_date_year"] = str(year)

    try:
        res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
        results = res.get("results", [])

        # Retry without year filter if no results
        if not results and year:
            params.pop("year", None)
            params.pop("first_air_date_year", None)
            res = HTTP_CLIENT.get(url, params=params, timeout=6).json()
            results = res.get("results", [])

        media_hits = [r for r in results if r.get("media_type") in ["movie", "tv"] or target_media_type]
        if not media_hits:
            return None

        best_candidate = None
        best_similarity = 0.0

        for item in media_hits:
            cand_name = item.get("title") or item.get("name") or ""
            cand_orig = item.get("original_title") or item.get("original_name") or ""
            
            # Test similarity against both translated and original title (e.g. Russian)
            sim_name = string_dice_similarity(clean_title, cand_name)
            sim_orig = string_dice_similarity(clean_title, cand_orig)
            similarity = max(sim_name, sim_orig)

            # Check year window
            cand_release = item.get("release_date") or item.get("first_air_date") or ""
            if year and cand_release:
                try:
                    c_yr = int(cand_release[:4])
                    if abs(c_yr - int(year)) > 1:
                        # Heavy penalty if year is off by more than 1 year
                        similarity -= 0.35
                except Exception:
                    pass

            if similarity > best_similarity:
                best_similarity = similarity
                best_candidate = item

        # Strict Gate: Candidate must have at least 65% whole-title similarity
        if best_candidate and best_similarity >= 0.65:
            m_type = "movie" if (best_candidate.get("media_type") == "movie" or target_media_type == "movie") else "series"
            tmdb_id = best_candidate.get("id")

            # Resolve external IMDb ID
            lookup_type = "movie" if m_type == "movie" else "tv"
            ext_url = f"https://api.themoviedb.org/3/{lookup_type}/{tmdb_id}/external_ids"
            ext_res = HTTP_CLIENT.get(ext_url, params={"api_key": TMDB_API_KEY}, timeout=5).json()
            imdb_id = ext_res.get("imdb_id")

            poster_path = best_candidate.get("poster_path")
            poster = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""

            return {
                "type": m_type,
                "imdb_id": imdb_id or f"tmdb:{tmdb_id}",
                "title": best_candidate.get("title") or best_candidate.get("name"),
                "poster": poster,
                "similarity": best_similarity
            }

    except Exception:
        pass

    return None

def fetch_series_episodes_from_cinemeta(imdb_id):
    url = f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=6).json()
        meta = res.get("meta", {})
        videos = meta.get("videos", [])
        return meta.get("poster", ""), videos
    except Exception:
        return "", []

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
# MAIN ROUTINE
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

    # Group unindexed files by parent directory
    folder_groups = {}
    for fid in missing_ids:
        item = all_live_files[fid]
        parent = item.get("_parent_folder", "Root")
        folder_groups.setdefault(parent, []).append((fid, item))

    for folder_name, items in folder_groups.items():
        # Step 1: Detect Show Collection Folders (e.g., "Tom and Jerry ... Collection")
        clean_folder = clean_noise(folder_name)
        is_collection = any(tag in folder_name.lower() for tag in ["collection", "cinemascope", "season", "series", "complete pack"])
        parent_show_match = None

        if is_collection and folder_name.lower() not in GENERIC_FOLDERS:
            # Query TMDB explicitly for TV series with the clean parent show title
            clean_anchor = re.sub(r"\b(the\s+)?(complete|collection|cinemascope|anthology|pack|season|series)\b.*", "", clean_folder, flags=re.I).strip(" ._-")
            parent_show_match = search_tmdb_atomic(clean_anchor, target_media_type="tv")

        # If parent folder is an authentic show collection, child files inherit its identity
        if parent_show_match and parent_show_match.get("type") == "series":
            series_id = parent_show_match.get("imdb_id")
            series_title = parent_show_match.get("title")
            poster, _ = fetch_series_episodes_from_cinemeta(series_id)
            print(f"📺 Show Anchor Locked: [{folder_name}] ➔ {series_title} ({series_id})")

            for seq, (fid, item) in enumerate(items, start=1):
                raw_name = item.get("name", "")
                parsed = PTN.parse(raw_name)
                season = parsed.get("season", 1) or 1
                episode = parsed.get("episode", seq) or seq
                quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

                # Route extras, shorts, and promos under Season 0 (Specials)
                if any(tag in raw_name.lower() for tag in ["extra", "promo", "interview", "bonus"]):
                    season = 0

                final_catalog[fid] = make_stream_entry(
                    fid, item, "series", series_id, series_title, poster,
                    season=season, episode=episode, edition=str(quality)
                )
            continue

        # Step 2: Individual File Resolution for Standalone Movies & Loose Files
        for fid, item in items:
            raw_name = item.get("name", "")
            parsed = PTN.parse(raw_name)

            title = parsed.get("title")
            year = parsed.get("year")
            quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

            # Fallback if PTN stripped everything or parsed foreign title inaccurately
            if not title:
                title = clean_noise(os.path.splitext(raw_name)[0])

            # Extract year from filename regex if PTN missed it
            if not year:
                ym = re.search(r"\b(19\d\d|20\d\d)\b", raw_name)
                if ym:
                    year = int(ym.group(1))

            # Atomic match against TMDB
            clean_search_title = clean_noise(title)
            match = search_tmdb_atomic(clean_search_title, year=year)

            if match:
                m_type = match.get("type")
                m_id = match.get("imdb_id")
                m_title = match.get("title")
                poster = match.get("poster")
                season = parsed.get("season", 1) or 1
                episode = parsed.get("episode", 1) or 1

                final_catalog[fid] = make_stream_entry(
                    fid, item, m_type, m_id, m_title, poster,
                    season=season, episode=episode, quality=str(quality)
                )
                print(f"✅ Matched ({match['similarity']:.2f}): {raw_name} ➔ {m_title} ({m_id}) [{m_type.upper()}]")
            else:
                # Local Guard: Refuse to guess. Save with clean title and avoid wrong posters/IDs
                clean_display_title = clean_noise(os.path.splitext(raw_name)[0])
                final_catalog[fid] = make_stream_entry(
                    fid, item, "movie", f"gf:{fid}", clean_display_title, "", quality=str(quality)
                )
                print(f"🛡️ Guard Protected (Unmatched): {raw_name} ➔ '{clean_display_title}' (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
