import json
import os
import requests

FOLDER_ID = "OBVVp1LI"  # Your Gofile root folder code
TOKEN = os.environ.get("GOFILE_API_TOKEN", "")


def crawl():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        ),
        "Referer": "https://gofile.io/",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    url = f"https://api.gofile.io/contents/{FOLDER_ID}?wt=4fd6sg89d7s6"
    print(f"Fetching contents for root folder: {FOLDER_ID}")

    res = requests.get(url, headers=headers, timeout=20)
    data = res.json()

    if data.get("status") != "ok":
        print(f"Error from Gofile: {data}")
        return

    children = data.get("data", {}).get("children", {})
    files = []

    for item_id, item in children.items():
        if item.get("type") == "file":
            # Extract direct storage node link
            link = (
                item.get("link")
                or item.get("directDownload")
                or item.get("downloadPage")
            )
            size = item.get("size", 0)
            size_mb = (
                f"{(size / (1024 * 1024)):.2f} MB" if size else "Unknown size"
            )

            files.append(
                {
                    "id": item_id,
                    "name": item.get("name", item_id),
                    "link": link,
                    "size": size_mb,
                }
            )

    print(f"Found {len(files)} playable files.")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(files, f, indent=2)


if __name__ == "__main__":
    crawl()
