import argparse
import json
import logging
import random
import sqlite3
import time
from typing import Dict, Optional
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fanqie-scraper-no-content")

REQUEST_DELAY = 1.2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
DB_PATH = "resources/personal_recommender.db"
SOURCE_NAME = "番茄免费小说"
DEFAULT_TIMEOUT = 20


def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS books (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        source_id TEXT,
        title TEXT,
        author TEXT,
        intro TEXT,
        cover_url TEXT,
        tags TEXT,
        extra TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source, source_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chapters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id INTEGER,
        chap_index INTEGER,
        chap_title TEXT,
        chap_url TEXT,
        content TEXT,
        last_fetched TIMESTAMP,
        UNIQUE(book_id, chap_index),
        FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
    );
    """)
    conn.commit()


def save_book(conn: sqlite3.Connection, source_id: str, title: str, author: str, intro: str,
              cover_url: str, tags: str, extra: Optional[Dict] = None) -> int:
    cur = conn.cursor()
    extra_json = json.dumps(extra, ensure_ascii=False) if extra is not None else None
    cur.execute("""
    INSERT INTO books (source, source_id, title, author, intro, cover_url, tags, extra)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(source, source_id) DO UPDATE SET
        title=excluded.title, author=excluded.author, intro=excluded.intro,
        cover_url=excluded.cover_url, tags=excluded.tags, extra=excluded.extra, last_updated=CURRENT_TIMESTAMP
    """, (SOURCE_NAME, source_id, title, author, intro, cover_url, tags, extra_json))
    conn.commit()
    cur.execute("SELECT id FROM books WHERE source=? AND source_id=?", (SOURCE_NAME, source_id))
    return cur.fetchone()[0]


def save_chapter(conn: sqlite3.Connection, book_id: int, chap_index: int, chap_title: str, chap_url: str):
    """
    明确不保存章节正文。content 字段保持 NULL。
    使用 INSERT OR IGNORE 防止重复索引写入。
    """
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO chapters (book_id, chap_index, chap_title, chap_url, content, last_fetched)
    VALUES (?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
    """, (book_id, chap_index, chap_title, chap_url))
    conn.commit()


def purge_existing_chapter_contents(conn: sqlite3.Connection, only_source: bool = True):
    """
    将已存在的章节正文置为 NULL。
    如果 only_source 为 True，则只清理 source = SOURCE_NAME 的书对应的章节；
    否则清理所有 chapters.content。
    """
    cur = conn.cursor()
    if only_source:
        cur.execute("SELECT id FROM books WHERE source = ?", (SOURCE_NAME,))
        rows = cur.fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            logger.info("数据库中无来自 %s 的书，跳过清理。", SOURCE_NAME)
            return 0
        q = ",".join(["?"] * len(ids))
        sql = f"UPDATE chapters SET content = NULL WHERE book_id IN ({q})"
        cur.execute(sql, ids)
    else:
        cur.execute("UPDATE chapters SET content = NULL")
    conn.commit()
    return cur.rowcount


def normalize_url(base: str, href: str) -> str:
    if not href:
        return ""
    return urljoin(base, href)


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_html(session: requests.Session, url: str) -> str:
    logger.info("GET %s", url)
    r = session.get(url, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    time.sleep(REQUEST_DELAY + random.random() * 0.6)
    return r.text


def extract_from_meta(soup: BeautifulSoup) -> Dict:
    data = {}
    og_title = soup.select_one('meta[property="og:title"]')
    if og_title and og_title.get("content"):
        data.setdefault("title", og_title["content"].strip())
    meta_title = soup.select_one("title")
    if meta_title and meta_title.get_text(strip=True):
        data.setdefault("title", meta_title.get_text(strip=True))
    og_image = soup.select_one('meta[property="og:image"]')
    if og_image and og_image.get("content"):
        data.setdefault("cover", og_image["content"].strip())
    meta_desc = soup.select_one('meta[name="description"]')
    if meta_desc and meta_desc.get("content"):
        data.setdefault("intro", meta_desc["content"].strip())
    author_meta = soup.select_one('meta[name="author"]')
    if author_meta and author_meta.get("content"):
        data.setdefault("author", author_meta["content"].strip())
    return data


def parse_book_page(html: str, page_url: str) -> Dict:
    base = "{0.scheme}://{0.netloc}".format(urlsplit(page_url))
    soup = BeautifulSoup(html, "html.parser")
    data = extract_from_meta(soup)

    if "title" not in data or not data["title"]:
        el = soup.select_one("h1.entry-title, h1.post-title, .book-title, h1")
        if el:
            data["title"] = el.get_text(strip=True)

    if "author" not in data or not data["author"]:
        el = soup.select_one(".author a, .book-author, .post-author, .meta-author")
        if el:
            data["author"] = el.get_text(strip=True)

    if "intro" not in data or not data["intro"]:
        el = soup.select_one(".entry-content .post-content, .book-desc, .description, .intro, .summary")
        if el:
            data["intro"] = el.get_text(separator="\n", strip=True)[:2000]

    if "cover" not in data or not data["cover"]:
        el = soup.select_one(".book-cover img, .post-thumbnail img, .cover img, img.book-cover")
        if el and el.get("src"):
            data["cover"] = normalize_url(base, el["src"])

    tags = []
    for t in soup.select(".tags a, .post-tags a, .book-tags a"):
        txt = t.get_text(strip=True)
        if txt:
            tags.append(txt)
    data["tags"] = ",".join(tags) if tags else ""

    chapters = []
    chapter_containers = soup.select(".chapter-list, .post-chapters, .chapters, .catalog, .book-chapters, #list-chapter")
    if not chapter_containers:
        for a in soup.select("a"):
            txt = a.get_text(strip=True)
            if txt and (("章" in txt) or txt.lower().startswith("chapter") or txt.lower().startswith("ch")):
                href = a.get("href")
                chapters.append({"title": txt, "url": normalize_url(base, href) if href else ""})
    else:
        for cont in chapter_containers:
            for a in cont.select("a"):
                txt = a.get_text(strip=True)
                href = a.get("href")
                if not txt:
                    continue
                chapters.append({"title": txt, "url": normalize_url(base, href) if href else ""})

    seen = set()
    chapters_filtered = []
    for c in chapters:
        key = (c["title"], c["url"])
        if key in seen:
            continue
        seen.add(key)
        chapters_filtered.append(c)
    data["chapters"] = chapters_filtered
    return data


def process_url(url: str):
    session = get_session()
    html = fetch_html(session, url)
    book = parse_book_page(html, url)

    parts = urlsplit(url)
    source_id = parts.path.rstrip("/").split("/")[-1] or parts.query or url

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)
    book_id = save_book(conn, source_id=source_id, title=book.get("title", ""),
                        author=book.get("author", ""), intro=book.get("intro", ""),
                        cover_url=book.get("cover", ""), tags=book.get("tags", ""), extra={
                            "url": url, "parsed_at": time.time()
                        })
    logger.info("保存书籍 [%s] id=%d（不保存章节正文）", book.get("title", ""), book_id)

    for idx, ch in enumerate(book.get("chapters", []), start=1):
        save_chapter(conn, book_id=book_id, chap_index=idx, chap_title=ch.get("title", ""), chap_url=ch.get("url"))
    conn.close()
    return {"book_id": book_id, "title": book.get("title", ""), "chapters": len(book.get("chapters", []))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="书籍详情页 URL")
    parser.add_argument("--file", help="包含 URL 的文件，每行一个 URL")
    parser.add_argument("--purge-existing", action="store_true", help="将 DB 中该来源的章节正文置为 NULL")
    args = parser.parse_args()

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)
    if args.purge_existing:
        cnt = purge_existing_chapter_contents(conn, only_source=True)
        logger.info("已清理 %d 条章节正文（置为 NULL）", cnt)
        conn.close()
        return

    urls = []
    if args.url:
        urls.append(args.url)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    urls.append(line)

    if not urls:
        parser.print_help()
        return

    results = []
    for u in urls:
        try:
            res = process_url(u)
            results.append(res)
        except Exception as e:
            logger.exception("处理 %s 时出错: %s", u, e)
    logger.info("完成，总计处理 %d 本书", len(results))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()