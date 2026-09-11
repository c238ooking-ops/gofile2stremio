import os
import sys
import json
import time
import re
import asyncio
from difflib import SequenceMatcher
from urllib.parse import quote
import aiohttp
import requests
import PTN
from playwright.async_api import async_playwright

ROOT_FOLDER_ID = "OBVVp1LI"
ROOT_URL = f"https://gofile.io/d/{ROOT_FOLDER_ID}"
KNOWLEDGE_FILE = "knowledge.json"
DATA_FILE = "data.json"

WORKER_SYNC_URL = os.environ.get("WORKER_SYNC_URL", "https://gofile-stremio.c238ooking.workers.dev/sync")
CONCURRENCY_LIMIT = 8

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
    "space jam", "space jam a new legacy", "looney tunes back in action",
    "a goofy movie", "an extremely goofy movie", "who framed roger rabbit",
    "tom and jerry the movie", "the movie"
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

def is_video_file(filename):
    if not filename or "." not in filename:
        return False
    return os.path.splitext(filename)[1].lower() in VALID_VIDEO_EXTENSIONS

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

async def crawl_gofile_tree(root_id):
    print("⚡ Launching Playwright session to traverse Gofile folders...")
    auth = {"headers": {}, "wt": ""}
    root_cached_data = {}
    init_event = asyncio.Event()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--single-process"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US"
        )
        page = await context.new_page()

        async def on_response(res):
            if f"/contents/{root_id}" in res.url:
                try:
                    data = await res.json()
                    if data.get("status") == "ok":
                        root_cached_data.update(data.get("data", {}))
                        req_h = res.request.headers
                        auth["headers"] = {
                            "Accept": "application/json, text/plain, */*",
                            "X-BL": req_h.get("x-bl", "en-US"),
                            "X-Website-Token": req_h.get("x-website-token", ""),
                            "Authorization": req_h.get("authorization", "")
                        }
                        wt_m = re.search(r"wt=([^&]+)", res.url)
                        auth["wt"] = wt_m.group(1) if wt_m else req_h.get("x-website-token", "")
                        init_event.set()
                except Exception:
                    pass

        page.on("response", on_response)

        print(f"🌐 Loading root folder {root_id}...")
        await page.goto(ROOT_URL, wait_until="commit", timeout=35000)

        try:
            await asyncio.wait_for(init_event.wait(), timeout=12.0)
            print("🎯 Live session authenticated successfully.")
        except Exception:
            print("❌ Root handshake timeout.")
            await browser.close()
            return {}

        headers_json = json.dumps(auth["headers"])
        initial_root_json = json.dumps(root_cached_data)

        print("🚀 Executing sliding-window pipelined traversal inside browser...")
        all_raw_files = await page.evaluate(f"""
            async () => {{
                const rootId = '{root_id}';
                const headers = {headers_json};
                const wt = '{auth["wt"]}';
                const initialData = {initial_root_json};

                const queue = [{{ id: rootId, name: 'Root', path: ['Root'] }}];
                const visited = new Set();
                const queued = new Set([rootId]);
                const collectedFiles = [];

                const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

                const fetchFolderWithPaging = async (folder) => {{
                    let children = [];
                    if (folder.id === rootId && initialData && initialData.children) {{
                        const rawC = initialData.children;
                        return Array.isArray(rawC) ? rawC : Object.values(rawC);
                    }}

                    let pageNum = 1;
                    while (true) {{
                        let json = null;
                        for (let attempt = 1; attempt <= 3; attempt++) {{
                            try {{
                                const url = 'https://api.gofile.io/contents/' + folder.id + '?page=' + pageNum + '&pageSize=100&sortField=name&sortDirection=1&wt=' + wt;
                                const r = await fetch(url, {{ headers }});
                                const res = await r.json();
                                if (res && res.status === 'ok') {{
                                    json = res.data || {{}};
                                    break;
                                }} else if (res && (res.status === 'error-rateLimit' || res.status === '429')) {{
                                    await sleep(attempt * 800);
                                }} else {{
                                    await sleep(100);
                                }}
                            }} catch (e) {{
                                await sleep(150);
                            }}
                        }}

                        if (!json) break;
                        const rawC = json.children || {{}};
                        const pageItems = Array.isArray(rawC) ? rawC : Object.values(rawC);
                        if (pageItems.length === 0) break;

                        children.push(...pageItems);
                        const total = json.totalChildrenCount || children.length;
                        if (children.length >= total || pageItems.length < 100) break;

                        pageNum++;
                        await sleep(80);
                    }}
                    return children;
                }};

                // Controlled sliding window worker pool (concurrency = 3)
                const CONCURRENCY = 3;
                let activeCount = 0;

                await new Promise((resolve) => {{
                    const checkDone = () => {{
                        if (queue.length === 0 && activeCount === 0) {{
                            resolve();
                        }}
                    }};

                    const pump = () => {{
                        while (activeCount < CONCURRENCY && queue.length > 0) {{
                            const current = queue.shift();
                            if (visited.has(current.id)) {{
                                checkDone();
                                continue;
                            }}
                            visited.add(current.id);
                            activeCount++;

                            (async () => {{
                                try {{
                                    const children = await fetchFolderWithPaging(current);
                                    for (const c of children) {{
                                        const cId = c.id || c.file_id;
                                        if (!cId) continue;

                                        if (c.type === 'folder') {{
                                            const subId = c.id || c.code || cId;
                                            const subName = c.name || subId;
                                            if (!queued.has(subId)) {{
                                                queued.add(subId);
                                                queue.push({{ id: subId, name: subName, path: [...current.path, subName] }});
                                            }}
                                        }} else {{
                                            collectedFiles.push({{
                                                item: c,
                                                fid: cId,
                                                parent_folder: current.name,
                                                folder_path: current.path
                                            }});
                                        }}
                                    }}
                                }} finally {{
                                    activeCount--;
                                    pump();
                                    checkDone();
                                }}
                            }})();
                        }}
                        checkDone();
                    }};

                    pump();
                }});

                return collectedFiles;
            }}
        """)

        await browser.close()

    all_files = {}
    for entry in all_raw_files:
        c = entry["item"]
        fid = entry["fid"]
        fname = c.get("name", "")
        if is_video_file(fname):
            direct_link = extract_direct_stream_link(c, fid)
            if direct_link:
                c["_resolved_link"] = direct_link
                c["_parent_folder"] = entry["parent_folder"]
                c["_folder_path"] = entry["folder_path"]
                all_files[fid] = c

    print(f"🎉 Traversal complete! Found {len(all_files)} physical video files across all folders.")
    return all_files

async def async_search_imdb(session, query, year=None, force_type=None):
    if not query or len(query.strip()) < 1: return None
    clean_q = query.strip()
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
                if not imdb_id.startswith("tt"): continue
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
    return None

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

async def main_async():
    start_time = time.time()
    
    raw_existing = load_json(DATA_FILE)
    existing_by_fid = {}
    if isinstance(raw_existing, list):
        for row in raw_existing:
            fid = row.get("file_id")
            rfid = row.get("real_file_id")
            if fid: existing_by_fid.setdefault(fid, []).append(row)
            if rfid and rfid != fid: existing_by_fid.setdefault(rfid, []).append(row)

    knowledge_base = load_json(KNOWLEDGE_FILE)
    print(f"📦 Loaded {len(raw_existing) if isinstance(raw_existing, list) else 0} catalog entries | 🧠 {len(knowledge_base)} verified matches")

    if isinstance(raw_existing, list):
        for row in raw_existing:
            imdb_id = row.get("imdb_id", "")
            if imdb_id and not imdb_id.startswith("gf:") and imdb_id != "tt37522729":
                raw_title = row.get("name") or row.get("title", "")
                cleaned_title, explicit_year = extract_clean_title_and_year(raw_title)
                cache_key = f"imdb_movie:{cleaned_title.lower()}:{explicit_year or ''}"
                if cache_key not in knowledge_base:
                    knowledge_base[cache_key] = {
                        "type": row.get("type", "movie"),
                        "imdb_id": imdb_id,
                        "title": row.get("title", cleaned_title),
                        "poster": row.get("poster", "")
                    }

    all_live_files = await crawl_gofile_tree(ROOT_FOLDER_ID)

    if not all_live_files:
        print("❌ 0 files retrieved. Verification failed.")
        sys.exit(1)

    final_catalog = {}
    missing_ids = []

    for fid, item in all_live_files.items():
        matched_cached_entries = existing_by_fid.get(fid, [])
        valid_reusable = [
            e for e in matched_cached_entries 
            if e.get("imdb_id") and not e.get("imdb_id", "").startswith("gf:") and e.get("imdb_id") != "tt37522729"
        ]

        if valid_reusable:
            for e in valid_reusable:
                k = e.get("file_id") or fid
                if e.get("type") == "series" and "season" in e and "episode" in e:
                    k = f"{fid}_S{e['season']:02d}E{e['episode']:02d}"
                e["link"] = item.get("_resolved_link")
                final_catalog[k] = e
            continue

        missing_ids.append(fid)

    print(f"📌 Fast-reused {len(final_catalog)} entries | Resolving {len(missing_ids)} items...")

    conn = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT, ssl=False)
    async with aiohttp.ClientSession(connector=conn) as session:
        short_seq_counter = {}

        async def resolve_item(fid):
            item = all_live_files[fid]
            raw_name = item.get("name", "")
            folder_path = item.get("_folder_path", ["Root"])

            parsed = PTN.parse(raw_name)
            cleaned_title, explicit_year = extract_clean_title_and_year(raw_name)
            if not explicit_year: explicit_year = parsed.get("year")

            version_cut_tag = extract_versions_and_cuts(raw_name)
            ep_meta = extract_episode_meta(raw_name)
            quality = parsed.get("resolution") or parsed.get("quality") or "1080P"

            if ep_meta.get("part_tag"):
                version_cut_tag = f"{version_cut_tag} | {ep_meta['part_tag']}".strip(" |")

            franchise = get_franchise_parent(folder_path, raw_name, explicit_year)
            if franchise:
                f_imdb = franchise["imdb_id"]
                short_seq_counter.setdefault(f_imdb, 1)
                seq_num = short_seq_counter[f_imdb]
                short_seq_counter[f_imdb] += 1
                combined_tag = f"{version_cut_tag} | Short: {cleaned_title}".strip(" |")
                return make_stream_entries(fid, item, "series", f_imdb, franchise["title"], franchise["poster"],
                                           season=1, episodes=[seq_num], version_tag=combined_tag, quality=str(quality))

            if ep_meta["is_tv"]:
                show_query = ep_meta.get("anchor") or cleaned_title
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

            cache_key = f"imdb_movie:{cleaned_title.lower()}:{explicit_year or ''}"
            match = knowledge_base.get(cache_key)
            if not match:
                match = await async_search_imdb(session, cleaned_title, year=explicit_year, force_type="movie")
                if match:
                    knowledge_base[cache_key] = match
                else:
                    knowledge_base[cache_key] = {
                        "type": "movie",
                        "imdb_id": f"gf:{fid}",
                        "title": cleaned_title,
                        "poster": ""
                    }
                    match = knowledge_base[cache_key]

            if match:
                return make_stream_entries(fid, item, "movie", match["imdb_id"], match["title"], match.get("poster", ""),
                                           version_tag=version_cut_tag, quality=str(quality))

        imdb_sem = asyncio.Semaphore(10)

        async def bounded_resolve(f_id):
            async with imdb_sem:
                return await resolve_item(f_id)

        resolve_tasks = [bounded_resolve(fid) for fid in missing_ids]
        batch_results = await asyncio.gather(*resolve_tasks)

        for entries in batch_results:
            if entries:
                for key_id, record in entries:
                    final_catalog[key_id] = record

    save_json(KNOWLEDGE_FILE, knowledge_base)
    output_list = list(final_catalog.values())
    save_json(DATA_FILE, output_list)

    elapsed = time.time() - start_time
    print(f"\n🎉 Catalog build complete! Total indexed: {len(output_list)} streams from {len(all_live_files)} files in {elapsed:.2f}s.")

    if WORKER_SYNC_URL:
        try:
            r = requests.post(WORKER_SYNC_URL, json=output_list, timeout=30)
            print(f"✅ Cloudflare KV Sync: {r.text}")
        except Exception as e:
            print(f"❌ Worker sync notice: {e}")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
