"""Publishes due Instagram posts listed in queue.json.

Runs on GitHub Actions every 15 minutes. Each queue item:
  {"id", "page_id", "ig_id", "time" (unix), "caption", "images": [public urls],
   "paths": [repo paths of the images], "status": "pending"}
The access token comes from the repository secret IG_TOKEN_<page_id>.
"""
import json
import os
import sys
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


def already_posted(item, token):
    """Safety net: if one of the account's last 25 posts has the same caption,
    return its id instead of publishing it again."""
    cap = (item.get("caption") or "").strip()
    if not cap:
        return None
    r = call("GET", f"{item['ig_id']}/media", {"fields": "id,caption,timestamp", "limit": "25", "access_token": token})
    for m in r.get("data", []):
        if (m.get("caption") or "").strip() == cap:
            return m["id"]
    return None


def publish(item, token):
    kind = item.get("kind", "ig_post")
    if kind == "ig_story":
        return publish_ig_story(item, token)
    if kind == "fb_story":
        return publish_fb_story(item, token)
    return publish_ig_post(item, token)


def publish_ig_story(item, token):
    """Each image becomes one story frame. done_count lets a retry skip frames already up."""
    ig = item["ig_id"]
    last = item.get("ig_media_id")
    for i, u in enumerate(item["images"][:10]):
        if i < item.get("done_count", 0):
            continue
        c = call("POST", f"{ig}/media", {"image_url": u, "media_type": "STORIES", "access_token": token})["id"]
        wait_ready(c, token)
        last = call("POST", f"{ig}/media_publish", {"creation_id": c, "access_token": token}).get("id")
        item["done_count"] = i + 1
        item["ig_media_id"] = last
    return last


def publish_fb_story(item, token):
    page = item["page_id"]
    last = item.get("ig_media_id")
    for i, u in enumerate(item["images"][:10]):
        if i < item.get("done_count", 0):
            continue
        photo = call("POST", f"{page}/photos", {"url": u, "published": "false", "access_token": token})["id"]
        r = call("POST", f"{page}/photo_stories", {"photo_id": photo, "access_token": token})
        last = r.get("post_id") or r.get("id") or photo
        item["done_count"] = i + 1
        item["ig_media_id"] = last
    return last


def remove_unused_images(q):
    """Delete image files of finished items, unless a still-pending item uses the same files."""
    keep = set()
    for it in q.get("posts", []):
        if it and it.get("status") == "pending":
            keep.update(it.get("paths", []))
    for it in q.get("posts", []):
        if it and it.get("status") == "published":
            for p in it.get("paths", []):
                if p not in keep and os.path.exists(p):
                    os.remove(p)


def publish_ig_post(item, token):
    ig = item["ig_id"]
    dup = already_posted(item, token)
    if dup:
        print(item["id"], "already on Instagram, not posting again:", dup)
        return dup
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


def check_tokens():
    for name, tok in sorted(secrets.items()):
        try:
            me = call("GET", "me", {"fields": "id,name", "access_token": tok})
            print(f"{name}: OK ({me.get('name')})")
        except Exception as e:
            print(f"{name}: NOT WORKING - {e}")


RESULT_KEYS = ("status", "ig_media_id", "published_at", "error", "attempts", "done_count")


def merge(mine_path):
    """Copy this run's results onto the latest queue.json (which the app may have
    changed meanwhile: new posts added, posts cancelled)."""
    with open(mine_path, encoding="utf-8") as f:
        mine = {p["id"]: p for p in json.load(f).get("posts", []) if p}
    with open(QUEUE, encoding="utf-8") as f:
        q = json.load(f)
    for item in q.get("posts", []):
        m = mine.get(item.get("id"))
        if not m:
            continue
        for k in RESULT_KEYS:
            if k in m:
                item[k] = m[k]
            else:
                item.pop(k, None)
    remove_unused_images(q)
    with open(QUEUE, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=1)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "merge":
        merge(sys.argv[2])
        return
    check_tokens()
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
            print(item["id"], item.get("kind", "ig_post"), "published", media_id)
        except Exception as e:
            item["attempts"] = item.get("attempts", 0) + 1
            item["error"] = str(e)[:500]
            if item["attempts"] >= MAX_ATTEMPTS:
                item["status"] = "failed"
            print(item["id"], "error:", item["error"])
    if changed:
        remove_unused_images(q)
        with open(QUEUE, "w", encoding="utf-8") as f:
            json.dump(q, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
