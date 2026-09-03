import json
import os
import sys
import requests

FOLDER_ID = "OBVVp1LI"
TOKEN = os.environ.get("GOFILE_API_TOKEN", "").strip()


def crawl():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://gofile.io/",
        "Origin": "https://gofile.io",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["Cookie"] = f"accountToken={TOKEN}"

    url = f"https://api.gofile.io/contents/{FOLDER_ID}?wt=4fd6sg89d7s6"
    print(f"Fetching: {url}")
    print(
        f"Token present: {'Yes (length ' + str(len(TOKEN)) + ')' if TOKEN else 'No'}"
    )

    files = []
    try:
        res = requests.get(url, headers=headers, timeout=30)
        print(f"HTTP Status: {res.status_code}")
        data = res.json()
        print(f"Gofile Response Status: {data.get('status')}")

        if data.get("status") == "ok":
            children = data.get("data", {}).get("children", {})
            for item_id, item in children.items():
                if item.get("type") == "file":
                    link = (
                        item.get("link")
                        or item.get("directDownload")
                        or item.get("downloadPage")
                    )
                    size = item.get("size", 0)
                    size_mb = (
                        f"{(size / (1024 * 1024)):.2f} MB"
                        if size
                        else "Unknown size"
                    )

                    files.append(
                        {
                            "id": item_id,
                            "name": item.get("name", item_id),
                            "link": link,
                            "size": size_mb,
                        }
                    )
            print(f"Parsed {len(files)} files successfully.")
        else:
            print(f"Gofile returned error details: {data}")
            # Do not exit cleanly so the runner knows the API failed
            sys.exit(1)

    except Exception as e:
        print(f"Crawler exception: {e}")
        sys.exit(1)
    finally:
        # Always make sure data.json exists so git add never throws a fatal error
        if not os.path.exists("data.json") and files:
            with open("data.json", "w", encoding="utf-8") as f:
                json.dump(files, f, indent=2)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2)


if __name__ == "__main__":
    crawl()
