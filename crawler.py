import os
import sys
import json
import time
import re
from collections import deque
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

NOISE_TOKENS = {
    "1080p", "720p", "480p", "2160p", "4k", "bluray", "webrip", "webdl", "hdtv",
    "x264", "x265", "hevc", "h264", "h265", "aac", "aac5", "ddp5", "atmos",
    "repack", "remux", "open", "matte", "yts", "mx", "garshasp", "rarbg",
    "complete", "collection", "cinemascope", "anthology", "pack", "series",
    "season", "movies", "specials", "bonus", "extra", "extras", "promo",
    "interview", "root", "downloads", "all", "items", "the", "a", "an", "and", "of"
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
# CRAWLER ENGINE (TRACKS FULL PATH ANCESTRY)
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
# TOKEN-SET JACCARD & AMBIGUITY GATEKEEPER
# ==========================================

def tokenize(text):
    """Splits string into normalized alphanumeric tokens minus stopwords."""
    if not text:
        return set()
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = set(cleaned.split())
    return tokens - NOISE_TOKENS

def compute_token_jaccard(target_tokens, candidate_tokens):
    """Calculates weighted directional containment and intersection over union."""
    if not target_tokens or not candidate_tokens:
        return 0.0
    intersection = target_tokens & candidate_tokens
    if not intersection:
        return 0.0
    # Recall (how much of candidate title is found in target string)
    recall = len(intersection) / len(candidate_tokens)
    # Strict Jaccard IoU
    iou = len(intersection) / len(target_tokens | candidate_tokens)
    return (recall * 0.7) + (iou * 0.3)

def search_and_score_cinemeta(catalog_type, query, target_tokens, expected_year=None):
    """Queries Cinemeta and gates candidates with token scoring."""
    url = f"https://v3-cinemeta.strem.io/catalog/{catalog_type}/top/search={requests.utils.quote(query)}.json"
    try:
        res = HTTP_CLIENT.get(url, timeout=5).json()
        metas = res.get("metas", [])
        scored_candidates = []

        for m in metas:
            cand_name = m.get("name", "")
            cand_tokens = tokenize(cand_name)
            score = compute_token_jaccard(target_tokens, cand_tokens)

            cand_year = str(m.get("year") or m.get("releaseInfo") or "")
            if expected_year and cand_year:
                try:
                    c_yr = int(cand_year[:4])
                    if abs(c_yr - int(expected_year)) <= 1:
                        score += 0.25
                    else:
                        score -= 0.40
                except Exception:
                    pass

            if score > 0.35:
                scored_candidates.append((score, m))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        # Ambiguity Gatekeeper
        if scored_candidates:
            top_score, top_match = scored_candidates[0]
            if top_score >= 0.58:
                if len(scored_candidates) > 1:
                    second_score = scored_candidates[1][0]
                    # Discard if tied or too close
                    if (top_score - second_score) < 0.08 and top_score < 0.75:
                        return None
                return top_match
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

def match_episode_via_tokens(fname, ep_map):
    # Rule 1: Explicit SxxExx
    se_match = re.search(r"\b[sS](\d{1,2})[eE](\d{1,3})\b", fname)
    if se_match:
        return int(se_match.group(1)), int(se_match.group(2))

    x_match = re.search(r"\b(\d{1,2})x(\d{1,3})\b", fname)
    if x_match:
        return int(x_match.group(1)), int(x_match.group(2))

    # Rule 2: Token overlap against official episode title list
    target_tokens = tokenize(fname)
    best_score = 0
    best_ep = None

    for ep in ep_map:
        ep_tokens = tokenize(ep.get("title", ""))
        score = compute_token_jaccard(target_tokens, ep_tokens)
        if score > best_score:
            best_score = score
            best_ep = ep

    if best_ep and best_score >= 0.50:
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

    # Phase 1: Container Token Matching
    print("\n🏗️ Scoring folder containers against Cinemeta Series Catalog...")
    for folder_id, container in folder_containers.items():
        fname = container["name"]
        files = container["files"]
        path_list = container["path"]

        # Aggregate tokens across the directory ancestry
        folder_tokens = set()
        for p in path_list:
            if p.lower() not in ["root", "downloads", "movies"]:
                folder_tokens.update(tokenize(p))

        matched_show = None
        if folder_tokens:
            # Query Cinemeta with clean directory tokens
            search_query = " ".join(list(folder_tokens)[:5])
            matched_show = search_and_score_cinemeta("series", search_query, folder_tokens)

        if matched_show:
            show_id = matched_show.get("imdb_id") or matched_show.get("id")
            show_title = matched_show.get("name")
            show_meta, ep_map = fetch_show_episodes(show_id)
            poster = show_meta.get("poster") or matched_show.get("poster", "")

            print(f"📺 Container Confirmed [SERIES]: [{fname}] ➔ {show_title} ({show_id}) with {len(ep_map)} episodes")

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
                    s_num, e_num = match_episode_via_tokens(iname, ep_map)
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
        else:
            for fid, item in files.items():
                loose_files.append((fid, item))

    # Phase 2: Standalone Movies / Unmatched Containers
    print(f"\n🎬 Scoring {len(loose_files)} loose files using Token-Set Jaccard...")
    for fid, item in loose_files:
        if fid in existing_catalog:
            cached = existing_catalog[fid]
            if cached.get("name") == item.get("name"):
                cached["link"] = item.get("_resolved_link")
                final_catalog[fid] = cached
                continue

        raw_name = item.get("name", "")
        file_tokens = tokenize(raw_name)

        g = guessit(raw_name)
        year = g.get("year")
        if not year:
            ym = re.search(r"\b(19\d\d|20\d\d)\b", raw_name)
            if ym:
                year = int(ym.group(1))

        clean_query = " ".join(list(file_tokens)[:4]) if file_tokens else raw_name
        matched_movie = search_and_score_cinemeta("movie", clean_query, file_tokens, expected_year=year)

        edition = str(g.get("edition", ""))
        quality = str(g.get("screen_size", "1080p")).upper()

        if matched_movie:
            movie_id = matched_movie.get("imdb_id") or matched_movie.get("id")
            movie_title = matched_movie.get("name")
            poster = matched_movie.get("poster", "")
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", movie_id, movie_title, poster, edition=edition, quality=quality
            )
            print(f"🍿 Matched: {raw_name} ➔ {movie_title} ({movie_id})")
        else:
            # Ambiguity Gatekeeper Fallback: Avoids matching with wrong items
            clean_title = re.sub(r"[\(\[\{].*?[\)\]\}]", "", raw_name)
            clean_title = os.path.splitext(clean_title)[0].replace(".", " ").strip()
            final_catalog[fid] = make_stream_entry(
                fid, item, "movie", f"gf:{fid}", clean_title, "", edition=edition, quality=quality
            )
            print(f"🛡️ Gatekeeper Protected (Raw Fallback): {raw_name} ➔ '{clean_title}'")

    output_list = list(final_catalog.values())
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(output_list, f, indent=2)

    print(f"\n🎉 Sync completed! Catalog items: {len(output_list)}")

if __name__ == "__main__":
    main()
