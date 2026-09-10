import os
import sys
import json
import time
import re
import asyncio
from difflib import SequenceMatcher
from urllib.parse import quote
import aiohttp
from playwright.async_api import async_playwright
import PTN

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"
KNOWLEDGE_FILE = "knowledge.json"
DATA_FILE = "data.json"

CONCURRENCY_LIMIT = 20

VALID_VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".wmv", ".mov", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp",
    ".divx", ".xvid", ".rmvb", ".asf", ".f4v", ".wtv", ".iso"
}

GENERIC_FOLDERS = {
    "extras", "featurettes", "bonus", "specials", "behind the scenes",
    "season", "root", "all items", "downloads", "movies", "tv shows", "unknown"
}

KNOWN_FEATURE_FILMS = {
    "space jam",
    "space jam a new legacy",
    "looney tunes back in action",
    "a goofy movie",
    "an extremely goofy movie",
    "who framed roger rabbit",
    "tom and jerry the movie",
    "the movie"
}

KNOWN_TITLE_ALIASES = {
    "baaghi": ["Baaghi", "Baaghi: A Rebel for Love"]
}

CANONICAL_CARTOON_FRANCHISES = {
    "tom and jerry": {
        "imdb_id": "tt0032138",
        "title": "Tom and Jerry",
        "poster": "https://m.media-amazon.com/images/M/MV5BMGUyNmIxNjItMGFkZi00YmU4LWFjM2QtYjMwM2MyYTU2MWI1XkEyXkFqcGc@._V1_.jpg"
    },
    "looney tunes": {
        "imdb_id": "tt0021064",
        "title": "Looney Tunes",
        "poster": "https://m.media-amazon.com/images/M/MV5BNDQzNDk4NTctNTk2Zi00ODIxLWFhYTMtYmJmZjNhOTU3Y2Y4XkEyXkFqcGc@._V1_.jpg"
    },
    "popeye": {
        "imdb_id": "tt0023783",
        "title": "Popeye the Sailor",
        "poster": "https://m.media-amazon.com/images/M/MV5BMTgzMDc0Mzc3M15BMl5BanBnXkFtZTcwNTI1OTAyMQ@@._V1_.jpg"
    },
    "pink panther": {
        "imdb_id": "tt0057779",
        "title": "The Pink Panther Show",
        "poster": "https://m.media-amazon.com/images/M/MV5BZDhjOTI5ODUtY2I3Mi00ODMzLWExMDktYzU0MzMwNDNmODRhXkEyXkFqcGc@._V1_.jpg"
    },
    "mickey mouse": {
        "imdb_id": "tt0020170",
        "title": "Mickey Mouse",
        "poster": "https://m.media-amazon.com/images/M/MV5BNmNhMWM1NWYtNjI1Mi00ZGNhLWI5ZWEtNTliMjA2NmVjZTY0XkEyXkFqcGc@._V1_.jpg"
    }
}

# ==========================================
# KNOWLEDGE BASE
# ==========================================

def load_json(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Write notice [{filepath}]: {e}")

# ==========================================
# ASYNC BROWSER SESSION CAPTURE
# ==========================================

async def get_browser_session_headers(root_url):
    print("⚡ Fast-capturing browser session headers via Async Playwright...")
    captured = {"headers": {}}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = await context.new_page()

        def intercept_request(request):
            if "contents/" in request.url:
                captured["headers"] = dict(request.headers)

        page.on("request", intercept_request)

        try:
            await page.goto(root_url, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(2)
        except Exception as e:
            print(f"Playwright notice: {e}")
        finally:
            await browser.close()

    if not captured["headers"]:
        print("❌ Failed to intercept browser session headers.")
        sys.exit(1)

    print("✅ Session credentials captured.")
    return captured["headers"]

# ==========================================
# STRING & METADATA PARSING
# ==========================================

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
        return f"https://{server}.gofile.io/download/web/{fid}/{quote(fname)}"
    if raw_link and not raw_link.startswith("https://gofile.io/d/"):
        return raw_link
    if server:
        return f"https://{server}.gofile.io/download/web/{fid}/{quote(fname)}"
    return raw_link or item.get("downloadPage")

def extract_versions_and_cuts(raw_name):
    cuts = []
    f_norm = re.sub(r"[-_.]+", " ", raw_name.lower())
    if "open matte" in f_norm or "openmatte" in f_norm: cuts.append("Open Matte")
    if "imax" in f_norm: cuts.append("IMAX")
    if "director's cut" in f_norm or "directors cut" in f_norm: cuts.append("Director's Cut")
    if "extended" in f_norm: cuts.append("Extended")
    if "theatrical" in f_norm: cuts.append("Theatrical")
    if "unrated" in f_norm: cuts.append("Unrated")
    if "remastered" in f_norm: cuts.append("Remastered")
    if "dual audio" in f_norm or "hindi-english" in f_norm or "multi" in f_norm: cuts.append("Dual Audio")
    if "criterion" in f_norm: cuts.append("Criterion")
    return " | ".join(cuts) if cuts else ""

def extract_clean_title_and_year(raw_name):
    base = os.path.splitext(raw_name)[0]
    base = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", base, flags=re.I)
    base = re.sub(r"^\d{1,3}\s*[\.\-]+\s*(?!\d*x\d+)", "", base, flags=re.I)
    base = re.sub(r"\[.*?\]", " ", base)

    explicit_year = None
    ym = re.search(r"(?:[\(\[\.\s\-_]|^)(19\d\d|20\d\d)(?:[\)\]\.\s\-_]|$)", base)
    if ym and not any(k in base.lower() for k in ["blade runner 2049", "2012", "1984"]):
        explicit_year = int(ym.group(1))
        base = base[:ym.start()].strip(" -_.")

    base = re.sub(r"[-_.]+", " ", base)
    base = re.sub(r"\b(open\s*matte|openmatte|imax|web\s*dl|webrip|hmax|hdtvrip|hdtv|bluray|dvdrip|dsnp|ds4k|1080p|720p|480p|2160p|4k|[hx]\.?26[45]|hevc|10bit|ivi|atmos|ddp5?\.?1?|hindi\s*english|dual\s*audio|aac5?\.?1?|ac3|dts|remux|repack|proper|org\s*bd|org\s*ddp|msubs|esubs|tombdoc|frds|garshasp|yts|team\s*ddh~rg|team\s*ddh|xdmovies(?:\.com)?)\b.*", "", base, flags=re.I)
    return re.sub(r"\s+", " ", base).strip(" ~-._"), explicit_year

def extract_episode_meta(fname):
    clean_f = re.sub(r"^@[\w\.\-]+(?:\s*-\s*|\s+)", "", fname, flags=re.I)
    clean_f = re.sub(r"^\d{1,3}\s*[\.\-]+\s*(?!\d*x\d+)", "", clean_f, flags=re.I)
    f_lower = clean_f.lower()

    if any(tag in f_lower for tag in ["extra", "promo", "interview", "featurette", "bonus", "deleted", "bloopers"]):
        anchor = re.split(r"[-_]\s*(?:extra|promo|interview|featurette|bonus|deleted|bloopers)\b", clean_f, flags=re.I)[0]
        anchor, _ = extract_clean_title_and_year(anchor)
        return {"is_tv": True, "season": 0, "episodes": [1], "part_tag": "Special / Extra", "anchor": anchor}

    se_match = re.search(r"\b[sS](\d{1,2})\s*[-_ ]?\s*[eE](\d{1,3})(?:\s*[-_ ]*?(?:[eE]|ep)?\s*(\d{1,3}))?([a-zA-Z])?\b", clean_f)
    if se_match:
        s = int(se_match.group(1))
        e1 = int(se_match.group(2))
        e2 = int(se_match.group(3)) if se_match.group(3) else e1
        part_char = se_match.group(4)
        part = f"Part {part_char.upper()}" if (part_char and part_char.lower() not in ['p', 'k']) else ""
        anchor, _ = extract_clean_title_and_year(clean_f[:se_match.start()])
        return {"is_tv": True, "season": s, "episodes": list(range(e1, e2 + 1)), "part_tag": part, "anchor": anchor}

    x_match = re.search(r"\b(\d{1,2})[xX](\d{1,3})(?:-(\d{1,3}))?([a-zA-Z])?\b", clean_f)
    if x_match:
        s = int(x_match.group(1))
        e1 = int(x_match.group(2))
        e2 = int(x_match.group(3)) if x_match.group(3) else e1
        part_char = x_match.group(4)
        part = f"Part {part_char.upper()}" if (part_char and part_char.lower() not in ['p', 'k']) else ""
        anchor, _ = extract_clean_title_and_year(clean_f[:x_match.start()])
        return {"is_tv": True, "season": s, "episodes": list(range(e1, e2 + 1)), "part_tag": part, "anchor": anchor}

    sp_match = re.search(r"\b(?:[sS]|Season\s*)(\d{1,2})\b(?!\s*[eE]\d+)", clean_f, re.I)
    if sp_match:
        anchor, _ = extract_clean_title_and_year(clean_f[:sp_match.start()])
        return {"is_tv": True, "season": int(sp_match.group(1)), "episodes": [1], "part_tag": "Season Pack", "anchor": anchor}

    return {"is_tv": False, "season": 1, "episodes": [1], "part_tag": "", "anchor": ""}

def get_franchise_parent(folder_path, raw_name, explicit_year):
    clean_lower, _ = extract_clean_title_and_year(raw_name)
    clean_lower = clean_lower.lower()
    if any(film in clean_lower for film in KNOWN_FEATURE_FILMS): return None
    if "tom and jerry" in clean_lower and explicit_year == 2021: return None

    path_str = " ".join(folder_path).lower()
    for k, v in CANONICAL_CARTOON_FRANCHISES.items():
        if k in path_str:
            return v
    return None

# ==========================================
# ASYNC GOFILE CRAWLER
# ==========================================

async def fetch_folder_contents(session, folder_code, sem):
    async with sem:
        page = 1
        items = []
        while True:
            url = f"https://api.gofile.io/contents/{folder_code}?page={page}&pageSize=50"
            try:
                async with session.get(url, timeout=15) as res:
                    data = await res.json()
                    if data.get("status") != "ok":
                        break
                    children = data.get("data", {}).get("children", {})
                    if not children:
                        break
                    c_list = children.values() if isinstance(children, dict) else children
                    items.extend(c_list)
                    if len(c_list) < 50:
                        break
                    page += 1
            except Exception:
                break
        return items

async def async_crawl_tree(session, root_id):
    print("🚀 Starting fast async tree crawl...")
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
    all_files = {}
    folders_to_scan = [(root_id, "Root", ["Root"])]
    visited = set()

    while folders_to_scan:
        current_batch = folders_to_scan[:]
        folders_to_scan.clear()

        tasks = [fetch_folder_contents(session, f_id, sem) for f_id, _, _ in current_batch]
        results = await asyncio.gather(*tasks)

        for (f_id, f_name, f_path), children in zip(current_batch, results):
            visited.add(f_id)
            for c in children:
                c_id = c.get("id") or c.get("file_id")
                if not c_id:
                    continue
                if c.get("type") == "folder":
                    sub_code = c.get("code") or c.get("id") or c_id
                    sub_name = c.get("name", sub_code)
                    if sub_code not in visited:
                        folders_to_scan.append((sub_code, sub_name, f_path + [sub_name]))
                else:
                    fname = c.get("name", "")
                    if is_video_file(fname):
                        direct_link = extract_direct_stream_link(c, c_id)
                        if direct_link:
                            c["_resolved_link"] = direct_link
                            c["_parent_folder"] = f_name
                            c["_folder_path"] = f_path
                            all_files[c_id] = c

        print(f"   ↳ Scanned {len(current_batch)} folders, cumulative files found: {len(all_files)}")

    return all_files

# ==========================================
# ASYNC IMDB & CINEMETA LOOKUPS
# ==========================================

async def async_search_imdb(session, query, year=None, force_type=None, sem=None):
    if not query or len(query.strip()) < 1:
        return None

    clean_q = query.strip()
    is_non_latin = any(ord(c) > 127 for c in clean_q)

    if is_non_latin:
        cat = "series" if force_type == "tv" else "movie"
        url = f"https://v3-cinemeta.strem.io/catalog/{cat}/top/search={quote(clean_q)}.json"
        try:
            async with session.get(url, timeout=5) as r:
                res = await r.json()
                metas = res.get("metas", [])
                for m in metas:
                    m_year = m.get("year") or m.get("releaseInfo")
                    if year and m_year and abs(int(str(m_year)[:4]) - int(year)) <= 1:
                        return {"type": cat, "imdb_id": m.get("imdb_id") or m.get("id"), "title": m.get("name"), "poster": m.get("poster")}
                if metas:
                    return {"type": cat, "imdb_id": metas[0].get("imdb_id") or metas[0].get("id"), "title": metas[0].get("name"), "poster": metas[0].get("poster")}
        except Exception:
            pass

    encoded_q = quote(clean_q.lower().replace(" ", "_"))
    url = f"https://v3.sg.media-imdb.com/suggestion/x/{encoded_q}.json"

    try:
        async with session.get(url, timeout=5) as r:
            res = await r.json()
            items = res.get("d", [])
            clean_target = clean_q.lower().strip()
            candidates = []

            for item in items:
                imdb_id = item.get("id", "")
                if not imdb_id.startswith("tt"):
                    continue

                q_type = item.get("q")
                item_year = item.get("y")
                title_lower = (item.get("l") or "").lower().strip()

                if force_type == "tv" and q_type not in ["TV series", "TV mini-series", "TV special"]: continue
                if force_type == "movie" and q_type in ["TV series", "TV mini-series", "TV episode"]: continue
                if year and (not item_year or abs(int(item_year) - int(year)) > 1): continue

                if title_lower == clean_target: sim = 1.0
                elif clean_target in title_lower: sim = 0.85
                else: sim = SequenceMatcher(None, clean_target, title_lower).ratio()
                candidates.append((sim, item))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                best_sim, best_item = candidates[0]
                if best_sim >= 0.65:
                    q_type = best_item.get("q")
                    img_info = best_item.get("i", {})
                    return {
                        "type": "series" if q_type in ["TV series", "TV mini-series"] else "movie",
                        "imdb_id": best_item.get("id"),
                        "title": best_item.get("l", clean_q),
                        "poster": img_info.get("imageUrl", "") if isinstance(img_info, dict) else ""
                    }
    except Exception:
        pass

    cat = "series" if force_type == "tv" else "movie"
    url = f"https://v3-cinemeta.strem.io/catalog/{cat}/top/search={quote(clean_q)}.json"
    try:
        async with session.get(url, timeout=5) as r:
            res = await r.json()
            metas = res.get("metas", [])
            for m in metas:
                m_year = m.get("year") or m.get("releaseInfo")
                if year and m_year and abs(int(str(m_year)[:4]) - int(year)) <= 1:
                    return {"type": cat, "imdb_id": m.get("imdb_id") or m.get("id"), "title": m.get("name"), "poster": m.get("poster")}
            if metas and not year:
                return {"type": cat, "imdb_id": metas[0].get("imdb_id") or metas[0].get("id"), "title": metas[0].get("name"), "poster": metas[0].get("poster")}
    except Exception:
        pass

    return None

# ==========================================
# STREAM ROW BUILDER
# ==========================================

def make_stream_entries(fid, item, m_type, imdb_id, title, poster, season=1, episodes=[1], version_tag="", quality="1080P"):
    fname = item.get("name", fid)
    link = item.get("_resolved_link") or extract_direct_stream_link(item, fid)
    size = item.get("size", 0)
    size_mb = f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"

    details = [quality]
    if version_tag: details.append(version_tag)
    details.append(size_mb)
    stream_desc = " | ".join(details)

    entries = []
    if m_type == "series":
        all_stream_ids = [f"{imdb_id}:{season}:{ep}" for ep in episodes]
        for ep in episodes:
            unique_fid = f"{fid}_e{ep}" if len(episodes) > 1 else fid
            key_id = f"{fid}_S{season:02d}E{ep:02d}"
            entries.append((key_id, {
                "file_id": unique_fid,
                "real_file_id": fid,
                "type": "series",
                "imdb_id": imdb_id,
                "title": title,
                "name": fname,
                "season": season,
                "episode": ep,
                "stream_id": f"{imdb_id}:{season}:{ep}",
                "stream_ids": all_stream_ids,
                "poster": poster or "https://gofile.io/dist/img/logo-small.png",
                "edition": version_tag,
                "quality": quality,
                "description": stream_desc,
                "size": size_mb,
                "link": link
            }))
    else:
        entries.append((fid, {
            "file_id": fid,
            "real_file_id": fid,
            "type": "movie",
            "imdb_id": imdb_id,
            "title": title,
            "name": fname,
            "stream_id": imdb_id,
            "stream_ids": [imdb_id],
            "poster": poster or "https://gofile.io/dist/img/logo-small.png",
            "edition": version_tag,
            "quality": quality,
            "description": stream_desc,
            "size": size_mb,
            "link": link
        }))
    return entries

# ==========================================
# MAIN ASYNC PIPELINE
# ==========================================

async def main_async():
    start_time = time.time()
    existing_catalog = {}
    raw_existing = load_json(DATA_FILE)
    if isinstance(raw_existing, list):
        for row in raw_existing:
            fid = row.get("file_id")
            if fid:
                existing_catalog[fid] = row

    knowledge_base = load_json(KNOWLEDGE_FILE)
    print(f"📦 Loaded {len(existing_catalog)} cached files | 🧠 {len(knowledge_base)} verified IMDb matches")

    browser_headers = await get_browser_session_headers(ROOT_URL)

    conn = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT, ssl=False)
    async with aiohttp.ClientSession(headers=browser_headers, connector=conn) as session:
        all_live_files = await async_crawl_tree(session, ROOT_FOLDER_ID)

        if not all_live_files:
            print("❌ 0 files retrieved. Halting.")
            return

        final_catalog = {}
        missing_ids = []

        for fid, item in all_live_files.items():
            if fid in existing_catalog:
                cached = existing_catalog[fid]
                imdb_id = cached.get("imdb_id", "")
                raw_name = item.get("name", "")

                is_corrupt = (
                    imdb_id.startswith("gf:") or
                    ("Baaghi" in raw_name and "1990" in raw_name and imdb_id == "tt4864932") or
                    imdb_id == "tt37522729"
                )

                if not is_corrupt:
                    cached["link"] = item.get("_resolved_link")
                    final_catalog[fid] = cached
                    continue
            missing_ids.append(fid)

        print(f"📌 Fast-reused {len(final_catalog)} entries | Resolving {len(missing_ids)} new/updated items...")

        short_seq_counter = {}

        async def resolve_item(fid):
            item = all_live_files[fid]
            raw_name = item.get("name", "")
            folder_path = item.get("_folder_path", ["Root"])
            parent_folder = item.get("_parent_folder", "Root")

            parsed = PTN.parse(raw_name)
            cleaned_title, explicit_year = extract_clean_title_and_year(raw_name)
            if not explicit_year: explicit_year = parsed.get("year")

            version_cut_tag = extract_versions_and_cuts(raw_name)
            ep_meta = extract_episode_meta(raw_name)
            quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

            if ep_meta.get("part_tag"):
                version_cut_tag = f"{version_cut_tag} | {ep_meta['part_tag']}".strip(" |")

            # 1. Franchise Short
            franchise = get_franchise_parent(folder_path, raw_name, explicit_year)
            if franchise:
                f_imdb = franchise["imdb_id"]
                short_seq_counter.setdefault(f_imdb, 1)
                seq_num = short_seq_counter[f_imdb]
                short_seq_counter[f_imdb] += 1
                combined_tag = f"{version_cut_tag} | Short: {cleaned_title}".strip(" |")
                return make_stream_entries(fid, item, "series", f_imdb, franchise["title"], franchise["poster"],
                                           season=1, episodes=[seq_num], version_tag=combined_tag, quality=str(quality))

            # 2. TV Show
            if ep_meta["is_tv"]:
                show_query = ep_meta.get("anchor")
                if not show_query or len(show_query.strip()) < 2:
                    for folder in reversed(folder_path):
                        f_clean, _ = extract_clean_title_and_year(folder)
                        if f_clean.lower() not in GENERIC_FOLDERS and not f_clean.lower().startswith("season"):
                            show_query = f_clean
                            break
                if not show_query: show_query = cleaned_title
                show_query = re.sub(r"\b(?:[sS]|Season\s*)\d{1,2}.*", "", show_query, flags=re.I).strip()

                cache_key = f"imdb_tv:{show_query.lower()}"
                match = knowledge_base.get(cache_key)
                if not match:
                    match = await async_search_imdb(session, show_query, force_type="tv")
                    if match and match.get("type") == "series":
                        knowledge_base[cache_key] = match

                if match and match.get("type") == "series":
                    return make_stream_entries(fid, item, "series", match["imdb_id"], match["title"], match["poster"],
                                               season=ep_meta["season"], episodes=ep_meta["episodes"], version_tag=version_cut_tag, quality=str(quality))

            # 3. Movie
            movie_queries = []
            if cleaned_title.lower() in KNOWN_TITLE_ALIASES:
                movie_queries.extend(KNOWN_TITLE_ALIASES[cleaned_title.lower()])
            for cand in re.split(r"\s*[-/|]\s*", cleaned_title):
                if cand.strip() and cand.strip() not in movie_queries:
                    movie_queries.append(cand.strip())

            cache_key = f"imdb_movie:{movie_queries[0].lower()}:{explicit_year or ''}"
            match = knowledge_base.get(cache_key)
            if not match:
                for q in movie_queries:
                    match = await async_search_imdb(session, q, year=explicit_year, force_type="movie")
                    if match:
                        break
                if match:
                    knowledge_base[cache_key] = match

            if match:
                return make_stream_entries(fid, item, "movie", match["imdb_id"], match["title"], match["poster"],
                                           version_tag=version_cut_tag, quality=str(quality))
            else:
                return make_stream_entries(fid, item, "movie", f"gf:{fid}", cleaned_title, "",
                                           version_tag=version_cut_tag, quality=str(quality))

        resolve_tasks = [resolve_item(fid) for fid in missing_ids]
        batch_results = await asyncio.gather(*resolve_tasks)

        for entries in batch_results:
            if entries:
                for key_id, record in entries:
                    final_catalog[key_id] = record

    save_json(KNOWLEDGE_FILE, knowledge_base)
    output_list = list(final_catalog.values())
    save_json(DATA_FILE, output_list)

    elapsed = time.time() - start_time
    print(f"\n⚡ Total scan & catalog sync completed in {elapsed:.2f}s! Indexed: {len(output_list)} records.")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
