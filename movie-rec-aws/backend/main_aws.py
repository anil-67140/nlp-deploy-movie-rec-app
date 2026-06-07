"""
Movie Recommender - FastAPI Backend (AWS Enhanced)
Adds: Cognito JWT auth, S3 pre-signed URLs, CloudWatch logging,
      watchlist CRUD (stored in S3/JSON), favourites.
All original /home, /tmdb/search, /movie/* routes preserved.
"""

import os
import json
import pickle
import logging
import time
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

import numpy as np
import pandas as pd
import httpx
import boto3
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from dotenv import load_dotenv

# ------- Conditional CloudWatch logging -------
try:
    import watchtower
    CW_AVAILABLE = True
except ImportError:
    CW_AVAILABLE = False

# =========================
# ENV & CONFIG
# =========================
load_dotenv()
TMDB_API_KEY  = os.getenv("TMDB_API_KEY")
TMDB_BASE     = "https://api.themoviedb.org/3"
TMDB_IMG_500  = "https://image.tmdb.org/t/p/w500"
AWS_REGION    = os.getenv("AWS_REGION", "us-east-1")
ASSETS_BUCKET = os.getenv("ASSETS_BUCKET", "")
COGNITO_POOL  = os.getenv("COGNITO_POOL_ID", "")
COGNITO_CLIENT= os.getenv("COGNITO_CLIENT_ID", "")
ENVIRONMENT   = os.getenv("ENVIRONMENT", "development")

if not TMDB_API_KEY:
    raise RuntimeError("TMDB_API_KEY missing in environment")

# =========================
# LOGGING (CloudWatch in prod, stdout in dev)
# =========================
logger = logging.getLogger("movie-rec")
logger.setLevel(logging.INFO)

if ENVIRONMENT == "production" and CW_AVAILABLE:
    try:
        cw_handler = watchtower.CloudWatchLogHandler(
            log_group="/movie-rec/application",
            stream_name=f"fastapi-{datetime.utcnow().strftime('%Y-%m-%d')}",
            boto3_client=boto3.client("logs", region_name=AWS_REGION),
        )
        logger.addHandler(cw_handler)
    except Exception as e:
        print(f"CloudWatch logging setup failed (continuing): {e}")
else:
    logging.basicConfig(level=logging.INFO)

# =========================
# AWS CLIENTS (lazy - avoid cold start penalty)
# =========================
_s3 = None
_cognito = None
_dynamodb = None  # ADD THIS LINE

def get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3

def get_cognito():
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp", region_name=AWS_REGION)
    return _cognito

def get_dynamodb():                                          # ADD THIS FUNCTION
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb

# =========================
# FASTAPI APP
# =========================
app = FastAPI(title="Movie Recommender API", version="4.0-aws")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# =========================
# MODELS
# =========================
class TMDBMovieCard(BaseModel):
    tmdb_id: int
    title: str
    poster_url: Optional[str] = None
    release_date: Optional[str] = None
    vote_average: Optional[float] = None

class TMDBMovieDetails(BaseModel):
    tmdb_id: int
    title: str
    overview: Optional[str] = None
    release_date: Optional[str] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None
    genres: List[dict] = []

class TFIDFRecItem(BaseModel):
    title: str
    score: float
    tmdb: Optional[TMDBMovieCard] = None

class SearchBundleResponse(BaseModel):
    query: str
    movie_details: TMDBMovieDetails
    tfidf_recommendations: List[TFIDFRecItem]
    genre_recommendations: List[TMDBMovieCard]

class WatchlistItem(BaseModel):
    tmdb_id: int
    title: str
    poster_url: Optional[str] = None
    added_at: Optional[str] = None

class PresignedUrlResponse(BaseModel):
    upload_url: str
    file_key: str
    expires_in: int

class UserRegisterRequest(BaseModel):
    email: str
    password: str

class UserLoginRequest(BaseModel):
    email: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int

# =========================
# PICKLE GLOBALS
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

df: Optional[pd.DataFrame] = None
indices_obj: Any = None
tfidf_matrix: Any = None
tfidf_obj: Any = None
TITLE_TO_IDX: Optional[Dict[str, int]] = None

# =========================
# AUTH HELPERS
# =========================
def _norm_title(t: str) -> str:
    return str(t).strip().lower()

def make_img_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return f"{TMDB_IMG_500}{path}"

def verify_cognito_token(token: str) -> dict:
    """Verify JWT with Cognito. Returns claims or raises 401."""
    try:
        cog = get_cognito()
        response = cog.get_user(AccessToken=token)
        attrs = {a["Name"]: a["Value"] for a in response.get("UserAttributes", [])}
        return {"username": response["Username"], "email": attrs.get("email", "")}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        raise HTTPException(status_code=500, detail=f"Auth error: {code}")

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Optional[dict]:
    """Optional auth - returns user dict or None (for public endpoints)."""
    if not credentials or not COGNITO_POOL:
        return None
    return verify_cognito_token(credentials.credentials)

async def require_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """Mandatory auth - raises 401 if not authenticated."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    return verify_cognito_token(credentials.credentials)

# =========================
# TMDB HELPERS
# =========================
async def tmdb_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    q = dict(params)
    q["api_key"] = TMDB_API_KEY
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{TMDB_BASE}{path}", params=q)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {repr(e)}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"TMDB {r.status_code}: {r.text}")
    return r.json()

async def tmdb_cards_from_results(results: List[dict], limit: int = 20) -> List[TMDBMovieCard]:
    return [
        TMDBMovieCard(
            tmdb_id=int(m["id"]),
            title=m.get("title") or m.get("name") or "",
            poster_url=make_img_url(m.get("poster_path")),
            release_date=m.get("release_date"),
            vote_average=m.get("vote_average"),
        )
        for m in (results or [])[:limit]
    ]

async def tmdb_movie_details(movie_id: int) -> TMDBMovieDetails:
    data = await tmdb_get(f"/movie/{movie_id}", {"language": "en-US"})
    return TMDBMovieDetails(
        tmdb_id=int(data["id"]),
        title=data.get("title") or "",
        overview=data.get("overview"),
        release_date=data.get("release_date"),
        poster_url=make_img_url(data.get("poster_path")),
        backdrop_url=make_img_url(data.get("backdrop_path")),
        genres=data.get("genres", []) or [],
    )

async def tmdb_search_movies(query: str, page: int = 1) -> Dict[str, Any]:
    return await tmdb_get(
        "/search/movie",
        {"query": query, "include_adult": "false", "language": "en-US", "page": page},
    )

async def tmdb_search_first(query: str) -> Optional[dict]:
    data = await tmdb_search_movies(query=query, page=1)
    results = data.get("results", [])
    return results[0] if results else None

# =========================
# TF-IDF HELPERS
# =========================
def build_title_to_idx_map(indices: Any) -> Dict[str, int]:
    title_to_idx: Dict[str, int] = {}
    if isinstance(indices, dict):
        for k, v in indices.items():
            title_to_idx[_norm_title(k)] = int(v)
        return title_to_idx
    try:
        for k, v in indices.items():
            title_to_idx[_norm_title(k)] = int(v)
        return title_to_idx
    except Exception:
        raise RuntimeError("indices.pkl must be dict or pandas Series-like")

def get_local_idx_by_title(title: str) -> int:
    global TITLE_TO_IDX
    if TITLE_TO_IDX is None:
        raise HTTPException(status_code=500, detail="TF-IDF index map not initialized")
    key = _norm_title(title)
    if key in TITLE_TO_IDX:
        return int(TITLE_TO_IDX[key])
    raise HTTPException(status_code=404, detail=f"Title not found locally: '{title}'")

def tfidf_recommend_titles(query_title: str, top_n: int = 10) -> List[Tuple[str, float]]:
    global df, tfidf_matrix
    if df is None or tfidf_matrix is None:
        raise HTTPException(status_code=500, detail="TF-IDF resources not loaded")
    idx = get_local_idx_by_title(query_title)
    qv = tfidf_matrix[idx]
    scores = (tfidf_matrix @ qv.T).toarray().ravel()
    order = np.argsort(-scores)
    out: List[Tuple[str, float]] = []
    for i in order:
        if int(i) == int(idx):
            continue
        try:
            title_i = str(df.iloc[int(i)]["title"])
        except Exception:
            continue
        out.append((title_i, float(scores[int(i)])))
        if len(out) >= top_n:
            break
    return out

async def attach_tmdb_card_by_title(title: str) -> Optional[TMDBMovieCard]:
    try:
        m = await tmdb_search_first(title)
        if not m:
            return None
        return TMDBMovieCard(
            tmdb_id=int(m["id"]),
            title=m.get("title") or title,
            poster_url=make_img_url(m.get("poster_path")),
            release_date=m.get("release_date"),
            vote_average=m.get("vote_average"),
        )
    except Exception:
        return None

# =========================
# S3 WATCHLIST HELPERS
# =========================
# def _watchlist_key(user_email: str) -> str:
#     safe = user_email.replace("@", "_at_").replace(".", "_")
#     return f"watchlists/{safe}.json"

# def _get_watchlist_from_s3(user_email: str) -> List[dict]:
#     if not ASSETS_BUCKET:
#         return []
#     try:
#         s3 = get_s3()
#         obj = s3.get_object(Bucket=ASSETS_BUCKET, Key=_watchlist_key(user_email))
#         return json.loads(obj["Body"].read().decode())
#     except ClientError as e:
#         if e.response["Error"]["Code"] == "NoSuchKey":
#             return []
#         raise HTTPException(status_code=500, detail="S3 read error")

# def _save_watchlist_to_s3(user_email: str, items: List[dict]) -> None:
#     if not ASSETS_BUCKET:
#         return
#     try:
#         s3 = get_s3()
#         s3.put_object(
#             Bucket=ASSETS_BUCKET,
#             Key=_watchlist_key(user_email),
#             Body=json.dumps(items, ensure_ascii=False),
#             ContentType="application/json",
#         )
#     except ClientError as e:
#         raise HTTPException(status_code=500, detail=f"S3 write error: {e}")

# =========================
# DYNAMODB WATCHLIST HELPERS
# =========================
WATCHLIST_TABLE = "movie-watchlist"

def _get_watchlist_from_db(user_email: str) -> List[dict]:
    try:
        table = get_dynamodb().Table(WATCHLIST_TABLE)
        resp = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("user_id").eq(user_email)
        )
        return resp.get("Items", [])
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"DB read error: {e}")

def _add_to_db(user_email: str, item_dict: dict) -> None:
    try:
        table = get_dynamodb().Table(WATCHLIST_TABLE)
        table.put_item(Item={"user_id": user_email, "movie_id": str(item_dict["tmdb_id"]), **item_dict})
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"DB write error: {e}")

def _remove_from_db(user_email: str, tmdb_id: int) -> None:
    try:
        table = get_dynamodb().Table(WATCHLIST_TABLE)
        table.delete_item(Key={"user_id": user_email, "movie_id": str(tmdb_id)})
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"DB delete error: {e}")

# =========================
# STARTUP: LOAD PICKLES
# =========================
@app.on_event("startup")
def load_pickles():
    global df, indices_obj, tfidf_matrix, tfidf_obj, TITLE_TO_IDX
    start = time.time()
    logger.info("Loading ML models from disk...")
    with open(os.path.join(BASE_DIR, "df.pkl"), "rb") as f:
        df = pickle.load(f)
    with open(os.path.join(BASE_DIR, "indices.pkl"), "rb") as f:
        indices_obj = pickle.load(f)
    with open(os.path.join(BASE_DIR, "tfidf_matrix.pkl"), "rb") as f:
        tfidf_matrix = pickle.load(f)
    with open(os.path.join(BASE_DIR, "tfidf.pkl"), "rb") as f:
        tfidf_obj = pickle.load(f)
    TITLE_TO_IDX = build_title_to_idx_map(indices_obj)
    if df is None or "title" not in df.columns:
        raise RuntimeError("df.pkl must contain a DataFrame with a 'title' column")
    logger.info(f"ML models loaded in {time.time()-start:.2f}s. Dataset size: {len(df)}")

# =========================
# ROUTES
# =========================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": ENVIRONMENT,
        "dataset_size": len(df) if df is not None else 0,
        "timestamp": datetime.utcnow().isoformat(),
    }

# ---- AUTH ROUTES (Cognito) ----

@app.post("/auth/register", response_model=dict)
async def register(req: UserRegisterRequest):
    """Register a new user via AWS Cognito."""
    if not COGNITO_POOL or not COGNITO_CLIENT:
        raise HTTPException(status_code=501, detail="Auth not configured")
    try:
        cog = get_cognito()
        cog.sign_up(
            ClientId=COGNITO_CLIENT,
            Username=req.email,
            Password=req.password,
            UserAttributes=[{"Name": "email", "Value": req.email}],
        )
        logger.info(f"New user registered: {req.email}")
        return {"message": "Registration successful. Please verify your email."}
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "UsernameExistsException":
            raise HTTPException(status_code=409, detail="Email already registered")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/auth/login", response_model=TokenResponse)
async def login(req: UserLoginRequest):
    """Login and get Cognito JWT tokens."""
    if not COGNITO_POOL or not COGNITO_CLIENT:
        raise HTTPException(status_code=501, detail="Auth not configured")
    try:
        cog = get_cognito()
        result = cog.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=COGNITO_CLIENT,
            AuthParameters={"USERNAME": req.email, "PASSWORD": req.password},
        )
        auth = result["AuthenticationResult"]
        logger.info(f"User logged in: {req.email}")
        return TokenResponse(
            access_token=auth["AccessToken"],
            refresh_token=auth["RefreshToken"],
            expires_in=auth["ExpiresIn"],
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "NotAuthorizedException":
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if code == "UserNotConfirmedException":
            raise HTTPException(status_code=403, detail="Email not verified")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/auth/me")
async def me(user: dict = Depends(require_user)):
    """Get current authenticated user info."""
    return user

# ---- WATCHLIST ROUTES (User-specific, stored in S3) ----

# @app.get("/watchlist", response_model=List[WatchlistItem])
# async def get_watchlist(user: dict = Depends(require_user)):
#     """Get user's watchlist from S3."""
#     items = _get_watchlist_from_s3(user["email"])
#     return items
@app.get("/watchlist", response_model=List[WatchlistItem])
async def get_watchlist(user: dict = Depends(require_user)):
    """Get user's watchlist from DynamoDB."""
    items = _get_watchlist_from_db(user["email"])
    return items

# @app.post("/watchlist", response_model=dict)
# async def add_to_watchlist(item: WatchlistItem, user: dict = Depends(require_user)):
#     """Add movie to watchlist (stored in S3)."""
#     items = _get_watchlist_from_s3(user["email"])
#     if any(i["tmdb_id"] == item.tmdb_id for i in items):
#         return {"message": "Already in watchlist"}
#     item_dict = item.dict()
#     item_dict["added_at"] = datetime.utcnow().isoformat()
#     items.append(item_dict)
#     _save_watchlist_to_s3(user["email"], items)
#     logger.info(f"User {user['email']} added movie {item.tmdb_id} to watchlist")
#     return {"message": "Added to watchlist", "count": len(items)}
@app.post("/watchlist", response_model=dict)
async def add_to_watchlist(item: WatchlistItem, user: dict = Depends(require_user)):
    """Add movie to watchlist (stored in DynamoDB)."""
    existing = _get_watchlist_from_db(user["email"])
    if any(i["tmdb_id"] == item.tmdb_id for i in existing):
        return {"message": "Already in watchlist"}
    item_dict = item.dict()
    item_dict["added_at"] = datetime.utcnow().isoformat()
    _add_to_db(user["email"], item_dict)
    logger.info(f"User {user['email']} added movie {item.tmdb_id} to watchlist")
    return {"message": "Added to watchlist"}

# @app.delete("/watchlist/{tmdb_id}", response_model=dict)
# async def remove_from_watchlist(tmdb_id: int, user: dict = Depends(require_user)):
#     """Remove movie from watchlist."""
#     items = _get_watchlist_from_s3(user["email"])
#     before = len(items)
#     items = [i for i in items if i["tmdb_id"] != tmdb_id]
#     if len(items) == before:
#         raise HTTPException(status_code=404, detail="Movie not in watchlist")
#     _save_watchlist_to_s3(user["email"], items)
#     return {"message": "Removed from watchlist", "count": len(items)}
@app.delete("/watchlist/{tmdb_id}", response_model=dict)
async def remove_from_watchlist(tmdb_id: int, user: dict = Depends(require_user)):
    """Remove movie from watchlist."""
    existing = _get_watchlist_from_db(user["email"])
    if not any(i["tmdb_id"] == tmdb_id for i in existing):
        raise HTTPException(status_code=404, detail="Movie not in watchlist")
    _remove_from_db(user["email"], tmdb_id)
    return {"message": "Removed from watchlist"}

# ---- S3 PRE-SIGNED URL (for file upload) ----

@app.post("/upload/presign", response_model=PresignedUrlResponse)
async def presign_upload(
    filename: str = Query(...),
    content_type: str = Query("image/jpeg"),
    user: dict = Depends(require_user),
):
    """Generate a pre-signed URL for direct S3 upload (user avatars, exports)."""
    if not ASSETS_BUCKET:
        raise HTTPException(status_code=501, detail="S3 not configured")
    safe_email = user["email"].replace("@", "_at_").replace(".", "_")
    file_key = f"uploads/{safe_email}/{int(time.time())}_{filename}"
    try:
        s3 = get_s3()
        url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": ASSETS_BUCKET,
                "Key": file_key,
                "ContentType": content_type,
            },
            ExpiresIn=300,  # 5 minutes
        )
        logger.info(f"Generated presigned URL for {user['email']}: {file_key}")
        return PresignedUrlResponse(upload_url=url, file_key=file_key, expires_in=300)
    except ClientError as e:
        raise HTTPException(status_code=500, detail=f"Could not generate URL: {e}")

@app.get("/upload/download-url")
async def presign_download(
    file_key: str = Query(...),
    user: dict = Depends(require_user),
):
    """Generate a pre-signed download URL."""
    if not ASSETS_BUCKET:
        raise HTTPException(status_code=501, detail="S3 not configured")
    try:
        s3 = get_s3()
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": ASSETS_BUCKET, "Key": file_key},
            ExpiresIn=3600,
        )
        return {"download_url": url, "expires_in": 3600}
    except ClientError as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---- ORIGINAL MOVIE ROUTES (unchanged) ----

@app.get("/home", response_model=List[TMDBMovieCard])
async def home(
    category: str = Query("popular"),
    limit: int = Query(24, ge=1, le=50),
):
    logger.info(f"Home feed requested: category={category}")
    try:
        if category == "trending":
            data = await tmdb_get("/trending/movie/day", {"language": "en-US"})
            return await tmdb_cards_from_results(data.get("results", []), limit=limit)
        if category not in {"popular", "top_rated", "upcoming", "now_playing"}:
            raise HTTPException(status_code=400, detail="Invalid category")
        data = await tmdb_get(f"/movie/{category}", {"language": "en-US", "page": 1})
        return await tmdb_cards_from_results(data.get("results", []), limit=limit)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Home route failed: {e}")

@app.get("/tmdb/search")
async def tmdb_search(
    query: str = Query(..., min_length=1),
    page: int = Query(1, ge=1, le=10),
):
    logger.info(f"TMDB search: query={query}")
    return await tmdb_search_movies(query=query, page=page)

@app.get("/movie/id/{tmdb_id}", response_model=TMDBMovieDetails)
async def movie_details_route(tmdb_id: int):
    return await tmdb_movie_details(tmdb_id)

@app.get("/recommend/genre", response_model=List[TMDBMovieCard])
async def recommend_genre(
    tmdb_id: int = Query(...),
    limit: int = Query(18, ge=1, le=50),
):
    details = await tmdb_movie_details(tmdb_id)
    if not details.genres:
        return []
    genre_id = details.genres[0]["id"]
    discover = await tmdb_get(
        "/discover/movie",
        {"with_genres": genre_id, "language": "en-US", "sort_by": "popularity.desc", "page": 1},
    )
    cards = await tmdb_cards_from_results(discover.get("results", []), limit=limit)
    return [c for c in cards if c.tmdb_id != tmdb_id]

@app.get("/recommend/tfidf")
async def recommend_tfidf(
    title: str = Query(..., min_length=1),
    top_n: int = Query(10, ge=1, le=50),
):
    recs = tfidf_recommend_titles(title, top_n=top_n)
    return [{"title": t, "score": s} for t, s in recs]

@app.get("/movie/search", response_model=SearchBundleResponse)
async def search_bundle(
    query: str = Query(..., min_length=1),
    tfidf_top_n: int = Query(12, ge=1, le=30),
    genre_limit: int = Query(12, ge=1, le=30),
):
    best = await tmdb_search_first(query)
    if not best:
        raise HTTPException(status_code=404, detail=f"No TMDB movie found: {query}")
    tmdb_id = int(best["id"])
    details = await tmdb_movie_details(tmdb_id)

    tfidf_items: List[TFIDFRecItem] = []
    recs: List[Tuple[str, float]] = []
    try:
        recs = tfidf_recommend_titles(details.title, top_n=tfidf_top_n)
    except Exception:
        try:
            recs = tfidf_recommend_titles(query, top_n=tfidf_top_n)
        except Exception:
            recs = []

    for title, score in recs:
        card = await attach_tmdb_card_by_title(title)
        tfidf_items.append(TFIDFRecItem(title=title, score=score, tmdb=card))

    genre_recs: List[TMDBMovieCard] = []
    if details.genres:
        genre_id = details.genres[0]["id"]
        discover = await tmdb_get(
            "/discover/movie",
            {"with_genres": genre_id, "language": "en-US", "sort_by": "popularity.desc", "page": 1},
        )
        cards = await tmdb_cards_from_results(discover.get("results", []), limit=genre_limit)
        genre_recs = [c for c in cards if c.tmdb_id != details.tmdb_id]

    logger.info(f"Search bundle: query={query}, tfidf={len(tfidf_items)}, genre={len(genre_recs)}")
    return SearchBundleResponse(
        query=query,
        movie_details=details,
        tfidf_recommendations=tfidf_items,
        genre_recommendations=genre_recs,
    )
