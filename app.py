from __future__ import annotations

import csv
import html
import json
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
STATIC_DIR = ROOT / "static"
EXPORT_DIR = ROOT / "exports"
HOST = "127.0.0.1"
PORT = int(os.environ.get("NEWS_LINK_MONITOR_PORT", "8765"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)


def build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"]:
        return context
    for path in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if Path(path).exists():
            context.load_verify_locations(cafile=path)
            break
    return context


SSL_CONTEXT = build_ssl_context()

DEFAULT_SOURCES = [
    {
        "id": "naver-news",
        "name": "네이버 뉴스",
        "type": "naver",
        "url": "https://news.naver.com/",
        "enabled": True,
        "keywords": [
            "대통령",
            "비서실장",
            "정책실장",
            "홍보수석",
            "안보실장",
            "정무수석",
            "김혜경 여사",
            "장관 인터뷰",
            "처장 인터뷰",
            "청장 인터뷰",
            "장관 기자 간담회",
            "처장 기자 간담회",
            "청장 기자 간담회",
        ],
    },
    {
        "id": "youtube-ktv",
        "name": "유튜브 KTV 국민방송",
        "type": "youtube",
        "url": "https://www.youtube.com/@KTV_korea",
        "enabled": True,
        "keywords": ["브리핑", "회의"],
    },
    {
        "id": "sns-public",
        "name": "X / Facebook 공개 계정",
        "type": "web",
        "url": "",
        "enabled": False,
        "keywords": ["대통령", "비서실장", "정책실장", "홍보수석", "안보실장", "정무수석"],
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def initial_data() -> dict[str, Any]:
    return {
        "interval_seconds": 180,
        "sources": DEFAULT_SOURCES,
        "items": [],
        "seen": [],
        "statuses": {},
        "last_cycle": None,
        "initialized_sources": [],
        "cycle_state": {"running": False, "started_at": None, "finished_at": None},
    }


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        if not DATA_FILE.exists():
            self.data = initial_data()
            self.save()
            return
        try:
            self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.data = initial_data()
            self.save()
            return
        self.data.setdefault("items", [])
        self.data.setdefault("seen", [])
        self.data.setdefault("statuses", {})
        self.data.setdefault("last_cycle", None)
        self.data.setdefault("initialized_sources", [])
        self.data.setdefault(
            "cycle_state", {"running": False, "started_at": None, "finished_at": None}
        )

    def save(self) -> None:
        with self.lock:
            temporary = DATA_FILE.with_suffix(".json.tmp")
            backup = DATA_FILE.with_suffix(".json.bak")
            temporary.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if DATA_FILE.exists():
                shutil.copy2(DATA_FILE, backup)
            os.replace(temporary, DATA_FILE)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def replace_settings(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("설정은 객체여야 합니다.")
        sources = payload.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError("출처 목록은 배열이어야 합니다.")
        with self.lock:
            old_sources = {
                source["id"]: source
                for source in self.data.get("sources", [])
                if isinstance(source, dict) and source.get("id")
            }
            self.data["interval_seconds"] = max(
                60, min(3600, int(payload.get("interval_seconds", 180)))
            )
            clean_sources = []
            for source in sources:
                if not isinstance(source, dict):
                    raise ValueError("출처 설정 형식이 올바르지 않습니다.")
                source_type = str(source.get("type", "web"))
                if source_type not in {"naver", "youtube", "web"}:
                    source_type = "web"
                clean_sources.append(
                    {
                        "id": str(source.get("id") or f"source-{time.time_ns()}"),
                        "name": str(source.get("name", "")).strip() or "이름 없는 출처",
                        "type": source_type,
                        "url": str(source.get("url", "")).strip(),
                        "enabled": bool(source.get("enabled", True)),
                        "keywords": [
                            str(keyword).strip()
                            for keyword in source.get("keywords", [])
                            if str(keyword).strip()
                        ],
                    }
                )
            self.data["sources"] = clean_sources
            for source in clean_sources:
                old = old_sources.get(source["id"])
                if old and any(
                    old.get(key) != source.get(key) for key in ("type", "url", "keywords")
                ):
                    self.data["initialized_sources"] = [
                        source_id
                        for source_id in self.data["initialized_sources"]
                        if source_id != source["id"]
                    ]
            self.save()

    def add_items(self, source: dict[str, Any], items: list[dict[str, Any]]) -> tuple[int, bool]:
        with self.lock:
            seen = set(self.data["seen"])
            source_id = source["id"]
            initialized = source_id in self.data["initialized_sources"]
            if not initialized:
                for item in items:
                    link = normalize_url(item.get("link", ""))
                    if link:
                        seen.add(link)
                self.data["seen"] = list(seen)
                self.data["initialized_sources"].append(source_id)
                self.save()
                return 0, True
            added = 0
            for item in items:
                link = normalize_url(item.get("link", ""))
                if not link or link in seen:
                    continue
                seen.add(link)
                self.data["items"].insert(
                    0,
                    {
                        "source_id": source["id"],
                        "source": source["name"],
                        "title": item.get("title", link),
                        "link": link,
                        "published": item.get("published", ""),
                        "matched_keywords": item.get("matched_keywords", []),
                        "live_status": item.get("live_status", ""),
                        "found_at": utc_now(),
                    },
                )
                added += 1
            active_links = [
                normalize_url(item.get("link", "")) for item in self.data["items"][:2000]
            ]
            self.data["seen"] = list(seen.union(link for link in active_links if link))
            self.data["items"] = self.data["items"][:2000]
            self.save()
            return added, False

    def status(self, source_id: str, ok: bool, message: str, added: int = 0) -> None:
        with self.lock:
            self.data["statuses"][source_id] = {
                "ok": ok,
                "message": message,
                "added": added,
                "checked_at": utc_now(),
            }
            self.save()

    def cycle_state(self, running: bool) -> None:
        with self.lock:
            state = self.data["cycle_state"]
            state["running"] = running
            if running:
                state["started_at"] = utc_now()
            else:
                state["finished_at"] = utc_now()
            self.save()


def export_items_to_csv(data: dict[str, Any], export_dir: Path = EXPORT_DIR) -> Path | None:
    items = data.get("items", [])
    if not items:
        return None
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"news-links-{export_timestamp()}.csv"
    headers = ["found_at", "source", "title", "link", "matched_keywords", "live_status"]
    with path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            row = dict(item)
            row["matched_keywords"] = ", ".join(item.get("matched_keywords", []))
            writer.writerow(row)
    return path


store = Store()
cycle_lock = threading.Lock()
stop_event = threading.Event()


def fetch(url: str, timeout: int = 20) -> tuple[str, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace"), response.geturl()


def normalize_url(url: str) -> str:
    url = html.unescape(url).strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        return ""
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urllib.parse.urlencode(query), "")
    )


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def keyword_matches(text: str, keywords: list[str]) -> list[str]:
    lowered = text.casefold()
    return [keyword for keyword in keywords if keyword.casefold() in lowered]


class AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value or "" for key, value in attrs}
        self.current = {
            "href": values.get("href", ""),
            "class": values.get("class", ""),
            "target": values.get("data-heatmap-target", ""),
            "text": "",
        }

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.current is not None:
            self.anchors.append(self.current)
            self.current = None


def parse_anchors(page: str) -> list[dict[str, str]]:
    parser = AnchorParser()
    parser.feed(page)
    return parser.anchors


def is_naver_news_article(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.casefold().split(":", 1)[0]
    if host not in {"news.naver.com", "n.news.naver.com"}:
        return False
    return bool(
        re.search(r"/(?:article|mnews/article|main/read|article/comment)/", parts.path)
        or "oid=" in parts.query
    )


def is_naver_search_result(anchor: dict[str, str], url: str) -> bool:
    classes = set(anchor["class"].split())
    return bool(
        anchor.get("target") == ".tit"
        or classes.intersection({"news_tit", "sds-comps-text-type-headline1"})
        or is_naver_news_article(url)
    )


def collect_naver(source: dict[str, Any]) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for keyword in source["keywords"]:
        url = "https://search.naver.com/search.naver?" + urllib.parse.urlencode(
            {"where": "news", "sort": "1", "query": keyword}
        )
        page, _ = fetch(url)
        for anchor in parse_anchors(page):
            link = anchor["href"]
            title = clean_text(anchor["text"])
            normalized = normalize_url(link)
            matches = keyword_matches(title, source["keywords"])
            if (
                not normalized
                or not title
                or keyword not in matches
                or not is_naver_search_result(anchor, normalized)
            ):
                continue
            if normalized not in results:
                results[normalized] = {
                    "title": title,
                    "link": normalized,
                    "published": "",
                    "matched_keywords": matches,
                }
            else:
                results[normalized]["matched_keywords"] = sorted(
                    set(results[normalized]["matched_keywords"] + matches)
                )
        time.sleep(1.5)
    return list(results.values())


def resolve_youtube_channel_id(source_url: str) -> str:
    match = re.search(r"/channel/(UC[\w-]+)", source_url)
    if match:
        return match.group(1)
    page, _ = fetch(source_url)
    for pattern in (
        r'"channelId":"(UC[\w-]+)"',
        r'<meta itemprop="channelId" content="(UC[\w-]+)"',
        r"youtube\.com/channel/(UC[\w-]+)",
    ):
        match = re.search(pattern, page)
        if match:
            return match.group(1)
    raise ValueError("유튜브 채널 ID를 찾지 못했습니다.")


def collect_youtube(source: dict[str, Any]) -> list[dict[str, Any]]:
    channel_id = resolve_youtube_channel_id(source["url"])
    xml, _ = fetch(
        "https://www.youtube.com/feeds/videos.xml?"
        + urllib.parse.urlencode({"channel_id": channel_id})
    )
    root = ElementTree.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns)
        link_node = entry.find("atom:link", ns)
        link = link_node.get("href", "") if link_node is not None else ""
        matches = keyword_matches(title, source["keywords"])
        if source["keywords"] and not matches:
            continue
        live_status = youtube_live_status(link)
        if not live_status:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "published": entry.findtext("atom:published", default="", namespaces=ns),
                "matched_keywords": matches,
                "live_status": live_status,
            }
        )
    return items


def youtube_live_status(video_url: str) -> str:
    page, _ = fetch(video_url)
    if re.search(r'"isLiveNow"\s*:\s*true', page):
        return "진행 중"
    if re.search(r'"isUpcoming"\s*:\s*true', page):
        return "예정"
    return ""


def collect_web(source: dict[str, Any]) -> list[dict[str, Any]]:
    page, resolved_url = fetch(source["url"])
    anchors = re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page, flags=re.I | re.S
    )
    items = []
    for href, raw_title in anchors:
        title = clean_text(raw_title)
        matches = keyword_matches(title, source["keywords"])
        if not title or (source["keywords"] and not matches):
            continue
        link = urllib.parse.urljoin(resolved_url, html.unescape(href))
        if normalize_url(link):
            items.append(
                {
                    "title": title[:300],
                    "link": link,
                    "published": "",
                    "matched_keywords": matches,
                }
            )
    return items[:100]


def run_cycle() -> bool:
    if not cycle_lock.acquire(blocking=False):
        return False
    store.cycle_state(True)
    try:
        snapshot = store.snapshot()
        for source in snapshot["sources"]:
            if not source["enabled"] or not source["url"]:
                continue
            try:
                if source["type"] == "naver":
                    items = collect_naver(source)
                elif source["type"] == "youtube":
                    items = collect_youtube(source)
                else:
                    items = collect_web(source)
                added, baseline = store.add_items(source, items)
                message = (
                    f"기준점 설정 · 기존 링크 {len(items)}개 제외"
                    if baseline
                    else f"{len(items)}개 후보 확인"
                )
                store.status(source["id"], True, message, added)
            except (urllib.error.URLError, TimeoutError, ValueError, ElementTree.ParseError) as exc:
                store.status(source["id"], False, f"확인 실패: {exc}")
            except Exception as exc:
                store.status(source["id"], False, f"예상하지 못한 오류: {exc}")
        with store.lock:
            store.data["last_cycle"] = utc_now()
            store.save()
    finally:
        store.cycle_state(False)
        cycle_lock.release()
    return True


def monitor_loop() -> None:
    while not stop_event.is_set():
        run_cycle()
        stop_event.wait(store.snapshot().get("interval_seconds", 180))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, payload: Any, status: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/state":
            self.json_response(store.snapshot())
            return
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in file_path.parents:
            self.send_bytes(b"Not found", "text/plain", 404)
            return
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_bytes(b"Not found", "text/plain", 404)
            return
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(file_path.suffix, "application/octet-stream")
        self.send_bytes(body, mime)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.json_response({"error": "잘못된 요청입니다."}, 400)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/settings":
            try:
                store.replace_settings(payload)
            except (TypeError, ValueError):
                self.json_response({"error": "설정 값을 확인해 주세요."}, 400)
                return
            self.json_response({"ok": True})
            return
        if path == "/api/check":
            if cycle_lock.locked():
                self.json_response({"ok": False, "error": "이미 확인 중입니다."}, 409)
                return
            threading.Thread(target=run_cycle, daemon=True).start()
            self.json_response({"ok": True})
            return
        self.json_response({"error": "Not found"}, 404)


def main() -> None:
    monitor = threading.Thread(target=monitor_loop, daemon=True)
    monitor.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"뉴스·게시물 링크 모니터가 실행 중입니다: {url}")
    if os.environ.get("NEWS_LINK_MONITOR_NO_BROWSER") != "1":
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()
        exported = export_items_to_csv(store.snapshot())
        if exported:
            print(f"CSV 파일로 저장했습니다: {exported}")
        else:
            print("저장할 새 링크가 없어 CSV 파일을 만들지 않았습니다.")


if __name__ == "__main__":
    main()
