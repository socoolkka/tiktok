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
from fastapi.responses import HTMLResponse, JSONResponse
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, ConnectEvent, DisconnectEvent, GiftEvent, LikeEvent, RoomUserSeqEvent

# ── Config ───────────────────────────────────────────────────────────────────
TARGET_USER    = "@exploiterkaisetusub"
MAX_HISTORY    = 100
CHECK_INTERVAL = 15
RETRY_ON_ERROR = 30

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
viewer_count:    int = 0
total_coins:     int = 0
total_likes:     int = 0

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


async def check_is_live() -> bool:
    try:
        client = TikTokLiveClient(unique_id=TARGET_USER)
        result = await client.is_live()
        return bool(result)
    except Exception as e:
        logger.warning(f"[check_is_live] {e}")
        return False


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
        await broadcast({"type": "status", "live": True, "message": "ライブ配信に接続しました"})

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        global is_live, live_start_time
        is_live = False
        live_start_time = None
        logger.info("[TikTok] 配信終了 / 切断")
        await broadcast({"type": "status", "live": False, "message": "配信が終了しました。再確認中..."})

    @client.on(CommentEvent)
    async def on_comment(event: CommentEvent):
        try:
            avatar = ""
            try:
                urls = getattr(event.user.avatar_thumb, "url_list", None)
                if urls:
                    avatar = str(urls[0])
            except Exception:
                pass
            entry = {
                "type":      "comment",
                "user":      event.user.nickname or "?",
                "user_id":   str(event.user.unique_id or ""),
                "avatar":    avatar,
                "comment":   event.comment or "",
                "timestamp": time.time(),
            }
            comment_history.append(entry)
            await broadcast(entry)
        except Exception as e:
            logger.warning(f"[on_comment] スキップ: {e}")

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        global total_coins
        try:
            diamond = getattr(event.m_gift, "diamond_count", 0) or 0
            repeat  = getattr(event, "repeat_count", 1) or 1
            coins   = diamond * repeat
            total_coins += coins
            await broadcast({
                "type":        "gift",
                "user":        event.from_user.nickname or "?",
                "gift_name":   getattr(event.m_gift, "name", "ギフト"),
                "coins":       coins,
                "total_coins": total_coins,
            })
        except Exception as e:
            logger.warning(f"[on_gift] スキップ: {e}")

    @client.on(LikeEvent)
    async def on_like(event: LikeEvent):
        global total_likes
        try:
            total_likes = event.total or total_likes
            await broadcast({"type": "like", "total_likes": total_likes})
        except Exception as e:
            logger.warning(f"[on_like] スキップ: {e}")

    @client.on(RoomUserSeqEvent)
    async def on_viewer(event: RoomUserSeqEvent):
        global viewer_count
        try:
            viewer_count = event.m_total or 0
            await broadcast({"type": "viewers", "viewer_count": viewer_count})
        except Exception as e:
            logger.warning(f"[on_viewer] スキップ: {e}")

    try:
        await client.connect()
    except Exception as e:
        logger.warning(f"[run_live_session] 接続エラー: {e}")
    finally:
        is_live = False
        live_start_time = None
        current_client  = None
        viewer_count    = 0
        total_coins     = 0
        total_likes     = 0


async def watcher_loop() -> None:
    global is_live
    logger.info("[Watcher] 監視ループ開始")
    while True:
        try:
            logger.info(f"[Watcher] ライブ確認中... ({TARGET_USER})")
            live_now = await check_is_live()
            if live_now:
                logger.info("[Watcher] ライブ検知！接続します")
                await broadcast({"type": "status", "live": False, "message": "ライブを検知しました。接続中..."})
                await run_live_session()
                logger.info(f"[Watcher] セッション終了。{CHECK_INTERVAL}秒後に再確認")
                await broadcast({"type": "status", "live": False, "message": f"配信終了。{CHECK_INTERVAL}秒後に再確認します"})
                await asyncio.sleep(CHECK_INTERVAL)
            else:
                logger.info(f"[Watcher] 未ライブ。{CHECK_INTERVAL}秒後に再確認")
                await broadcast({"type": "status", "live": False, "message": "現在ライブ配信はありません。自動確認中..."})
                await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[Watcher] キャンセルされました")
            break
        except Exception as e:
            logger.error(f"[Watcher] 予期しないエラー: {e}")
            await asyncio.sleep(RETRY_ON_ERROR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(watcher_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="TikTok Live Comment API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>TikTok Live 読み上げ</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#0a0a12; color:#e0d8ff; font-family:'Segoe UI',sans-serif; display:flex; flex-direction:column; height:100vh; overflow:hidden; }
  #header { background:#14142a; border-bottom:1px solid #4b3bc8; padding:12px 16px; display:flex; align-items:center; gap:12px; }
  #header h1 { font-size:14px; color:#fff; }
  #dot { width:10px; height:10px; border-radius:50%; background:#c33; flex-shrink:0; }
  #dot.live { background:#2dd770; animation:pulse 1.2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  #statusEl { font-size:11px; color:#7870aa; margin-left:auto; }
  #controls { background:#10101e; padding:8px 16px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; border-bottom:1px solid #2a2a44; }
  #controls label { font-size:11px; color:#7870aa; }
  select, input[type=range] { background:#1a1a2e; color:#e0d8ff; border:1px solid #4b3bc8; border-radius:6px; padding:3px 6px; font-size:11px; }
  #startBtn { background:#2dd770; color:#000; border:none; border-radius:8px; padding:6px 16px; font-size:12px; font-weight:bold; cursor:pointer; }
  #toggleTTS { background:#6450ff; color:#fff; border:none; border-radius:8px; padding:5px 14px; font-size:12px; cursor:pointer; font-weight:bold; }
  #toggleTTS.off { background:#444; }
  #debugEl { font-size:10px; color:#ff8040; margin-left:8px; }
  #log { flex:1; overflow-y:auto; padding:10px 14px; display:flex; flex-direction:column; gap:6px; }
  .entry { background:#16162a; border-radius:10px; padding:7px 10px; border-left:3px solid #6450ff; font-size:12px; animation:fadeIn 0.2s ease; }
  .entry.gift { border-left-color:#ffcd32; }
  .name { color:#6450ff; font-weight:bold; margin-right:6px; }
  .gift .name { color:#ffcd32; }
  .msg { color:#ccc8ee; }
  .speaking { color:#2dd770; font-size:10px; margin-left:6px; }
  @keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1} }
</style>
</head>
<body>
<div id="header">
  <div id="dot"></div>
  <h1>⟡ TikTok Live 読み上げ</h1>
  <span id="statusEl">接続中...</span>
</div>
<div id="controls">
  <button id="startBtn">▶ 読み上げ開始</button>
  <label>声:</label>
  <select id="voiceSelect"></select>
  <label>速さ:</label>
  <input id="rateRange" type="range" min="0.5" max="2" step="0.1" value="1.1">
  <span id="rateVal" style="font-size:11px;color:#7870aa">1.1</span>
  <label>音量:</label>
  <input id="volRange" type="range" min="0" max="1" step="0.05" value="1">
  <span id="volVal" style="font-size:11px;color:#7870aa">1.0</span>
  <button id="toggleTTS">🔊 ON</button>
  <span id="debugEl"></span>
</div>
<div id="log"></div>

<script>
const dot       = document.getElementById('dot')
const statusEl  = document.getElementById('statusEl')
const log       = document.getElementById('log')
const voiceSel  = document.getElementById('voiceSelect')
const rateRange = document.getElementById('rateRange')
const rateVal   = document.getElementById('rateVal')
const volRange  = document.getElementById('volRange')
const volVal    = document.getElementById('volVal')
const toggleBtn = document.getElementById('toggleTTS')
const startBtn  = document.getElementById('startBtn')
const debugEl   = document.getElementById('debugEl')

let ttsEnabled  = false
let seenTotal   = -1
let isLive      = false
let voices      = []
let ttsQueue    = []
let ttsBusy     = false
let started     = false

function debug(msg) { debugEl.textContent = msg }

function loadVoices() {
  voices = speechSynthesis.getVoices()
  voiceSel.innerHTML = ''
  let jaIndex = 0
  voices.forEach((v, i) => {
    const opt = document.createElement('option')
    opt.value = i
    opt.textContent = v.name + ' (' + v.lang + ')'
    if (v.lang.startsWith('ja')) { opt.selected = true; jaIndex = i }
    voiceSel.appendChild(opt)
  })
  debug('声: ' + voices.length + '個')
}
speechSynthesis.onvoiceschanged = loadVoices
loadVoices()

// ── 必ずユーザー操作で最初の発話をトリガー ──────────────────
startBtn.onclick = () => {
  if (started) return
  started = true
  ttsEnabled = true
  toggleBtn.textContent = '🔊 ON'
  toggleBtn.className = ''
  startBtn.style.background = '#444'
  startBtn.textContent = '✓ 開始済み'

  // Safari対策：無音の発話でTTSを初期化
  const init = new SpeechSynthesisUtterance(' ')
  init.volume = 0
  init.onend = () => {
    debug('TTS初期化完了')
    speakNext()
  }
  speechSynthesis.speak(init)
}

function speakNext() {
  if (ttsBusy || ttsQueue.length === 0 || !ttsEnabled || !started) return
  ttsBusy = true
  const { text, span } = ttsQueue.shift()
  debug('読み上げ中: ' + text.slice(0, 20))

  const utter = new SpeechSynthesisUtterance(text)
  const vi = parseInt(voiceSel.value)
  utter.voice  = voices[vi] || null
  utter.rate   = parseFloat(rateRange.value)
  utter.volume = parseFloat(volRange.value)
  utter.lang   = 'ja-JP'

  if (span) { span.innerHTML = ' <span class="speaking">🔊</span>' }

  utter.onend = () => {
    ttsBusy = false
    if (span) span.innerHTML = ''
    debug('完了 残り' + ttsQueue.length + '件')
    speakNext()
  }
  utter.onerror = (e) => {
    ttsBusy = false
    if (span) span.innerHTML = ''
    debug('エラー: ' + e.error)
    speakNext()
  }
  speechSynthesis.speak(utter)
}

function queueTTS(text, span) {
  if (ttsQueue.length > 10) ttsQueue.shift() // 溜まりすぎたら古いものを捨てる
  ttsQueue.push({ text, span })
  if (started) speakNext()
}

function addEntry(data) {
  const isGift = data.type === 'gift'
  const div = document.createElement('div')
  div.className = 'entry' + (isGift ? ' gift' : '')
  const name = document.createElement('span')
  name.className = 'name'
  name.textContent = (isGift ? '🪙 ' : '') + (data.user || '?')
  const msg = document.createElement('span')
  msg.className = 'msg'
  msg.textContent = isGift
    ? (data.gift_name || 'ギフト') + ' (' + (data.coins || 0) + ' コイン)'
    : (data.comment || '')
  const ind = document.createElement('span')
  div.appendChild(name); div.appendChild(msg); div.appendChild(ind)
  log.appendChild(div)
  log.scrollTop = log.scrollHeight
  while (log.children.length > 60) log.removeChild(log.firstChild)

  const ttsText = isGift
    ? (data.user + 'が' + (data.gift_name || 'ギフト') + 'を' + (data.coins || 0) + 'コイン送りました')
    : (data.user + '、' + data.comment)
  queueTTS(ttsText, ind)
}

function setStatus(live, msg) {
  isLive = live
  dot.className = live ? 'live' : ''
  statusEl.textContent = msg || (live ? 'ライブ配信中' : '未配信')
}

async function fetchStatus() {
  try {
    const res = await fetch('/status')
    const d = await res.json()
    setStatus(d.live, d.live ? 'ライブ配信中 ✓' : '現在ライブ配信はありません')
  } catch { setStatus(false, 'サーバーエラー') }
}

async function fetchComments() {
  if (!isLive) return
  try {
    const res = await fetch('/comments?limit=50')
    const d = await res.json()
    if (!d || !d.comments) return
    const serverTotal = d.total || 0
    if (seenTotal === -1) { seenTotal = serverTotal; return }
    const newCount = serverTotal - seenTotal
    if (newCount <= 0) return
    const comments = d.comments
    const startIdx = Math.max(0, comments.length - newCount)
    for (let i = startIdx; i < comments.length; i++) addEntry(comments[i])
    seenTotal = serverTotal
  } catch {}
}

rateRange.oninput = () => rateVal.textContent = rateRange.value
volRange.oninput  = () => volVal.textContent  = parseFloat(volRange.value).toFixed(2)
toggleBtn.onclick = () => {
  ttsEnabled = !ttsEnabled
  toggleBtn.textContent = ttsEnabled ? '🔊 ON' : '🔇 OFF'
  toggleBtn.className   = ttsEnabled ? '' : 'off'
  if (!ttsEnabled) speechSynthesis.cancel()
}

async function loop() {
  await fetchStatus()
  await fetchComments()
  setTimeout(loop, 5000)
}
loop()
</script>
</body>
</html>
""")


@app.get("/status")
async def get_status():
    return {
        "live":           is_live,
        "target":         TARGET_USER,
        "buffered":       len(comment_history),
        "live_since":     live_start_time,
        "check_interval": CHECK_INTERVAL,
        "viewer_count":   viewer_count,
        "total_coins":    total_coins,
        "total_likes":    total_likes,
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
