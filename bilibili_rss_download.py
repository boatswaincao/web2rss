import os
import html
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from feedgen.feed import FeedGenerator

UID = "230105574"
API_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
FEED_URL = f"https://space.bilibili.com/{UID}/dynamic"
OUTPUT_FILE = f"rss/bilibili_{UID}.xml"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def build_cookie():
    """Read credentials from GitHub Actions Secrets; never print their values."""
    names = {
        "SESSDATA": os.getenv("BILI_SESSDATA", "").strip(),
        "bili_jct": os.getenv("BILI_JCT", "").strip(),
        "buvid3": os.getenv("BILI_BUVID3", "").strip(),
    }
    if not names["SESSDATA"]:
        raise RuntimeError("Missing GitHub Secret: BILI_SESSDATA")

    return "; ".join(f"{key}={value}" for key, value in names.items() if value)


def fetch_dynamics(cookie, max_pages=3):
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": FEED_URL,
        "Origin": "https://space.bilibili.com",
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
    }

    params = {
        "host_mid": UID,
        "timezone_offset": "-480",
        "platform": "web",
        "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
    }

    session = requests.Session()
    session.headers.update(headers)
    all_items = []
    offset = ""

    for page in range(max_pages):
        if offset:
            params["offset"] = offset
        else:
            params.pop("offset", None)

        response = session.get(API_URL, params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()

        code = payload.get("code", -1)
        if code != 0:
            message = payload.get("message", "unknown error")
            if code == -352:
                raise RuntimeError(
                    "Bilibili returned -352 (risk-control verification). "
                    "The request was blocked; this is not necessarily a Secret error."
                )
            if code in (-101, -111):
                raise RuntimeError(
                    f"Bilibili login/session error ({code}): {message}. "
                    "The SESSDATA Secret may have expired."
                )
            raise RuntimeError(f"Bilibili API error {code}: {message}")

        data = payload.get("data") or {}
        items = data.get("items") or []
        all_items.extend(items)

        print(f"Bilibili page {page + 1}: received {len(items)} dynamics")

        if not data.get("has_more") or not data.get("offset"):
            break
        offset = data["offset"]

    return all_items


def get_text(modules):
    dynamic = (modules or {}).get("module_dynamic") or {}
    desc = dynamic.get("desc") or {}
    text = desc.get("text") or ""
    major = dynamic.get("major") or {}

    if not text:
        opus = major.get("opus") or {}
        text = ((opus.get("summary") or {}).get("text")) or ""

    if not text:
        # Some content types carry their title/description under the type-specific key.
        major_type = (major.get("type") or "").replace("MAJOR_TYPE_", "").lower()
        typed = major.get(major_type) or {}
        text = typed.get("title") or typed.get("desc") or ""

    return text.strip()


def get_title(modules, fallback_text):
    dynamic = (modules or {}).get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    major_type = (major.get("type") or "").replace("MAJOR_TYPE_", "").lower()

    if major_type == "live_rcmd":
        try:
            live_info = json.loads((major.get("live_rcmd") or {}).get("content") or "{}")
            title = (live_info.get("live_play_info") or {}).get("title")
            if title:
                return title
        except (ValueError, TypeError):
            pass

    typed = major.get(major_type) or {}
    title = typed.get("title") or typed.get("sub_title")
    if title:
        return str(title).strip()

    if fallback_text:
        return fallback_text[:80]
    return "Bilibili 动态"


def collect_images(modules):
    dynamic = (modules or {}).get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    images = []

    opus = major.get("opus") or {}
    for pic in opus.get("pics") or []:
        url = pic.get("url")
        if url:
            images.append(url)

    for pic in (major.get("draw") or {}).get("items") or []:
        url = pic.get("src")
        if url:
            images.append(url)

    for url in (major.get("article") or {}).get("covers") or []:
        if url:
            images.append(url)

    archive = major.get("archive") or {}
    if archive.get("cover"):
        images.append(archive["cover"])

    if major.get("live_rcmd", {}).get("content"):
        try:
            live_info = json.loads(major["live_rcmd"]["content"])
            cover = (live_info.get("live_play_info") or {}).get("cover")
            if cover:
                images.append(cover)
        except (ValueError, TypeError):
            pass

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(urljoin("https:", u) for u in images))


def dynamic_link(item):
    dynamic_id = item.get("id_str") or item.get("id")
    if dynamic_id:
        return f"https://t.bilibili.com/{dynamic_id}"

    modules = item.get("modules") or {}
    major = (modules.get("module_dynamic") or {}).get("major") or {}
    archive = major.get("archive") or {}
    if archive.get("bvid"):
        return f"https://www.bilibili.com/video/{archive['bvid']}"
    return FEED_URL


def render_description(item):
    modules = item.get("modules") or {}
    text = get_text(modules)
    parts = []

    if text:
        parts.append(f"<p>{html.escape(text).replace(chr(10), '<br>')}</p>")

    images = collect_images(modules)
    for image_url in images:
        safe_url = html.escape(image_url, quote=True)
        parts.append(
            f'<p><a href="{safe_url}"><img src="{safe_url}" '
            f'style="max-width:100%;height:auto"></a></p>'
        )

    # Include the original post when this dynamic is a repost/forward.
    original = item.get("orig")
    if original:
        original_modules = original.get("modules") or {}
        original_author = (
            (original_modules.get("module_author") or {}).get("name") or "原动态作者"
        )
        original_text = get_text(original_modules)
        original_link = dynamic_link(original)
        parts.append(
            "<hr><p><strong>转发内容</strong></p>"
            f"<p>{html.escape(original_author)}</p>"
        )
        if original_text:
            parts.append(
                f"<p>{html.escape(original_text).replace(chr(10), '<br>')}</p>"
            )
        parts.append(
            f'<p><a href="{html.escape(original_link, quote=True)}">查看原动态</a></p>'
        )
        for image_url in collect_images(original_modules):
            safe_url = html.escape(image_url, quote=True)
            parts.append(
                f'<p><a href="{safe_url}"><img src="{safe_url}" '
                f'style="max-width:100%;height:auto"></a></p>'
            )

    parts.append(
        f'<p><a href="{html.escape(dynamic_link(item), quote=True)}">'
        "在哔哩哔哩查看动态</a></p>"
    )
    return "\n".join(parts)


def generate_feed(items):
    fg = FeedGenerator()
    fg.id(FEED_URL)
    fg.title("Bilibili UP 主动态")
    fg.link(href=FEED_URL, rel="alternate")
    fg.description(f"UID {UID} 的全部动态（文字、图片、视频、转发等）")
    fg.language("zh-CN")

    count = 0
    seen = set()

    for item in items:
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        dynamic_id = str(item.get("id_str") or item.get("id") or "")
        if not dynamic_id or dynamic_id in seen:
            continue
        seen.add(dynamic_id)

        text = get_text(modules)
        title = get_title(modules, text)
        if item.get("orig"):
            title = f"[转发] {title}"

        entry = fg.add_entry()
        entry.id(f"https://t.bilibili.com/{dynamic_id}")
        entry.title(title)
        entry.link(href=dynamic_link(item))
        entry.description(render_description(item), type="html")

        timestamp = author.get("pub_ts")
        if timestamp:
            try:
                published = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
                entry.pubDate(published)
            except (ValueError, TypeError, OSError):
                pass

        if author.get("name"):
            entry.author({"name": author["name"]})

        count += 1

    if count == 0:
        raise RuntimeError(
            "The API returned no usable dynamics. The feed was not overwritten."
        )

    os.makedirs("rss", exist_ok=True)
    with open(OUTPUT_FILE, "wb") as output:
        output.write(fg.rss_str(pretty=True))

    print(f"Generated {count} dynamics -> {OUTPUT_FILE}")


def main():
    cookie = build_cookie()
    items = fetch_dynamics(cookie)
    generate_feed(items)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Bilibili RSS generation failed: {exc}", file=sys.stderr)
        raise
