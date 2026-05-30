import base64
import html
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime

import streamlit as st

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Course Feed",
    page_icon="📱",
    layout="centered",
)

# ── Passwords ────────────────────────────────────────────────────────────────
FEED_PASSWORD = st.secrets.get("FEED_PASSWORD", "test")
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "test")

# ── Platform config ──────────────────────────────────────────────────────────
CONTENT_LIMIT = 2200
ACCOUNT_LABEL = "Account name (fictional)"

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social.db")


def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            platform     TEXT NOT NULL,
            account_name TEXT NOT NULL,
            title        TEXT,
            content      TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            media_type   TEXT,
            media_data   TEXT
        );
        CREATE TABLE IF NOT EXISTS likes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id     INTEGER NOT NULL,
            session_id  TEXT NOT NULL,
            reaction    TEXT NOT NULL DEFAULT 'like',
            created_at  TEXT NOT NULL,
            UNIQUE(post_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS comments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id      INTEGER NOT NULL,
            account_name TEXT NOT NULL,
            content      TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            parent_id    INTEGER DEFAULT NULL,
            depth        INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS comment_likes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id  INTEGER NOT NULL,
            session_id  TEXT NOT NULL,
            reaction    TEXT NOT NULL DEFAULT 'like',
            created_at  TEXT NOT NULL,
            UNIQUE(comment_id, session_id, reaction)
        );
    """)
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('comment_mode', 'anonymous')")
    conn.commit()
    conn.close()


def migrate_db():
    conn = get_conn()
    migrations = [
        "ALTER TABLE posts ADD COLUMN media_type TEXT",
        "ALTER TABLE posts ADD COLUMN media_data TEXT",
        "ALTER TABLE comments ADD COLUMN parent_id INTEGER DEFAULT NULL",
        "ALTER TABLE comments ADD COLUMN depth INTEGER DEFAULT 0",
        "ALTER TABLE likes ADD COLUMN reaction TEXT DEFAULT 'like'",
        """CREATE TABLE IF NOT EXISTS comment_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            reaction TEXT NOT NULL DEFAULT 'like',
            created_at TEXT NOT NULL,
            UNIQUE(comment_id, session_id, reaction)
        )""",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()
migrate_db()

# ── Session state ─────────────────────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "post_count" not in st.session_state:
    st.session_state.post_count = 0
if "show_extra_form" not in st.session_state:
    st.session_state.show_extra_form = False
if "account_name" not in st.session_state:
    st.session_state.account_name = ""
if "feed_unlocked" not in st.session_state:
    st.session_state.feed_unlocked = False
if "liked_posts" not in st.session_state:
    st.session_state.liked_posts = set()
if "liked_comments" not in st.session_state:
    st.session_state.liked_comments = set()
if "reply_to" not in st.session_state:
    st.session_state.reply_to = {}
if "show_comments" not in st.session_state:
    st.session_state.show_comments = {}
if "admin_unlocked" not in st.session_state:
    st.session_state.admin_unlocked = False

# ── Global CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stButton > button { min-height: 44px; border-radius: 8px; }
@media (min-width: 900px) { .block-container { max-width: 820px !important; } }
</style>
""", unsafe_allow_html=True)


# ── DB helpers ────────────────────────────────────────────────────────────────
def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def time_ago(ts):
    try:
        diff = datetime.utcnow() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        s = int(diff.total_seconds())
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m"
        if s < 86400:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
    except Exception:
        return ""


def db_get_like_count(post_id):
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM likes WHERE post_id = ? AND reaction = 'like'",
        (post_id,),
    ).fetchone()[0]
    conn.close()
    return count


def db_get_session_reaction(post_id, session_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT reaction FROM likes WHERE post_id = ? AND session_id = ?",
        (post_id, session_id),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def db_set_reaction(post_id, session_id, reaction):
    conn = get_conn()
    existing = conn.execute(
        "SELECT reaction FROM likes WHERE post_id = ? AND session_id = ?",
        (post_id, session_id),
    ).fetchone()
    if existing and existing[0] == reaction:
        conn.execute(
            "DELETE FROM likes WHERE post_id = ? AND session_id = ?",
            (post_id, session_id),
        )
    elif existing:
        conn.execute(
            "UPDATE likes SET reaction = ?, created_at = ? WHERE post_id = ? AND session_id = ?",
            (reaction, now_str(), post_id, session_id),
        )
    else:
        conn.execute(
            "INSERT INTO likes (post_id, session_id, reaction, created_at) VALUES (?, ?, ?, ?)",
            (post_id, session_id, reaction, now_str()),
        )
    conn.commit()
    conn.close()


def db_get_comments(post_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, account_name, content, created_at, parent_id, depth "
        "FROM comments WHERE post_id = ? ORDER BY created_at ASC",
        (post_id,),
    ).fetchall()
    conn.close()
    return rows


def db_add_comment(post_id, account_name, content, parent_id=None, depth=0):
    conn = get_conn()
    conn.execute(
        "INSERT INTO comments (post_id, account_name, content, created_at, parent_id, depth) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (post_id, account_name, content, now_str(), parent_id, depth),
    )
    conn.commit()
    conn.close()


def db_get_comment_mode():
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = 'comment_mode'").fetchone()
    conn.close()
    return row[0] if row else "anonymous"


def db_set_comment_mode(mode):
    conn = get_conn()
    conn.execute("UPDATE settings SET value = ? WHERE key = 'comment_mode'", (mode,))
    conn.commit()
    conn.close()


def db_get_comment_like_count(comment_id, reaction="like"):
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM comment_likes WHERE comment_id = ? AND reaction = ?",
        (comment_id, reaction),
    ).fetchone()[0]
    conn.close()
    return count


def db_toggle_comment_like(comment_id, session_id, reaction="like"):
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM comment_likes WHERE comment_id = ? AND session_id = ? AND reaction = ?",
        (comment_id, session_id, reaction),
    ).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM comment_likes WHERE comment_id = ? AND session_id = ? AND reaction = ?",
            (comment_id, session_id, reaction),
        )
    else:
        try:
            conn.execute(
                "INSERT INTO comment_likes (comment_id, session_id, reaction, created_at) "
                "VALUES (?, ?, ?, ?)",
                (comment_id, session_id, reaction, now_str()),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()


def db_get_all_posts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, platform, account_name, title, content, created_at, media_type, media_data "
        "FROM posts ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return rows


def db_create_post(platform, account_name, title, content, media_type=None, media_data=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO posts (platform, account_name, title, content, created_at, media_type, media_data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (platform, account_name, title, content, now_str(), media_type, media_data),
    )
    conn.commit()
    conn.close()


def db_delete_comment(comment_id):
    conn = get_conn()
    children = conn.execute(
        "SELECT id FROM comments WHERE parent_id = ?", (comment_id,)
    ).fetchall()
    for (child_id,) in children:
        grandchildren = conn.execute(
            "SELECT id FROM comments WHERE parent_id = ?", (child_id,)
        ).fetchall()
        for (gc_id,) in grandchildren:
            conn.execute("DELETE FROM comment_likes WHERE comment_id = ?", (gc_id,))
            conn.execute("DELETE FROM comments WHERE id = ?", (gc_id,))
        conn.execute("DELETE FROM comment_likes WHERE comment_id = ?", (child_id,))
        conn.execute("DELETE FROM comments WHERE id = ?", (child_id,))
    conn.execute("DELETE FROM comment_likes WHERE comment_id = ?", (comment_id,))
    conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()


def db_delete_post(post_id):
    conn = get_conn()
    comment_ids = conn.execute(
        "SELECT id FROM comments WHERE post_id = ?", (post_id,)
    ).fetchall()
    for (cid,) in comment_ids:
        conn.execute("DELETE FROM comment_likes WHERE comment_id = ?", (cid,))
    conn.execute("DELETE FROM likes WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM comments WHERE post_id = ?", (post_id,))
    conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    conn.commit()
    conn.close()


def db_reset():
    conn = get_conn()
    conn.executescript(
        "DELETE FROM comment_likes; DELETE FROM comments; DELETE FROM likes; DELETE FROM posts;"
    )
    conn.commit()
    conn.close()


# ── Media helpers ─────────────────────────────────────────────────────────────
def youtube_embed_url(url):
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    if "youtube.com/embed/" in url:
        return url
    return None


def render_media_html(media_type, media_data):
    if not media_type or not media_data:
        return ""
    if media_type in ("image_url", "image_data"):
        img_style = "width:100%; aspect-ratio:1/1; object-fit:cover; display:block;"
        src = html.escape(media_data) if media_type == "image_url" else media_data
        return f'<img src="{src}" style="{img_style}" />'
    elif media_type in ("video_url", "video_data"):
        if media_type == "video_url":
            embed = youtube_embed_url(media_data)
            if embed:
                return (
                    f'<iframe width="100%" height="280" src="{html.escape(embed)}" '
                    f'frameborder="0" allowfullscreen style="display:block; border-radius:4px;"></iframe>'
                )
            src = html.escape(media_data)
        else:
            src = media_data
        return (
            f'<video src="{src}" controls '
            f'style="width:100%; max-height:300px; border-radius:4px; display:block;"></video>'
        )
    return ""


def avatar_initial(account_name):
    name = account_name.lstrip("@").strip()
    return name[0].upper() if name else "?"


# ── Platform card renderers ───────────────────────────────────────────────────
def _card_instagram(acc, content, media_html):
    i = html.escape(avatar_initial(acc))
    a = html.escape(acc)
    c = html.escape(content)
    media_block = (
        f'<div style="overflow:hidden; background:#efefef;">{media_html}</div>'
        if media_html else ""
    )
    return f"""
<div style="background:#fff; color:#262626; border:1px solid #dbdbdb; border-radius:4px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            overflow:hidden; margin-bottom:0;">
  <div style="display:flex; align-items:center; padding:12px 16px;">
    <div style="width:32px; height:32px; border-radius:50%;
                background:linear-gradient(45deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888);
                display:flex; align-items:center; justify-content:center;
                color:#fff; font-weight:700; font-size:14px; flex-shrink:0; margin-right:10px;">{i}</div>
    <span style="font-weight:600; font-size:14px;">{a}</span>
  </div>
  {media_block}
  <div style="padding:8px 16px 14px; font-size:14px; line-height:1.5;
              white-space:pre-wrap; word-break:break-word;">
    {c}
  </div>
</div>"""


def render_post_card(acc, content, media_type, media_data):
    media_html = render_media_html(media_type, media_data)
    return _card_instagram(acc, content, media_html)


# ── Post creation form ────────────────────────────────────────────────────────
def show_post_form(key_prefix=""):
    col_form, col_preview = st.columns([1, 1])

    media_type_val = None
    media_data_val = None

    with col_form:
        default_acc = st.session_state.account_name if st.session_state.account_name else ""
        raw_account = st.text_input(
            ACCOUNT_LABEL,
            value=default_acc,
            placeholder="e.g. clima_fox",
            help="Fictional username",
            key=f"{key_prefix}account",
        )

        content_val = st.text_area(
            "Caption",
            max_chars=CONTENT_LIMIT,
            height=150,
            placeholder=f"Write here… (max {CONTENT_LIMIT} characters)",
            key=f"{key_prefix}content",
        )
        chars_used = len(content_val)
        char_color = "red" if chars_used >= CONTENT_LIMIT else "gray"
        st.markdown(
            f'<p style="color:{char_color}; font-size:0.82rem; margin-top:-12px;">'
            f"{chars_used} / {CONTENT_LIMIT} characters</p>",
            unsafe_allow_html=True,
        )

        st.markdown("**Media** (optional)")
        media_option = st.radio(
            "Add media",
            ["No media", "Image URL", "Video URL", "Upload image", "Upload video"],
            horizontal=True,
            label_visibility="collapsed",
            key=f"{key_prefix}media_option",
        )

        if media_option == "Image URL":
            img_url = st.text_input(
                "Image URL", placeholder="https://example.com/photo.jpg",
                key=f"{key_prefix}img_url",
            )
            if img_url.strip():
                media_type_val = "image_url"
                media_data_val = img_url.strip()
                st.image(img_url.strip(), caption="Preview", use_container_width=True)

        elif media_option == "Video URL":
            vid_url = st.text_input(
                "Video URL",
                placeholder="YouTube link or direct video URL",
                help="youtube.com/watch?v=… · youtu.be/… · or direct .mp4",
                key=f"{key_prefix}vid_url",
            )
            if vid_url.strip():
                media_type_val = "video_url"
                media_data_val = vid_url.strip()
                embed = youtube_embed_url(vid_url.strip())
                if embed:
                    st.markdown(
                        f'<iframe width="100%" height="220" src="{html.escape(embed)}" '
                        f'frameborder="0" allowfullscreen></iframe>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.video(vid_url.strip())

        elif media_option == "Upload image":
            uploaded = st.file_uploader(
                "Upload image",
                type=["jpg", "jpeg", "png", "gif", "webp"],
                help="Maximum 2 MB",
                key=f"{key_prefix}upload_img",
            )
            if uploaded is not None:
                if uploaded.size > 2 * 1024 * 1024:
                    st.error("Image too large. Maximum size is 2 MB.")
                    uploaded = None
                else:
                    file_bytes = uploaded.read()
                    mime = uploaded.type or "image/jpeg"
                    b64 = base64.b64encode(file_bytes).decode()
                    media_data_val = f"data:{mime};base64,{b64}"
                    media_type_val = "image_data"
                    st.image(uploaded, caption="Preview", use_container_width=True)

        elif media_option == "Upload video":
            uploaded_video = st.file_uploader(
                "Upload video",
                type=["mp4", "mov", "webm", "m4v"],
                help="Maximum 20 MB",
                key=f"{key_prefix}upload_video",
            )
            if uploaded_video is not None:
                if uploaded_video.size > 20 * 1024 * 1024:
                    st.error("Video too large. Maximum size is 20 MB.")
                    uploaded_video = None
                else:
                    file_bytes = uploaded_video.read()
                    mime = uploaded_video.type or "video/mp4"
                    b64 = base64.b64encode(file_bytes).decode()
                    media_data_val = f"data:{mime};base64,{b64}"
                    media_type_val = "video_data"
                    st.video(uploaded_video)

        if st.button("Post", use_container_width=True, type="primary", key=f"{key_prefix}submit"):
            errors = []
            account_name = raw_account.strip()
            if not account_name:
                errors.append("Account name is required.")
            if not content_val.strip():
                errors.append("Caption is required.")
            if media_option in ("Image URL", "Video URL") and not media_data_val:
                errors.append("Please enter a URL for the selected media type.")

            if errors:
                for e in errors:
                    st.error(e)
            else:
                if not account_name.startswith("@"):
                    account_name = "@" + account_name
                db_create_post(
                    "Instagram",
                    account_name,
                    None,
                    content_val.strip(),
                    media_type_val,
                    media_data_val,
                )
                st.session_state.show_extra_form = False
                st.session_state.post_count += 1
                st.session_state.account_name = account_name
                st.rerun()

    with col_preview:
        st.caption("Preview")
        cur_acc = st.session_state.get(f"{key_prefix}account", "") or "@your_account"
        cur_content = st.session_state.get(f"{key_prefix}content", "") or "Your caption will appear here…"
        card_html = render_post_card(cur_acc, cur_content, media_type_val, media_data_val)
        st.markdown(card_html, unsafe_allow_html=True)


# ── Feed interaction buttons ──────────────────────────────────────────────────
def render_interaction_buttons(pid, session_id):
    like_count = db_get_like_count(pid)
    cur_reaction = db_get_session_reaction(pid, session_id)
    comments = db_get_comments(pid)
    top_level_count = sum(1 for c in comments if c[4] is None)
    show_key = f"show_comments_{pid}"
    if show_key not in st.session_state.show_comments:
        st.session_state.show_comments[show_key] = False
    comments_open = st.session_state.show_comments[show_key]
    cmt_lbl = f"💬 {top_level_count} Comments ▲" if comments_open else f"💬 {top_level_count} Comments"

    col_like, col_cmt, _ = st.columns([1, 2, 3])
    with col_like:
        liked = cur_reaction == "like"
        lbl = f"❤️ {like_count}" if liked else f"🤍 {like_count}"
        if st.button(lbl, key=f"like_{pid}", disabled=liked):
            db_set_reaction(pid, session_id, "like")
            st.rerun()
    with col_cmt:
        if st.button(cmt_lbl, key=f"cmt_{pid}"):
            st.session_state.show_comments[show_key] = not comments_open
            st.rerun()


# ── Comment thread renderer ───────────────────────────────────────────────────
def render_comment_thread(comments, comment_mode, pid, session_id):
    by_parent = {}
    for c in comments:
        cid, cacc, ctext, ctime, parent_id, depth = c
        by_parent.setdefault(parent_id, []).append(c)

    INDENT = {0: 0, 1: 32, 2: 64}

    def render_one(comment):
        cid, cacc, ctext, ctime, parent_id, depth = comment
        display_name = "Anonymous" if comment_mode == "anonymous" else html.escape(cacc)
        initial = html.escape(avatar_initial(cacc))
        like_count = db_get_comment_like_count(cid)
        liked = cid in st.session_state.liked_comments
        indent_px = INDENT.get(depth, 64)
        ta = time_ago(ctime)

        st.markdown(
            f'<div style="display:flex; align-items:flex-start; padding:6px 0 10px 0; '
            f'margin-left:{indent_px}px; border-bottom:1px solid #f8f8f8;">'
            f'<div style="width:28px; height:28px; border-radius:50%; flex-shrink:0; margin-right:8px;'
            f'background:linear-gradient(45deg,#f09433,#e6683c,#dc2743,#cc2366,#bc1888);'
            f'display:flex; align-items:center; justify-content:center;'
            f'color:#fff; font-weight:700; font-size:11px;">{initial}</div>'
            f'<div style="flex:1; min-width:0;">'
            f'<span style="font-weight:700; font-size:0.88rem;">{display_name}</span>&nbsp;'
            f'<span style="font-size:0.88rem; word-break:break-word;">{html.escape(ctext)}</span>'
            f'<div style="font-size:0.76rem; color:#8e8e8e; margin-top:2px;">{ta}</div>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if depth < 2:
            btn_cols = st.columns([1, 0.4, 1, 5.6])
        else:
            btn_cols = st.columns([1, 7])

        with btn_cols[0]:
            rl = f"❤️ {like_count}" if liked else f"🤍 {like_count}"
            if st.button(rl, key=f"clk_{cid}"):
                db_toggle_comment_like(cid, session_id)
                if liked:
                    st.session_state.liked_comments.discard(cid)
                else:
                    st.session_state.liked_comments.add(cid)
                st.rerun()

        if depth < 2:
            with btn_cols[2]:
                cur_open = st.session_state.reply_to.get(cid, False)
                if st.button("Reply", key=f"replyBtn_{cid}"):
                    st.session_state.reply_to[cid] = not cur_open
                    st.rerun()

        if st.session_state.reply_to.get(cid, False):
            with st.form(key=f"reply_form_{cid}", clear_on_submit=True):
                reply_text = st.text_area(
                    "Reply",
                    max_chars=500,
                    height=80,
                    label_visibility="collapsed",
                    placeholder="Add a reply…",
                )
                if st.form_submit_button("Post", use_container_width=True):
                    if reply_text.strip():
                        db_add_comment(
                            pid,
                            st.session_state.account_name or "Anonymous",
                            reply_text.strip(),
                            parent_id=cid,
                            depth=min(depth + 1, 2),
                        )
                        st.session_state.reply_to[cid] = False
                        st.rerun()
                    else:
                        st.error("Reply cannot be empty.")

        for child in by_parent.get(cid, []):
            render_one(child)

    for top in by_parent.get(None, []):
        render_one(top)


# ── Admin comment helper ──────────────────────────────────────────────────────
def _render_admin_comment(comment, by_parent, indent):
    cid, cacc, ctext, _, parent_id, depth = comment
    col_text, col_btn = st.columns([5, 1])
    col_text.markdown(
        f'<div style="margin-left:{indent}px; font-size:0.88rem;">'
        f'<strong>{html.escape(cacc)}</strong>: {html.escape(ctext[:60])}'
        f'</div>',
        unsafe_allow_html=True,
    )
    if col_btn.button("Del", key=f"del_cmt_{cid}"):
        db_delete_comment(cid)
        st.rerun()
    for child in by_parent.get(cid, []):
        _render_admin_comment(child, by_parent, indent + 16)


# ── Sidebar: Admin area ───────────────────────────────────────────────────────
with st.sidebar:
    st.title("Admin Area")
    admin_pw = st.text_input("Admin password", type="password", key="admin_pw_input")
    if admin_pw == ADMIN_PASSWORD:
        st.session_state.admin_unlocked = True

    if st.session_state.admin_unlocked:
        st.success("Access granted")

        current_mode = db_get_comment_mode()
        new_mode = st.radio(
            "Comment display mode",
            options=["anonymous", "account_name"],
            index=0 if current_mode == "anonymous" else 1,
            format_func=lambda x: "Anonymous" if x == "anonymous" else "Show account name",
        )
        if new_mode != current_mode:
            db_set_comment_mode(new_mode)
            st.rerun()

        st.divider()
        st.subheader("Manage Posts")
        all_posts = db_get_all_posts()
        if not all_posts:
            st.info("No posts yet.")

        for post in all_posts:
            pid, platform, acc, title, content, created, media_type, media_data = post
            label = f"{acc} — {content[:40]}"
            with st.expander(label):
                st.caption(f"Posted: {created}")
                if media_type:
                    st.caption(f"Media: {media_type}")
                st.write(content[:200])
                if st.button("Delete post", key=f"del_post_{pid}", type="primary"):
                    db_delete_post(pid)
                    st.rerun()

                post_comments = db_get_comments(pid)
                if post_comments:
                    st.caption(f"{len(post_comments)} comment(s):")
                    by_parent = {}
                    for c in post_comments:
                        cid, cacc, ctext, _, parent_id, depth = c
                        by_parent.setdefault(parent_id, []).append(c)
                    for top_comment in by_parent.get(None, []):
                        _render_admin_comment(top_comment, by_parent, 0)

        st.divider()
        st.subheader("Danger Zone")
        if st.checkbox("I confirm: permanently delete ALL data"):
            if st.button("Reset entire database", type="primary", use_container_width=True):
                db_reset()
                st.success("Database has been reset.")
                st.rerun()


# ── Main page ─────────────────────────────────────────────────────────────────
st.title("📱 Course Social Media Feed")

# ── Before feed: post creation + unlock ──────────────────────────────────────
if not st.session_state.feed_unlocked:
    st.subheader("Create your post")
    show_post_form(key_prefix="pre_")

    st.markdown("---")
    st.subheader("Finished writing your post? Enter password here.")
    feed_pw = st.text_input("Feed password", type="password", key="feed_pw_input")
    if st.button("Unlock feed", use_container_width=True, type="primary", key="unlock_btn"):
        if feed_pw == FEED_PASSWORD:
            st.session_state.feed_unlocked = True
            st.rerun()
        else:
            st.error("Wrong password — please try again.")

# ── Feed ──────────────────────────────────────────────────────────────────────
else:
    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = time.time()
    if time.time() - st.session_state.last_refresh > 30:
        st.session_state.last_refresh = time.time()
        st.rerun()

    st.subheader("Class Feed")

    with st.expander("➕ Create another post"):
        show_post_form(key_prefix="feed_")

    st.write("")

    comment_mode = db_get_comment_mode()
    posts = db_get_all_posts()

    if not posts:
        st.info("No posts yet — be the first!")

    for post in posts:
        pid, platform, acc, title, content, created, media_type, media_data = post

        card_html = render_post_card(acc, content, media_type, media_data)
        st.markdown(card_html, unsafe_allow_html=True)

        render_interaction_buttons(pid, st.session_state.session_id)

        show_key = f"show_comments_{pid}"
        if st.session_state.show_comments.get(show_key, False):
            post_comments = db_get_comments(pid)
            if post_comments:
                render_comment_thread(
                    post_comments, comment_mode, pid,
                    st.session_state.session_id,
                )
            else:
                st.caption("No comments yet.")

            with st.form(key=f"comment_form_{pid}", clear_on_submit=True):
                new_comment = st.text_area(
                    "Add a comment",
                    max_chars=500,
                    placeholder="Add a comment…",
                    label_visibility="collapsed",
                )
                if st.form_submit_button("Post", use_container_width=True):
                    if new_comment.strip():
                        db_add_comment(
                            pid,
                            st.session_state.account_name or "Anonymous",
                            new_comment.strip(),
                        )
                        st.rerun()
                    else:
                        st.error("Comment cannot be empty.")

        st.write("")
