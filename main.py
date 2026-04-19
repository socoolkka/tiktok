"""
TikTok Live Comment API Server
────────────────────────────────────────────────────────────
Render などの常時稼働環境向け。

・ライブ未開始 → 一定間隔でポーリングして自動検知
・ライブ開始   → TikTokLive で接続しコメントを配信
・ライブ終了   → 自動切断 → 再ポーリングループへ戻る
────────────────────────────────────────────────────────────
"""

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent

# ── Config ───────────────────────────────────────────────────────────────────
TARGET_USER    = "@exploiterkaisetusubu"
MAX_HISTORY    = 100
CHECK_INTERVAL = 15     # ライブ未開始時の確認間隔（秒）
RETRY_ON_ERROR = 30     # エラー後の待機秒数

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Shared State ─────────────────────────────────────────────────────────────
comment_history: deque[dict] = deque(maxlen=MAX_HISTORY)
active_ws:       Set[WebSocket] = set()
is_live          = False
live_start_time: float | None  = None
current_client:  TikTokLiveClient | None = None

# ── WebSocket broadcast ───────────────────────────────────────────────────────
async def broadcast(data: dict) -> None:
    dead: Set[WebSocket] = set()
    msg = json.dumps(data, ensure_ascii=False)
    for ws in active_ws:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    active_ws.difference_update(dead)


# ── ライブ中かどうかを確認 ────────────────────────────────────────────────
async def check_is_live() -> bool:
    try:
        client = TikTokLiveClient(unique_id=TARGET_USER)
        result = await client.is_live()
        return bool(result)
    except Exception as e:
        logger.warning(f"[check_is_live] {e}")
        return False


# ── TikTokLive 接続セッション ─────────────────────────────────────────────
async def run_live_session() -> None:
    global is_live, live_start_time, current_client

    client = TikTokLiveClient(unique_id=TARGET_USER)
    current_client = client

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        global is_live, live_start_time
        is_live = True
        live_start_time = time.time()
        logger.info(f"[TikTok] ✓ 接続成功 → {TARGET_USER}")
        await broadcast({
            "type":    "status",
            "live":    True,
            "message": "ライブ配信に接続しました",
        })

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        global is_live, live_start_time
        is_live = False
        live_start_time = None
        logger.info("[TikTok] 配信終了 / 切断")
        await broadcast({
            "type":    "status",
            "live":    False,
            "message": "配信が終了しました。再確認中...",
        })

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        entry = {
            "type":      "comment",
            "user":      event.user.nickname,
            "user_id":   str(event.user.unique_id),
            "avatar":    (
                str(event.user.avatar_thumb.url_list[0])
                if event.user.avatar_thumb and event.user.avatar_thumb.url_list
                else ""
            ),
            "comment":   event.comment,
            "timestamp": time.time(),
        }
        comment_history.append(entry)
        await broadcast(entry)

    try:
        await client.connect()
    except Exception as e:
        logger.warning(f"[run_live_session] 接続エラー: {e}")
    finally:
        is_live = False
        live_start_time = None
        current_client  = None


# ── メイン監視ループ ──────────────────────────────────────────────────────
async def watcher_loop() -> None:
    global is_live
    logger.info("[Watcher] 監視ループ開始")

    while True:
        try:
            logger.info(f"[Watcher] ライブ確認中... ({TARGET_USER})")
            live_now = await check_is_live()

            if live_now:
                logger.info("[Watcher] ライブ検知！接続します")
                await broadcast({
                    "type":    "status",
                    "live":    False,
                    "message": "ライブを検知しました。接続中...",
                })
                await run_live_session()
                logger.info(f"[Watcher] セッション終了。{CHECK_INTERVAL}秒後に再確認")
                await broadcast({
                    "type":    "status",
                    "live":    False,
                    "message": f"配信終了。{CHECK_INTERVAL}秒後に再確認します",
                })
                await asyncio.sleep(CHECK_INTERVAL)

            else:
                logger.info(f"[Watcher] 未ライブ。{CHECK_INTERVAL}秒後に再確認")
                await broadcast({
                    "type":    "status",
                    "live":    False,
                    "message": "現在ライブ配信はありません。自動確認中...",
                })
                await asyncio.sleep(CHECK_INTERVAL)

        except asyncio.CancelledError:
            logger.info("[Watcher] キャンセルされました")
            break
        except Exception as e:
            logger.error(f"[Watcher] 予期しないエラー: {e}")
            await asyncio.sleep(RETRY_ON_ERROR)


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(watcher_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="TikTok Live Comment API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/status")
async def get_status():
    return {
        "live":           is_live,
        "target":         TARGET_USER,
        "buffered":       len(comment_history),
        "live_since":     live_start_time,
        "check_interval": CHECK_INTERVAL,
    }


@app.get("/comments")
async def get_comments(limit: int = 50):
    items = list(comment_history)[-limit:]
    return {"comments": items, "total": len(comment_history)}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    active_ws.add(ws)

    await ws.send_text(json.dumps({
        "type":     "init",
        "live":     is_live,
        "comments": list(comment_history),
        "message":  "ライブ配信中" if is_live else "現在ライブ配信はありません。自動確認中...",
    }, ensure_ascii=False))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        active_ws.discard(ws)
