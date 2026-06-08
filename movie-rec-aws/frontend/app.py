"""
Movie Recommender - Streamlit Frontend (AWS Enhanced)
Adds: Login/Register with Cognito, Watchlist management, S3 file upload.
All original home/search/details pages preserved.
"""

import requests
import streamlit as st
from typing import Optional

# =============================
# CONFIG
# =============================
import os
API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000")
TMDB_IMG = "https://image.tmdb.org/t/p/w500"

st.set_page_config(page_title="Movie Recommender", page_icon="🎬", layout="wide")

# =============================
# STYLES
# =============================
st.markdown("""
<style>
.block-container { padding-top: 1rem; padding-bottom: 2rem; max-width: 1400px; }
.small-muted { color:#6b7280; font-size: 0.92rem; }
.movie-title { font-size: 0.9rem; line-height: 1.15rem; height: 2.3rem; overflow: hidden; }
.card { border: 1px solid rgba(0,0,0,0.08); border-radius: 16px; padding: 14px; background: rgba(255,255,255,0.7); }
.auth-box { max-width: 400px; margin: 0 auto; }
</style>
""", unsafe_allow_html=True)

# =============================
# SESSION STATE
# =============================
for key, default in [
    ("view", "home"),
    ("selected_tmdb_id", None),
    ("access_token", None),
    ("user_email", None),
    ("auth_view", "login"),  # login | register
]:
    if key not in st.session_state:
        st.session_state[key] = default

# =============================
# API HELPERS
# =============================
def auth_headers() -> dict:
    if st.session_state.access_token:
        return {"Authorization": f"Bearer {st.session_state.access_token}"}
    return {}

@st.cache_data(ttl=30)
def api_get_json(path: str, params: dict | None = None, _token: str = ""):
    try:
        headers = {"Authorization": f"Bearer {_token}"} if _token else {}
        r = requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=25)
        if r.status_code >= 400:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        return r.json(), None
    except Exception as e:
        return None, f"Request failed: {e}"

def api_post(path: str, payload: dict) -> tuple:
    try:
        r = requests.post(f"{API_BASE}{path}", json=payload, timeout=25)
        return r.json(), None if r.status_code < 400 else r.json().get("detail", "Error")
    except Exception as e:
        return None, str(e)

def api_post_auth(path: str, payload: dict = None, params: dict = None) -> tuple:
    try:
        r = requests.post(
            f"{API_BASE}{path}", json=payload, params=params,
            headers=auth_headers(), timeout=25
        )
        if r.status_code >= 400:
            return None, r.json().get("detail", "Error")
        return r.json(), None
    except Exception as e:
        return None, str(e)

def api_delete_auth(path: str) -> tuple:
    try:
        r = requests.delete(f"{API_BASE}{path}", headers=auth_headers(), timeout=15)
        if r.status_code >= 400:
            return None, r.json().get("detail", "Error")
        return r.json(), None
    except Exception as e:
        return None, str(e)

def api_get_auth(path: str, params: dict = None) -> tuple:
    try:
        r = requests.get(f"{API_BASE}{path}", params=params, headers=auth_headers(), timeout=25)
        if r.status_code >= 400:
            return None, r.json().get("detail", "Error")
        return r.json(), None
    except Exception as e:
        return None, str(e)

# =============================
# NAVIGATION
# =============================
def goto_home():
    st.session_state.view = "home"
    st.session_state.selected_tmdb_id = None
    st.query_params.clear()
    st.rerun()

def goto_details(tmdb_id: int):
    st.session_state.view = "details"
    st.session_state.selected_tmdb_id = int(tmdb_id)
    st.query_params["view"] = "details"
    st.query_params["id"] = str(int(tmdb_id))
    st.rerun()

def goto_watchlist():
    st.session_state.view = "watchlist"
    st.rerun()

def goto_upload():
    st.session_state.view = "upload"
    st.rerun()

def goto_auth():
    st.session_state.view = "auth"
    st.rerun()

# Read query params on page load
qp_view = st.query_params.get("view")
qp_id   = st.query_params.get("id")
if qp_view in ("home", "details") and st.session_state.view == "home":
    st.session_state.view = qp_view
if qp_id:
    try:
        st.session_state.selected_tmdb_id = int(qp_id)
        st.session_state.view = "details"
    except Exception:
        pass

# =============================
# POSTER GRID
# =============================
def poster_grid(cards, cols=6, key_prefix="grid"):
    if not cards:
        st.info("No movies to show.")
        return
    rows = (len(cards) + cols - 1) // cols
    idx = 0
    for r in range(rows):
        colset = st.columns(cols)
        for c in range(cols):
            if idx >= len(cards):
                break
            m = cards[idx]; idx += 1
            tmdb_id = m.get("tmdb_id")
            title   = m.get("title", "Untitled")
            poster  = m.get("poster_url")
            with colset[c]:
                if poster:
                    st.image(poster, use_container_width=True)
                else:
                    st.write("🖼️ No poster")
                if st.button("Open", key=f"{key_prefix}_{r}_{c}_{idx}_{tmdb_id}"):
                    if tmdb_id:
                        goto_details(tmdb_id)
                st.markdown(f"<div class='movie-title'>{title}</div>", unsafe_allow_html=True)

def to_cards_from_tfidf_items(tfidf_items):
    cards = []
    for x in tfidf_items or []:
        tmdb = x.get("tmdb") or {}
        if tmdb.get("tmdb_id"):
            cards.append({
                "tmdb_id": tmdb["tmdb_id"],
                "title": tmdb.get("title") or x.get("title") or "Untitled",
                "poster_url": tmdb.get("poster_url"),
            })
    return cards

def parse_tmdb_search_to_cards(data, keyword: str, limit: int = 24):
    keyword_l = keyword.strip().lower()
    if isinstance(data, dict) and "results" in data:
        raw = data.get("results") or []
        raw_items = [
            {
                "tmdb_id": int(m["id"]),
                "title": (m.get("title") or "").strip(),
                "poster_url": f"{TMDB_IMG}{m['poster_path']}" if m.get("poster_path") else None,
                "release_date": m.get("release_date", ""),
            }
            for m in raw if m.get("title") and m.get("id")
        ]
    elif isinstance(data, list):
        raw_items = [
            {
                "tmdb_id": int(m.get("tmdb_id") or m.get("id")),
                "title": (m.get("title") or "").strip(),
                "poster_url": m.get("poster_url"),
                "release_date": m.get("release_date", ""),
            }
            for m in data if (m.get("tmdb_id") or m.get("id")) and m.get("title")
        ]
    else:
        return [], []

    matched = [x for x in raw_items if keyword_l in x["title"].lower()]
    final_list = matched if matched else raw_items

    suggestions = []
    for x in final_list[:10]:
        year = (x.get("release_date") or "")[:4]
        label = f"{x['title']} ({year})" if year else x["title"]
        suggestions.append((label, x["tmdb_id"]))

    cards = [{"tmdb_id": x["tmdb_id"], "title": x["title"], "poster_url": x["poster_url"]} for x in final_list[:limit]]
    return suggestions, cards

# =============================
# SIDEBAR
# =============================
with st.sidebar:
    st.markdown("## 🎬 Menu")
    if st.button("🏠 Home"):
        goto_home()

    if st.session_state.access_token:
        st.success(f"👤 {st.session_state.user_email}")
        if st.button("📋 Watchlist"):
            goto_watchlist()
        if st.button("📤 Upload"):
            goto_upload()
        if st.button("🚪 Logout"):
            st.session_state.access_token = None
            st.session_state.user_email   = None
            goto_home()
    else:
        if st.button("🔐 Login / Register"):
            goto_auth()

    st.markdown("---")
    st.markdown("### 🏠 Home Feed")
    home_category = st.selectbox(
        "Category",
        ["trending", "popular", "top_rated", "now_playing", "upcoming"],
        index=0,
    )
    grid_cols = st.slider("Grid columns", 4, 8, 6)

# =============================
# HEADER
# =============================
st.title("🎬 Movie Recommender")
st.markdown("<div class='small-muted'>NLP-powered movie recommendations on AWS</div>", unsafe_allow_html=True)
st.divider()

# =====================================================
# VIEW: AUTH (Login / Register)
# =====================================================
if st.session_state.view == "auth":
    st.markdown("## 🔐 Account")
    tab_login, tab_register = st.tabs(["Login", "Register"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")
        if submitted:
            if not email or not password:
                st.error("Please fill in all fields.")
            else:
                data, err = api_post("/auth/login", {"email": email, "password": password})
                if err:
                    st.error(f"Login failed: {err}")
                elif data:
                    st.session_state.access_token = data.get("access_token")
                    st.session_state.user_email   = email
                    st.success("Logged in! Redirecting...")
                    goto_home()

    with tab_register:
        with st.form("register_form"):
            email_r = st.text_input("Email", key="reg_email")
            pass_r  = st.text_input("Password (min 8 chars, include a number)", type="password", key="reg_pass")
            submitted_r = st.form_submit_button("Register")
        if submitted_r:
            if not email_r or not pass_r:
                st.error("Please fill in all fields.")
            else:
                data, err = api_post("/auth/register", {"email": email_r, "password": pass_r})
                if err:
                    st.error(f"Registration failed: {err}")
                else:
                    st.success("Registered! Check your email to verify, then log in.")

# =====================================================
# VIEW: WATCHLIST
# =====================================================
elif st.session_state.view == "watchlist":
    if not st.session_state.access_token:
        st.warning("Please log in to view your watchlist.")
        goto_auth()

    st.markdown("## 📋 My Watchlist")
    if st.button("← Back"):
        goto_home()

    items, err = api_get_auth("/watchlist")
    if err:
        st.error(f"Could not load watchlist: {err}")
    elif not items:
        st.info("Your watchlist is empty. Open any movie and add it!")
    else:
        cols = st.columns(6)
        for i, item in enumerate(items):
            with cols[i % 6]:
                if item.get("poster_url"):
                    st.image(item["poster_url"], use_container_width=True)
                st.markdown(f"**{item.get('title','?')}**")
                added = (item.get("added_at") or "")[:10]
                if added:
                    st.caption(f"Added: {added}")
                if st.button("Remove", key=f"rm_{item['tmdb_id']}"):
                    _, err2 = api_delete_auth(f"/watchlist/{item['tmdb_id']}")
                    if err2:
                        st.error(err2)
                    else:
                        st.success("Removed!")
                        st.rerun()

# =====================================================
# VIEW: UPLOAD (S3 Pre-signed URL demo)
# =====================================================
elif st.session_state.view == "upload":
    if not st.session_state.access_token:
        st.warning("Please log in to upload files.")
        goto_auth()

    st.markdown("## 📤 Upload File to S3")
    st.info("Upload any file to your personal S3 folder using a pre-signed URL.")
    if st.button("← Back"):
        goto_home()

    uploaded_file = st.file_uploader("Choose a file", type=["jpg", "jpeg", "png", "pdf", "csv"])
    if uploaded_file is not None:
        content_type_map = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "pdf": "application/pdf", "csv": "text/csv"
        }
        ext = uploaded_file.name.split(".")[-1].lower()
        ctype = content_type_map.get(ext, "application/octet-stream")

        if st.button("Upload to S3"):
            # 1. Get pre-signed URL from backend
            data, err = api_post_auth(
                "/upload/presign",
                params={"filename": uploaded_file.name, "content_type": ctype}
            )
            if err:
                st.error(f"Could not get upload URL: {err}")
            else:
                upload_url = data["upload_url"]
                file_key   = data["file_key"]
                # 2. Upload directly to S3 (browser -> S3, bypasses EC2)
                file_bytes = uploaded_file.read()
                try:
                    resp = requests.put(
                        upload_url,
                        data=file_bytes,
                        headers={"Content-Type": ctype},
                        timeout=60
                    )
                    if resp.status_code in (200, 204):
                        st.success(f"✅ Uploaded successfully! File key: `{file_key}`")
                        st.code(file_key)
                    else:
                        st.error(f"Upload failed: {resp.status_code}")
                except Exception as e:
                    st.error(f"Upload error: {e}")

# =====================================================
# VIEW: HOME
# =====================================================
elif st.session_state.view == "home":
    typed = st.text_input("Search by movie title (keyword)", placeholder="Type: avenger, batman, love...")
    st.divider()

    if typed.strip():
        if len(typed.strip()) < 2:
            st.caption("Type at least 2 characters for suggestions.")
        else:
            data, err = api_get_json("/tmdb/search", params={"query": typed.strip()})
            if err or data is None:
                st.error(f"Search failed: {err}")
            else:
                suggestions, cards = parse_tmdb_search_to_cards(data, typed.strip(), limit=24)
                if suggestions:
                    labels   = ["-- Select a movie --"] + [s[0] for s in suggestions]
                    selected = st.selectbox("Suggestions", labels, index=0)
                    if selected != "-- Select a movie --":
                        label_to_id = {s[0]: s[1] for s in suggestions}
                        goto_details(label_to_id[selected])
                else:
                    st.info("No suggestions found. Try another keyword.")
                st.markdown("### Results")
                poster_grid(cards, cols=grid_cols, key_prefix="search_results")
        st.stop()

    st.markdown(f"### 🏠 Home — {home_category.replace('_',' ').title()}")
    home_cards, err = api_get_json("/home", params={"category": home_category, "limit": 24})
    if err or not home_cards:
        st.error(f"Home feed failed: {err or 'Unknown error'}")
        st.stop()
    poster_grid(home_cards, cols=grid_cols, key_prefix="home_feed")

# =====================================================
# VIEW: DETAILS
# =====================================================
elif st.session_state.view == "details":
    tmdb_id = st.session_state.selected_tmdb_id
    if not tmdb_id:
        st.warning("No movie selected.")
        if st.button("← Back to Home"):
            goto_home()
        st.stop()

    a, b = st.columns([3, 1])
    with a:
        st.markdown("### 📄 Movie Details")
    with b:
        if st.button("← Back to Home"):
            goto_home()

    data, err = api_get_json(f"/movie/id/{tmdb_id}")
    if err or not data:
        st.error(f"Could not load details: {err or 'Unknown error'}")
        st.stop()

    left, right = st.columns([1, 2.4], gap="large")
    with left:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        if data.get("poster_url"):
            st.image(data["poster_url"], use_container_width=True)
        else:
            st.write("🖼️ No poster")
        # Watchlist button
        if st.session_state.access_token:
            if st.button("➕ Add to Watchlist"):
                payload = {
                    "tmdb_id": tmdb_id,
                    "title": data.get("title", ""),
                    "poster_url": data.get("poster_url"),
                }
                result, err2 = api_post_auth("/watchlist", payload)
                if err2:
                    st.error(err2)
                else:
                    st.success(result.get("message", "Added!"))
        else:
            st.caption("Login to add to watchlist")
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.markdown("<div class='card'>", unsafe_allow_html=True)
        st.markdown(f"## {data.get('title','')}")
        release = data.get("release_date") or "-"
        genres  = ", ".join([g["name"] for g in data.get("genres", [])]) or "-"
        st.markdown(f"<div class='small-muted'>Release: {release}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='small-muted'>Genres: {genres}</div>", unsafe_allow_html=True)
        st.markdown("---")
        st.markdown("### Overview")
        st.write(data.get("overview") or "No overview available.")
        st.markdown("</div>", unsafe_allow_html=True)

    if data.get("backdrop_url"):
        st.markdown("#### Backdrop")
        st.image(data["backdrop_url"], use_container_width=True)

    st.divider()
    st.markdown("### ✅ Recommendations")

    title = (data.get("title") or "").strip()
    if title:
        bundle, err2 = api_get_json(
            "/movie/search",
            params={"query": title, "tfidf_top_n": 12, "genre_limit": 12},
        )
        if not err2 and bundle:
            st.markdown("#### 🔎 Similar Movies (TF-IDF NLP)")
            poster_grid(to_cards_from_tfidf_items(bundle.get("tfidf_recommendations")), cols=grid_cols, key_prefix="details_tfidf")
            st.markdown("#### 🎭 More Like This (Genre)")
            poster_grid(bundle.get("genre_recommendations", []), cols=grid_cols, key_prefix="details_genre")
        else:
            genre_only, err3 = api_get_json("/recommend/genre", params={"tmdb_id": tmdb_id, "limit": 18})
            if not err3 and genre_only:
                poster_grid(genre_only, cols=grid_cols, key_prefix="details_genre_fallback")
            else:
                st.warning("No recommendations available right now.")
    else:
        st.warning("No title available to compute recommendations.")
