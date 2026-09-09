import os
import sys
import json
import time
import re
from collections import deque
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from playwright.sync_api import sync_playwright
from guessit import guessit

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

SERIES_ACRONYMS = {
    "bcs": "Better Call Saul",
    "bb": "Breaking Bad",
    "got": "Game of Thrones",
    "hotd": "House of the Dragon",
    "himym": "How I Met Your Mother",
    "tbbt": "The Big Bang Theory",
    "atla": "Avatar: The Last Airbender"
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
        print("⚡ Capturing authenticated Gofile session headers via Chromium...")
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
# CRAWLER (CAPTURES CONTAINERS AND HIERARCHY)
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
    folder_containers = {}

    while folders_queue:
        current_folder_id, current_folder_name, current_path = folders_queue.popleft()

        if current_folder_id in visited_folders:
            continue
        visited_folders.add(current_folder_id)

        container_files = {}
        page_num = 1
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
                        container_files[item_id] = item

            if new_items_on_page == 0 or len(children_items) < 50:
                break

            page_num += 1
            time.sleep(0.5)

        if container_files:
            folder_containers[current_folder_id] = {
                "name": current_folder_name,
                "path": current_path,
                "files": container_files
            }
            print(f"📁 Scanned [{current_folder_name}]: {len(container_files)} video files")

        time.sleep(0.8)

    return folder_containers

# ==========================================
# STRING & CINEMETA UTILITIES
# ==========================================

def normalize(s):
    return re.sub(r"[^\w]", "", (s or "").lower())

def clean_preparse_filename(filename):
    clean_name = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", filename, flags=re.I)
    clean_name = re.sub(r"\[(?:TTT|CN Dub|Tamil|Hindi|Eng|Dual Audio|HEVC|10bit|YTS\.[A-Z]+)[^\]]*\]", "", clean_name, flags=re.I)
    clean_name = re.sub(r"\b(ia)\b", "", clean_name, flags=re.I).strip(" ._-")
    return clean_name

def extract_primary_folder_title(parent_name):
    """Isolates the show title from collection strings."""
    if not parent_name:
        return ""
    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", parent_name)
    clean = re.sub(r"\b(the\s+)?(complete|collection|cinemascope|anthology|pack|season|series|movies|specials|films)\b", "", clean, flags=re.I)
    clean = re.sub(r"\s*-\s*.*", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" ._-")
    if clean.lower() in GENERIC_FOLDERS or len(clean) < 3:
        return ""
    return SERIES_ACRONYMS.get(clean.lower(), clean)

def search_cinemeta_show(title):
    """Directly queries Cinemeta Series Catalog."""
    clean_t = re.sub(r"[^\w\s]", " ", title).strip()
    url = f"https://v3-cinemeta.strem.io/catalog/series/top/search={requests.utils.quote(clean_t)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        for m in metas:
            if normalize(m.get("name", "")) == normalize(clean_t) or normalize(clean_t) in normalize(m.get("name", "")):
                return m
        return metas[0] if metas else None
    except Exception:
        return None

def search_cinemeta_movie(title, year):
    """Directly queries Cinemeta Movie Catalog with strict year matching."""
    clean_t = re.sub(r"[^\w\s]", " ", title).strip()
    url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={requests.utils.quote(clean_t)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        for m in metas:
            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            if year and cand_year and abs(int(cand_year[:4]) - int(year)) <= 1:
                return m
            if not year and normalize(m.get("name", "")) == normalize(clean_t):
                return m
        return metas[0] if (metas and not year) else None
    except Exception:
        return None

def fetch_show_episodes(imdb_id):
    """Fetches full episode breakdown for a show from Cinemeta."""
    url = f"https://v3-cinemeta.strem.io/meta/series/{imdb_id}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=6).json()
        meta = res.get("meta", {})
        videos = meta.get("videos", [])
        episodes_map = []
        for v in videos:
            episodes_map.append({
                "season": v.get("season", 1),
                "episode": v.get("episode") or v.get("number") or 1,
                "title": (v.get("title") or v.get("name") or "").lower(),
                "id": v.get("id")
            })
        return meta, episodes_map
    except Exception:
        return {}, []

def match_episode_in_show(fname, ep_map):
    """Identifies episode/season number via SxxExx tag or title fuzzy match."""
    clean_f = re.sub(r"[\(\[\{].*?[\)\]\}]", "", fname)
    clean_f = re.sub(r"[^\w\s]", " ", clean_f).strip().lower()

    # Rule 1: Explicit S01E02 or 1x02 tag
    se_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})\b", fname)
    if se_match:
        return int(se_match.group(1)), int(se_match.group(2))

    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", fname)
    if x_match:
        return int(x_match.group(1)), int(x_match.group(2))

    single_ep = re.search(r"\b[eE](\d{1,3})\b", fname)
    if single_ep:
        return 1, int(single_ep.group(1))

    # Rule 2: Match short/episode name against official show episode list
    best_ratio = 0
    best_ep = None
    for ep in ep_map:
        ep_title = ep.get("title", "")
        if not ep_title or len(ep_title) < 3:
            continue
        if ep_title in clean_f:
            ratio = len(ep_title) / max(len(clean_f), 1) + 0.4
        else:
            ratio = SequenceMatcher(None, clean_f, ep_title).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_ep = ep

    if best_ep and best_ratio >= 0.52:
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
    folder_containers = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    all_scanned_files = {}
    for c in folder_containers.values():
        all_scanned_files.update(c["files"])

    print(f"\n📊 Discovered {len(all_scanned_files)} live video files across {len(folder_containers)} folders.")
    if not all_scanned_files:
        print("❌ 0 files retrieved. Preserving data.json and exiting.")
        sys.exit(1)

    final_catalog = {}
    loose_files = []

    # Phase 1: Evaluate Containers Top-Down
    print("\n🏗️ Evaluating folder containers against Cinemeta Series Catalog...")
    for folder_id, container in folder_containers.items():
        fname = container["name"]
        files = container["files"]
        path_list = container["path"]

        # Check full folder path for series names (e.g. Root / Ed, Edd n Eddy / Extras)
        candidate_show_name = ""
        for p in reversed(path_list):
            extracted = extract_primary_folder_title(p)
            if extracted:
                candidate_show_name = extracted
                break

        matched_show = search_cinemeta_show(candidate_show_name) if candidate_show_name else None

        # Container is an identified Series/Show
        if matched_show:
            show_id = matched_show.get("imdb_id") or matched_show.get("id")
            show_title = matched_show.get("name", candidate_show_name)
            show_meta, ep_map = fetch_show_episodes(show_id)
            poster = show_meta.get("poster") or matched_show.get("poster", "")

            print(f"📺 Series Confirmed: [{fname}] ➔ {show_title} ({show_id}) with {len(ep_map)} indexed episodes")

            extra_seq = 1
            for fid, item in files.items():
                iname = item.get("name", "")
                is_extra = any(tag in iname.lower() for tag in ["extra", "promo", "interview", "featurette", "bonus"])

                if is_extra:
                    # Specials slot (Season 0)
                    final_catalog[fid] = make_stream_entry(
                        fid, item, "series", show_id, show_title, poster,
                        season=0, episode=extra_seq, edition="Special / Extra"
                    )
                    extra_seq += 1
                else:
                    s_num, e_num = match_episode_in_show(iname, ep_map)
                    if s_num is not None and e_num is not None:
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", show_id, show_title, poster,
                            season=s_num, episode=e_num
                        )
                    else:
                        # Fallback for shorts not strictly numbered in seasons
                        final_catalog[fid] = make_stream_entry(
                            fid, item, "series", show_id, show_title, poster,
                            season=0, episode=extra_seq, edition=f"Short: {iname[:25]}"
                        )
                        extra_seq += 1
        else:
            # Not a series container -> route files to standalone/movie phase
            for fid, item in files.items():
                loose_files.append((fid, item))

    # Phase 2: Resolve Standalone / Loose Movies
    print(f"\n🎬 Resolving {len(loose_files)} loose / standalone files with GuessIt...")
    for fid, item in loose_files:
        # Check cache
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                final_catalog[fid] = cached
                continue

        raw_name = item.get("name", "")
        clean_name = clean_preparse_filename(raw_name)
        g = guessit(clean_name)
        title = g.get("title")
        year = g.get("year")

        if not year:
            ym = re.search(r"\b(19\d\d|20\d\d)\b", clean_name)
            if ym:
                year = int(ym.group(1))

        if not title:
            title = clean_name

        quality = str(g.get("screen_size", "1080p")).upper()
        edition = str(g.get("edition", ""))

        matched_movie = search_cinemeta_movie(title, year)
        if matched_movie:
            movie_id = matched_movie.get("imdb_id") or matched_movie.get("id")
            movie_title = matched_movie.get("name", title)
            poster = matched_movie.get("poster", "")
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", movie_id, movie_title, poster, edition=edition, quality=quality
            )
            print(f"🍿 Movie: {raw_name} ➔ {movie_title} ({movie_id})")
        else:
            # Fallback catalog entry
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", title, "", edition=edition, quality=quality
            )
            print(f"⚠️ Unmatched Movie: {raw_name} (Saved as gf:{fid})")

    # Output data.json
    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} entries.")

if __name__ == "__main__":
    main()
