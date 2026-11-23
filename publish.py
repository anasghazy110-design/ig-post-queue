"""Publishes due Instagram posts listed in queue.json.

Runs on GitHub Actions every 15 minutes. Each queue item:
  {"id", "page_id", "ig_id", "time" (unix), "caption", "images": [public urls],
   "paths": [repo paths of the images], "status": "pending"}
The access token comes from the repository secret IG_TOKEN_<page_id>.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

VER = "v27.0"
QUEUE = "queue.json"
MAX_ATTEMPTS = 3

secrets = {k: v for k, v in os.environ.items() if k.startswith("IG_TOKEN_") and v}


def call(method, path, params):
    data = urllib.parse.urlencode(params)
    url = f"https://graph.facebook.com/{VER}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data)
    else:
        req = urllib.request.Request(url, data=data.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body
        raise RuntimeError(msg)


def wait_ready(container_id, token):
    for _ in range(36):
        s = call("GET", container_id, {"fields": "status_code,status", "access_token": token})
        code = s.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError("Instagram could not process the image: " + str(s.get("status", code)))
        time.sleep(5)
    raise RuntimeError("Instagram took too long to process the image")


def publish(item, token):
    ig = item["ig_id"]
    urls = item["images"]
    if len(urls) == 1:
        container = call("POST", f"{ig}/media", {
            "image_url": urls[0], "caption": item["caption"], "access_token": token})["id"]
    else:
        children = []
        for u in urls[:10]:
            children.append(call("POST", f"{ig}/media", {
                "image_url": u, "is_carousel_item": "true", "access_token": token})["id"])
        for c in children:
            wait_ready(c, token)
        container = call("POST", f"{ig}/media", {
            "media_type": "CAROUSEL", "children": ",".join(children),
            "caption": item["caption"], "access_token": token})["id"]
    wait_ready(container, token)
    return call("POST", f"{ig}/media_publish", {"creation_id": container, "access_token": token}).get("id")


def main():
    if not os.path.exists(QUEUE):
        print("queue.json not found, nothing to do")
        return
    with open(QUEUE, encoding="utf-8") as f:
        q = json.load(f)
    now = time.time()
    changed = False
    for item in q.get("posts", []):
        if item.get("status") != "pending" or item.get("time", 0) > now:
            continue
        changed = True
        token = secrets.get("IG_TOKEN_" + str(item["page_id"]))
        if not token:
            item["status"] = "failed"
            item["error"] = "Missing repository secret IG_TOKEN_" + str(item["page_id"])
            print(item["id"], item["error"])
            continue
        try:
            media_id = publish(item, token)
            item["status"] = "published"
            item["ig_media_id"] = media_id
            item["published_at"] = int(time.time())
            item.pop("error", None)
            print(item["id"], "published", media_id)
            for p in item.get("paths", []):
                if os.path.exists(p):
                    os.remove(p)
        except Exception as e:
            item["attempts"] = item.get("attempts", 0) + 1
            item["error"] = str(e)[:500]
            if item["attempts"] >= MAX_ATTEMPTS:
                item["status"] = "failed"
            print(item["id"], "error:", item["error"])
    if changed:
        with open(QUEUE, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
