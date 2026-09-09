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
from guessit import guessit

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
            print(f"📁 Scanned [{current_folder_name}]: {len(container_files)} files")

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

def is_true_series_container(folder_name, sample_filenames):
    """Only returns True if folder or its files strictly indicate an episodic series."""
    f_lower = folder_name.lower()
    if any(tag in f_lower for tag in ["season", "series", "s0", "s1", "s2", "complete pack"]):
        return True

    # Count how many files have episodic patterns
    ep_count = 0
    for name in sample_filenames:
        n_lower = name.lower()
        if re.search(r"\b[sS]\d{1,2}[eE]\d{1,3}\b", name) or re.search(r"\b\d{1,2}x\d{1,3}\b", name):
            ep_count += 1
        elif any(tag in n_lower for tag in ["extra", "promo", "featurette", "bonus"]):
            ep_count += 1

    # If >40% of files have episode tags, it's a TV show
    return ep_count >= max(2, int(len(sample_filenames) * 0.4))

def search_cinemeta_show(title):
    clean_t = re.sub(r"[^\w\s]", " ", title).strip()
    url = f"https://v3-cinemeta.strem.io/catalog/series/top/search={requests.utils.quote(clean_t)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        for m in metas:
            cand_norm = normalize(m.get("name", ""))
            query_norm = normalize(clean_t)
            if cand_norm == query_norm or query_norm in cand_norm:
                return m
        return metas[0] if metas else None
    except Exception:
        return None

def search_cinemeta_movie(title, year):
    clean_t = re.sub(r"[^\w\s]", " ", title).strip()
    url = f"https://v3-cinemeta.strem.io/catalog/movie/top/search={requests.utils.quote(clean_t)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        
        # 1. First priority: Title + Year exact match
        for m in metas:
            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            cand_name = m.get("name", "")
            if year and cand_year and abs(int(cand_year[:4]) - int(year)) <= 1:
                ratio = SequenceMatcher(None, normalize(cand_name), normalize(clean_t)).ratio()
                if ratio >= 0.70:
                    return m

        # 2. Second priority: Title match without year constraint
        for m in metas:
            cand_name = m.get("name", "")
            if normalize(cand_name) == normalize(clean_t):
                return m

        # 3. Fallback: First candidate if ratio is high
        if metas:
            ratio = SequenceMatcher(None, normalize(metas[0].get("name", "")), normalize(clean_t)).ratio()
            if ratio >= 0.80:
                return metas[0]
    except Exception:
        pass
    return None

def fetch_show_episodes(imdb_id):
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
    se_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})\b", fname)
    if se_match:
        return int(se_match.group(1)), int(se_match.group(2))

    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", fname)
    if x_match:
        return int(x_match.group(1)), int(x_match.group(2))

    # Match by title
    clean_f = re.sub(r"[\(\[\{].*?[\)\]\}]", "", fname)
    clean_f = re.sub(r"[^\w\s]", " ", clean_f).strip().lower()

    best_ratio = 0
    best_ep = None
    for ep in ep_map:
        ep_title = ep.get("title", "")
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
    folder_containers = crawl_tree(session_mgr, ROOT_FOLDER_ID)

    all_scanned_files = {}
    for c in folder_containers.values():
        all_scanned_files.update(c["files"])

    print(f"\n📊 Discovered {len(all_scanned_files)} live video files across {len(folder_containers)} folders.")
    if not all_scanned_files:
        print("❌ 0 files retrieved. Preserving data.json.")
        sys.exit(1)

    final_catalog = {}
    loose_files = []

    # Phase 1: STRICT Container Check (Only real TV shows qualify)
    print("\n🏗️ Evaluating folder containers...")
    for folder_id, container in folder_containers.items():
        fname = container["name"]
        files = container["files"]
        sample_names = [f.get("name", "") for f in files.values()]

        # Only check against Series catalog if explicitly episodic!
        if is_true_series_container(fname, sample_names):
            clean_show_title = re.sub(r"[\(\[\{].*?[\)\]\}]", "", fname)
            clean_show_title = re.sub(r"\b(season|series|complete|pack|collection)\b.*", "", clean_show_title, flags=re.I).strip(" ._-")

            matched_show = search_cinemeta_show(clean_show_title) if clean_show_title else None
            if matched_show:
                show_id = matched_show.get("imdb_id") or matched_show.get("id")
                show_title = matched_show.get("name", clean_show_title)
                show_meta, ep_map = fetch_show_episodes(show_id)
                poster = show_meta.get("poster") or matched_show.get("poster", "")

                print(f"📺 Verified Series: [{fname}] ➔ {show_title} ({show_id}) with {len(ep_map)} episodes")

                extra_seq = 1
                for fid, item in files.items():
                    iname = item.get("name", "")
                    is_extra = any(tag in iname.lower() for tag in ["extra", "promo", "interview", "featurette", "bonus"])

                    if is_extra:
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
                            final_catalog[fid] = make_stream_entry(
                                fid, item, "series", show_id, show_title, poster,
                                season=0, episode=extra_seq, edition=f"Short: {iname[:25]}"
                            )
                            extra_seq += 1
                continue

        # If not a verified episodic series, process files individually
        for fid, item in files.items():
            loose_files.append((fid, item))

    # Phase 2: Resolve Movies & Standalone Files with Direct Cinemeta Matching
    print(f"\n🎬 Resolving {len(loose_files)} movies & standalone files...")
    for fid, item in loose_files:
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

        # Fallback if guessit missed the title
        if not title:
            clean_fallback = re.sub(r"[\(\[\{].*?[\)\]\}]", "", raw_name)
            title = os.path.splitext(clean_fallback)[0].replace(".", " ").strip()

        edition = str(g.get("edition", ""))
        quality = str(g.get("screen_size", "1080p")).upper()

        matched_movie = search_cinemeta_movie(title, year)
        if matched_movie:
            movie_id = matched_movie.get("imdb_id") or matched_movie.get("id")
            movie_title = matched_movie.get("name")
            poster = matched_movie.get("poster", "")
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", movie_id, movie_title, poster, edition=edition, quality=quality
            )
            print(f"🍿 Movie: {raw_name} ➔ {movie_title} ({movie_id})")
        else:
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", title, "", edition=edition, quality=quality
            )
            print(f"⚠️ Unmatched Fallback: {raw_name} ➔ '{title}' (gf:{fid})")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Sync completed! Catalog items: {len(output_list)}")

if __name__ == "__main__":
    main()
