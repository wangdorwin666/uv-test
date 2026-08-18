from flask import Flask, render_template, request, jsonify, g, abort
from flask_cors import CORS
import sqlite3
import os

from recommender_agent import recommend_by_genre

DB_PATH = os.path.join("resources", "personal_recommender.db")
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/recommend", methods=["POST"])
def recommend():
    genre = request.form.get("genre", "").strip()
    preferences = request.form.get("preferences", "").strip()
    if not genre:
        return render_template("index.html", error="请填写小说题材。")
    try:
        report = recommend_by_genre(genre, preferences)
    except Exception as e:
        report = f"调用推荐 agent 时出错：{e}"
    return render_template("result.html", genre=genre, preferences=preferences, report=report)


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        g._database = db
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def row_to_dict(row: sqlite3.Row):
    return {k: row[k] for k in row.keys()}


@app.route("/api/books", methods=["GET"])
def list_books():
    q = (request.args.get("q") or "").strip()
    source = (request.args.get("source") or "").strip()
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    if limit <= 0:
        limit = DEFAULT_LIMIT
    if limit > MAX_LIMIT:
        limit = MAX_LIMIT
    if offset < 0:
        offset = 0

    db = get_db()
    params = []
    where_clauses = []

    if source:
        where_clauses.append("source = ?")
        params.append(source)

    if q:
        like_q = f"%{q}%"
        where_clauses.append("(title LIKE ? OR author LIKE ? OR tags LIKE ? OR intro LIKE ?)")
        params.extend([like_q, like_q, like_q, like_q])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    count_sql = f"SELECT COUNT(1) as cnt FROM books {where_sql}"
    cur = db.execute(count_sql, params)
    total = cur.fetchone()["cnt"]

    sql = f"""
    SELECT id, source, source_id, title, author, intro, cover_url, tags, last_updated, extra
    FROM books
    {where_sql}
    ORDER BY last_updated DESC
    LIMIT ? OFFSET ?
    """
    cur_params = params + [limit, offset]
    cur = db.execute(sql, cur_params)
    items = []
    for row in cur.fetchall():
        r = row_to_dict(row)
        items.append({
            "id": r.get("id"),
            "source": r.get("source"),
            "source_id": r.get("source_id"),
            "title": r.get("title"),
            "author": r.get("author"),
            "intro": r.get("intro"),
            "cover_url": r.get("cover_url"),
            "tags": r.get("tags"),
            "last_updated": r.get("last_updated"),
            "extra": r.get("extra"),
        })

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(items),
        "items": items,
    })


@app.route("/api/book/<id_or_source_id>", methods=["GET"])
def get_book(id_or_source_id):
    db = get_db()

    book_row = None
    if id_or_source_id.isdigit():
        cur = db.execute("SELECT id, source, source_id, title, author, intro, cover_url, tags, last_updated, extra FROM books WHERE id = ?",
                         (int(id_or_source_id),))
        book_row = cur.fetchone()

    if not book_row:
        cur = db.execute("SELECT id, source, source_id, title, author, intro, cover_url, tags, last_updated, extra FROM books WHERE source_id = ?",
                         (id_or_source_id,))
        book_row = cur.fetchone()

    if not book_row:
        abort(404, description="Book not found")

    book = row_to_dict(book_row)

    cur = db.execute("SELECT chap_index, chap_title, chap_url FROM chapters WHERE book_id = ? ORDER BY chap_index ASC",
                     (book["id"],))
    chapters = []
    for r in cur.fetchall():
        chapters.append({
            "chap_index": r["chap_index"],
            "chap_title": r["chap_title"],
            "chap_url": r["chap_url"]
        })

    return jsonify({
        "id": book["id"],
        "source": book["source"],
        "source_id": book["source_id"],
        "title": book["title"],
        "author": book["author"],
        "intro": book["intro"],
        "cover_url": book["cover_url"],
        "tags": book["tags"],
        "last_updated": book["last_updated"],
        "extra": book["extra"],
        "chapters": chapters
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)