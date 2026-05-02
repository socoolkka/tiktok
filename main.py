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
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent, GiftEvent, LikeEvent, RoomUserSeqEvent

MAX_HISTORY    = 100
CHECK_INTERVAL = 15
RETRY_ON_ERROR = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class LiveSession:
    def __init__(self, username: str):
        self.username      = username
        self.is_live       = False
        self.live_since    = None
        self.viewer_count  = 0
        self.total_coins   = 0
        self.total_likes   = 0
        self.comments      = deque(maxlen=MAX_HISTORY)
        self.like_ranking: dict = {}
        self.gift_ranking: dict = {}
        self.ws_clients: Set[WebSocket] = set()
        self.task          = None
        self.running       = True

    async def broadcast(self, data: dict):
        dead = set()
        msg = json.dumps(data, ensure_ascii=False)
        for ws in self.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self.ws_clients.difference_update(dead)

    async def check_is_live(self) -> bool:
        try:
            client = TikTokLiveClient(unique_id=self.username)
            return bool(await client.is_live())
        except Exception as e:
            logger.warning(f"[{self.username}] check_is_live: {e}")
            return False

    async def run_live(self):
        client = TikTokLiveClient(unique_id=self.username)

        @client.on(ConnectEvent)
        async def on_connect(event):
            self.is_live = True
            self.live_since = time.time()
            logger.info(f"[{self.username}] ✓ 接続成功")
            await self.broadcast({"type": "status", "live": True, "message": "ライブ配信に接続しました"})

        @client.on(DisconnectEvent)
        async def on_disconnect(event):
            self.is_live = False
            self.live_since = None
            logger.info(f"[{self.username}] 切断")
            await self.broadcast({"type": "status", "live": False, "message": "配信が終了しました。再確認中..."})

        @client.on(CommentEvent)
        async def on_comment(event):
            try:
                avatar = ""
                try:
                    urls = getattr(event.user.avatar_thumb, "url_list", None)
                    if urls: avatar = str(urls[0])
                except: pass
                entry = {
                    "type":      "comment",
                    "user":      event.user.nickname or "?",
                    "user_id":   str(event.user.unique_id or ""),
                    "avatar":    avatar,
                    "comment":   event.comment or "",
                    "timestamp": time.time(),
                }
                self.comments.append(entry)
                await self.broadcast(entry)
            except Exception as e:
                logger.warning(f"[{self.username}] on_comment: {e}")

        @client.on(GiftEvent)
        async def on_gift(event):
            try:
                diamond = getattr(event.m_gift, "diamond_count", 0) or 0
                repeat  = getattr(event, "repeat_count", 1) or 1
                coins   = diamond * repeat
                self.total_coins += coins

                # ギフトランキング更新
                uid  = str(event.from_user.unique_id or "")
                name = event.from_user.nickname or "?"
                gift_name = getattr(event.m_gift, "name", "ギフト")
                if uid not in self.gift_ranking:
                    self.gift_ranking[uid] = {"user": name, "coins": 0}
                self.gift_ranking[uid]["coins"] += coins
                self.gift_ranking[uid]["user"]   = name

                top3 = self._get_gift_top3()
                await self.broadcast({
                    "type":           "gift",
                    "user":           name,
                    "gift_name":      gift_name,
                    "coins":          coins,
                    "total_coins":    self.total_coins,
                    "gift_top3":      top3,
                })
            except Exception as e:
                logger.warning(f"[{self.username}] on_gift: {e}")

        @client.on(LikeEvent)
        async def on_like(event):
            try:
                self.total_likes = event.total or self.total_likes
                uid  = str(event.user.unique_id or "")
                name = event.user.nickname or "?"
                count = getattr(event, "count", 1) or 1
                if uid not in self.like_ranking:
                    self.like_ranking[uid] = {"user": name, "likes": 0}
                self.like_ranking[uid]["likes"] += count
                self.like_ranking[uid]["user"]   = name

                top3 = self._get_like_top3()
                await self.broadcast({
                    "type":        "like",
                    "total_likes": self.total_likes,
                    "top3":        top3,
                })
            except Exception as e:
                logger.warning(f"[{self.username}] on_like: {e}")

        @client.on(RoomUserSeqEvent)
        async def on_viewer(event):
            try:
                self.viewer_count = event.m_total or 0
                await self.broadcast({"type": "viewers", "viewer_count": self.viewer_count})
            except Exception as e:
                logger.warning(f"[{self.username}] on_viewer: {e}")

        try:
            await client.connect()
        except Exception as e:
            logger.warning(f"[{self.username}] 接続エラー: {e}")
        finally:
            self.is_live      = False
            self.live_since   = None
            self.viewer_count = 0
            self.total_coins  = 0
            self.total_likes  = 0
            self.like_ranking = {}
            self.gift_ranking = {}

    def _get_like_top3(self):
        return sorted(self.like_ranking.values(), key=lambda x: x["likes"], reverse=True)[:3]

    def _get_gift_top3(self):
        return sorted(self.gift_ranking.values(), key=lambda x: x["coins"], reverse=True)[:3]

    async def watcher(self):
        logger.info(f"[{self.username}] 監視開始")
        while self.running:
            try:
                live = await self.check_is_live()
                if live:
                    logger.info(f"[{self.username}] ライブ検知")
                    await self.broadcast({"type": "status", "live": False, "message": "ライブを検知しました。接続中..."})
                    await self.run_live()
                    await self.broadcast({"type": "status", "live": False, "message": f"配信終了。{CHECK_INTERVAL}秒後に再確認します"})
                else:
                    await self.broadcast({"type": "status", "live": False, "message": "現在ライブ配信はありません。自動確認中..."})
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.username}] エラー: {e}")
                await asyncio.sleep(RETRY_ON_ERROR)

    def start(self):
        self.task = asyncio.create_task(self.watcher())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass


sessions: Dict[str, LiveSession] = {}
session_last_access: Dict[str, float] = {}
SESSION_TIMEOUT = 3600

async def get_or_create_session(username: str) -> LiveSession:
    key = username.lower().lstrip("@")
    session_last_access[key] = time.time()
    if key not in sessions:
        logger.info(f"新規セッション: {key}")
        session = LiveSession("@" + key)
        sessions[key] = session
        session.start()
    return sessions[key]

async def cleanup_sessions():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        dead = [k for k, t in session_last_access.items() if now - t > SESSION_TIMEOUT]
        for k in dead:
            if k in sessions:
                await sessions[k].stop()
                del sessions[k]
                del session_last_access[k]
                logger.info(f"セッション削除: {k}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(cleanup_sessions())
    yield
    task.cancel()
    for s in sessions.values():
        await s.stop()


app = FastAPI(title="TikTok Live Comment API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TikTok Live Viewer</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a12; color:#e0d8ff; font-family:'Segoe UI',sans-serif; min-height:100vh; display:flex; align-items:center; justify-content:center; }
  #inputScreen { display:flex; flex-direction:column; align-items:center; gap:20px; }
  #inputScreen h1 { font-size:22px; color:#fff; }
  #inputScreen p { font-size:13px; color:#7870aa; }
  #userInput { background:#16162a; color:#e0d8ff; border:1.5px solid #4b3bc8; border-radius:12px; padding:10px 16px; font-size:15px; width:260px; outline:none; text-align:center; }
  #userInput:focus { border-color:#6450ff; }
  #startBtn { background:#6450ff; color:#fff; border:none; border-radius:12px; padding:10px 32px; font-size:14px; font-weight:bold; cursor:pointer; width:260px; }
  #errMsg { color:#ff6060; font-size:12px; min-height:16px; }
  #mainScreen { display:none; flex-direction:column; width:100%; max-width:480px; height:100vh; }
  #header { background:#14142a; border-bottom:1px solid #4b3bc8; padding:10px 14px; display:flex; align-items:center; gap:10px; }
  #dot { width:9px; height:9px; border-radius:50%; background:#c33; flex-shrink:0; }
  #dot.live { background:#2dd770; animation:pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  #headerTitle { font-size:13px; color:#fff; font-weight:bold; }
  #headerUser { font-size:11px; color:#6450ff; background:#1a1430; border-radius:6px; padding:2px 8px; }
  #changeBtn { margin-left:auto; font-size:11px; color:#7870aa; background:none; border:1px solid #333; border-radius:6px; padding:2px 8px; cursor:pointer; }
  #statsBar { background:#10101e; border-bottom:1px solid #2a2a44; padding:6px 14px; display:flex; }
  .stat { flex:1; text-align:center; font-size:11px; }
  .stat.viewers span { color:#a0a0c8; }
  .stat.likes span { color:#ff5096; }
  .stat.coins span { color:#ffcd32; }
  #statusBar { background:#10101e; border-bottom:1px solid #2a2a44; padding:5px 14px; display:flex; align-items:center; gap:7px; }
  #statusDot { width:7px; height:7px; border-radius:50%; background:#c33; flex-shrink:0; }
  #statusDot.live { background:#2dd770; }
  #statusMsg { font-size:10px; color:#7870aa; }
  #log { flex:1; overflow-y:auto; padding:8px 10px; display:flex; flex-direction:column; gap:5px; }
  .entry { background:#16162a; border-radius:8px; padding:6px 9px; border-left:3px solid #6450ff; font-size:12px; animation:fadeIn 0.2s; }
  .entry.gift { border-left-color:#ffcd32; }
  .name { color:#6450ff; font-weight:bold; margin-right:5px; }
  .gift .name { color:#ffcd32; }
  .msg { color:#ccc8ee; }
  #overlay { position:absolute; inset:0; background:rgba(8,6,16,0.92); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; z-index:10; }
  #overlayIcon { font-size:40px; }
  #overlayTitle { font-size:16px; font-weight:bold; }
  #overlaySub { font-size:11px; color:#7870aa; }
  #overlayCountdown { font-size:11px; color:#ffa828; }
  #mainScreen { position:relative; }
  @keyframes fadeIn { from{opacity:0;transform:translateY(3px)} to{opacity:1} }
</style>
</head>
<body>
<div id="inputScreen">
  <h1>⟡ TikTok Live Viewer</h1>
  <p>TikTokのユーザー名を入力してください</p>
  <input id="userInput" type="text" placeholder="@username" autocomplete="off" autocapitalize="none">
  <div id="errMsg"></div>
  <button id="startBtn">接続する</button>
</div>
<div id="mainScreen">
  <div id="header">
    <div id="dot"></div>
    <span id="headerTitle">⟡ TikTok Live</span>
    <span id="headerUser"></span>
    <button id="changeBtn">変更</button>
  </div>
  <div id="statsBar" style="display:none">
    <div class="stat viewers">👁 <span id="viewerVal">0</span></div>
    <div class="stat likes">♥ <span id="likeVal">0</span></div>
    <div class="stat coins">🪙 <span id="coinVal">0</span></div>
  </div>
  <div id="statusBar">
    <div id="statusDot"></div>
    <span id="statusMsg">接続確認中...</span>
  </div>
  <div id="log"></div>
  <div id="overlay">
    <div id="overlayIcon">📵</div>
    <div id="overlayTitle">ライブ配信なし</div>
    <div id="overlaySub">自動で検知します...</div>
    <div id="overlayCountdown"></div>
  </div>
</div>
<script>
const inputScreen=document.getElementById('inputScreen'),mainScreen=document.getElementById('mainScreen'),userInput=document.getElementById('userInput'),startBtn=document.getElementById('startBtn'),errMsg=document.getElementById('errMsg'),headerUser=document.getElementById('headerUser'),changeBtn=document.getElementById('changeBtn'),dot=document.getElementById('dot'),statsBar=document.getElementById('statsBar'),viewerVal=document.getElementById('viewerVal'),likeVal=document.getElementById('likeVal'),coinVal=document.getElementById('coinVal'),statusDot=document.getElementById('statusDot'),statusMsg=document.getElementById('statusMsg'),log=document.getElementById('log'),overlay=document.getElementById('overlay'),overlayTitle=document.getElementById('overlayTitle'),overlaySub=document.getElementById('overlaySub'),overlayCountdown=document.getElementById('overlayCountdown')
let currentUser='',seenTotal=-1,isLive=false,checkInterval=15,loopTimer=null,countdown=0
function setStatus(live,msg){isLive=live;dot.className=live?'live':'';statusDot.className=live?'live':'';statusMsg.textContent=msg||(live?'ライブ配信中':'未配信');statsBar.style.display=live?'flex':'none';if(live){overlay.style.display='none'}else{overlay.style.display='flex';overlayTitle.textContent='ライブ配信なし';overlaySub.textContent=msg||'自動で検知します...'}}
function addEntry(data){const isGift=data.type==='gift';const div=document.createElement('div');div.className='entry'+(isGift?' gift':'');const name=document.createElement('span');name.className='name';name.textContent=(isGift?'🪙 ':'')+(data.user||'?');const msg=document.createElement('span');msg.className='msg';msg.textContent=isGift?(data.gift_name||'ギフト')+' ('+(data.coins||0)+' コイン)':(data.comment||'');div.appendChild(name);div.appendChild(msg);log.appendChild(div);log.scrollTop=log.scrollHeight;while(log.children.length>60)log.removeChild(log.firstChild)}
async function fetchStatus(){try{const res=await fetch('/status?user='+encodeURIComponent(currentUser));const d=await res.json();checkInterval=d.check_interval||15;if(d.live){setStatus(true,'ライブ配信中 ✓');viewerVal.textContent=d.viewer_count||0;likeVal.textContent=d.total_likes||0;coinVal.textContent=d.total_coins||0}else{setStatus(false,'現在ライブ配信はありません')}}catch{setStatus(false,'サーバーエラー')}}
async function fetchComments(){if(!isLive)return;try{const res=await fetch('/comments?user='+encodeURIComponent(currentUser)+'&limit=50');const d=await res.json();if(!d||!d.comments)return;const serverTotal=d.total||0;if(seenTotal===-1){seenTotal=serverTotal;return}const newCount=serverTotal-seenTotal;if(newCount<=0)return;const comments=d.comments;const startIdx=Math.max(0,comments.length-newCount);for(let i=startIdx;i<comments.length;i++)addEntry(comments[i]);seenTotal=serverTotal}catch{}}
function startViewer(username){currentUser=username.replace(/^@/,'');seenTotal=-1;isLive=false;log.innerHTML='';headerUser.textContent='@'+currentUser;inputScreen.style.display='none';mainScreen.style.display='flex';if(loopTimer)clearInterval(loopTimer);fetchStatus();fetchComments();loopTimer=setInterval(()=>{fetchStatus();fetchComments();},checkInterval*1000);countdown=checkInterval;setInterval(()=>{if(!isLive){countdown--;if(countdown<=0)countdown=checkInterval;overlayCountdown.textContent='次の確認まで約'+countdown+'秒'}else{overlayCountdown.textContent='';countdown=checkInterval}},1000)}
startBtn.onclick=()=>{const val=userInput.value.trim();if(!val){errMsg.textContent='ユーザー名を入力してください';return}errMsg.textContent='';startViewer(val)}
userInput.addEventListener('keydown',e=>{if(e.key==='Enter')startBtn.click()})
changeBtn.onclick=()=>{if(loopTimer)clearInterval(loopTimer);mainScreen.style.display='none';inputScreen.style.display='flex';userInput.value=''}
</script>
</body>
</html>
""")


@app.get("/status")
async def get_status(user: str = Query(...)):
    session = await get_or_create_session(user)
    return {
        "live":           session.is_live,
        "target":         session.username,
        "buffered":       len(session.comments),
        "live_since":     session.live_since,
        "check_interval": CHECK_INTERVAL,
        "viewer_count":   session.viewer_count,
        "total_coins":    session.total_coins,
        "total_likes":    session.total_likes,
        "like_top3":      session._get_like_top3(),
        "gift_top3":      session._get_gift_top3(),
    }


@app.get("/comments")
async def get_comments(user: str = Query(...), limit: int = 50):
    session = await get_or_create_session(user)
    items = list(session.comments)[-limit:]
    return {"comments": items, "total": len(session.comments)}


@app.get("/likes/ranking")
async def get_like_ranking(user: str = Query(...)):
    session = await get_or_create_session(user)
    return {"ranking": session._get_like_top3()}


@app.get("/gifts/ranking")
async def get_gift_ranking(user: str = Query(...)):
    session = await get_or_create_session(user)
    return {"ranking": session._get_gift_top3()}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, user: str = Query(...)):
    await ws.accept()
    session = await get_or_create_session(user)
    session.ws_clients.add(ws)
    await ws.send_text(json.dumps({
        "type":     "init",
        "live":     session.is_live,
        "comments": list(session.comments),
        "message":  "ライブ配信中" if session.is_live else "現在ライブ配信はありません。自動確認中...",
    }, ensure_ascii=False))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        session.ws_clients.discard(ws)
