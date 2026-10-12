"""
Asha AI: Telegram + WhatsApp bot server (alag chalne wali chhoti service).

Streamlit app webhook receive nahi kar sakta, isliye bot ke liye ye file kisi free host par chalayein
(Render / Railway / Hugging Face Space / koi VPS). Start command:
    uvicorn bot_server:app --host 0.0.0.0 --port $PORT

Environment variables (host ke "Environment" / Secrets me daalein):
  AI keys (kam se kam ek):  GROQ_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
  Telegram:  TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET (koi bhi lamba random text)
  WhatsApp:  WHATSAPP_TOKEN, WHATSAPP_PHONE_ID, WHATSAPP_VERIFY_TOKEN (aapka chuna hua text), WHATSAPP_APP_SECRET
  Optional:  BOT_RATE_PER_HOUR (default 20), BOT_NAME (default Asha)
"""
import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque

import requests
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Asha AI bot")

G = lambda k, d="": os.getenv(k, d).strip()  # noqa: E731
RATE = int(G("BOT_RATE_PER_HOUR", "20") or 20)
NAME = G("BOT_NAME", "Asha")
SYSTEM = (
    f"You are {NAME}, a warm, helpful Indian AI companion chatting on a messaging app. Reply in the same language the user writes in "
    "(Hindi, Hinglish, English or any other). Keep answers short and clear, a few sentences, plain text without markdown tables. "
    "Help with study, business, work, health basics, money basics and daily problems. Be honest: if you are not sure, say so. "
    "Never help with violence, weapons, hacking, fraud, sexual content, or tracking or identifying people. "
    "If someone seems in danger or distress, be kind and mention India's emergency number 112 and Tele-MANAS 14416. "
    "Never ask for passwords, OTPs or card details."
)

_lock = threading.Lock()
_hist = defaultdict(lambda: deque(maxlen=10))   # chat id -> last messages
_hits = defaultdict(deque)                       # chat id -> timestamps


def rate_ok(uid: str) -> bool:
    now = time.time()
    with _lock:
        q = _hits[uid]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE:
            return False
        q.append(now)
        return True


def chunks(text: str, n: int = 3800) -> list:
    text = (text or "").strip() or "..."
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out + [text]


# ---------------- AI providers (pehla jo chale) ----------------
def _openai_style(url: str, key: str, model: str, msgs: list) -> str:
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=60,
                      json={"model": model, "messages": msgs, "temperature": 0.6, "max_tokens": 700})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _gemini(key: str, msgs: list) -> str:
    model = G("GEMINI_MODEL", "gemini-2.5-flash")
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in msgs if m["role"] != "system"]
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": key}, timeout=60,
                      json={"system_instruction": {"parts": [{"text": SYSTEM}]}, "contents": contents})
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def ask_ai(uid: str, text: str) -> str:
    with _lock:
        _hist[uid].append({"role": "user", "content": text[:4000]})
        msgs = [{"role": "system", "content": SYSTEM}] + list(_hist[uid])
    chain = []
    if G("GROQ_API_KEY"):
        chain.append(lambda: _openai_style("https://api.groq.com/openai/v1/chat/completions", G("GROQ_API_KEY"),
                                           G("GROQ_MODEL", "llama-3.3-70b-versatile"), msgs))
    if G("GEMINI_API_KEY"):
        chain.append(lambda: _gemini(G("GEMINI_API_KEY"), msgs))
    if G("OPENROUTER_API_KEY"):
        chain.append(lambda: _openai_style("https://openrouter.ai/api/v1/chat/completions", G("OPENROUTER_API_KEY"),
                                           G("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"), msgs))
    for fn in chain:
        try:
            ans = fn()
            if ans:
                with _lock:
                    _hist[uid].append({"role": "assistant", "content": ans[:4000]})
                return ans
        except Exception as e:
            print("provider fail:", str(e)[:150])
    with _lock:
        if _hist[uid]:
            _hist[uid].pop()  # fail hua sawal history me na rahe
    return "Abhi mujhe jawab dene me dikkat aa rahi hai. Thodi der baad dobara likhiye. 🙏"


def handle_text(uid: str, text: str) -> str:
    t = (text or "").strip()
    if t.lower() in ("/start", "start", "hi", "hello", "namaste"):
        return f"Namaste! Main {NAME} hoon, aapki AI saathi. Padhai, business, kaam ya koi bhi sawal poochhiye. 😊"
    if t.lower() in ("/reset", "reset", "/new"):
        with _lock:
            _hist.pop(uid, None)
        return "Nayi baat shuru karte hain. Poochhiye! ✨"
    if not rate_ok(uid):
        return "Aapne ek ghante me bahut messages bhej diye. Thodi der baad dobara likhiye. 🙏"
    return ask_ai(uid, t)


# ---------------- Telegram ----------------
def tg_send(chat_id, text: str) -> None:
    tok = G("TELEGRAM_BOT_TOKEN")
    for part in chunks(text):
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=20,
                          json={"chat_id": chat_id, "text": part})
        except Exception as e:
            print("telegram send fail:", str(e)[:120])


def tg_work(chat_id, text: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{G('TELEGRAM_BOT_TOKEN')}/sendChatAction", timeout=10,
                      json={"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass
    tg_send(chat_id, handle_text(f"tg:{chat_id}", text))


@app.post("/telegram")
async def telegram_hook(request: Request, bg: BackgroundTasks):
    secret = G("TELEGRAM_WEBHOOK_SECRET")
    if not G("TELEGRAM_BOT_TOKEN") or not secret:
        raise HTTPException(503, "Telegram set nahi hai")
    if not hmac.compare_digest(request.headers.get("x-telegram-bot-api-secret-token", ""), secret):
        raise HTTPException(403, "bad secret")
    upd = await request.json()
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if chat is not None:
        txt = msg.get("text")
        if txt:
            bg.add_task(tg_work, chat, txt)
        else:
            bg.add_task(tg_send, chat, "Abhi main sirf likhe hue message samajh sakti hoon. Apna sawal likhkar bhejiye. 🙂")
    return {"ok": True}


# ---------------- WhatsApp Cloud API ----------------
def wa_send(to: str, text: str) -> None:
    pid, tok = G("WHATSAPP_PHONE_ID"), G("WHATSAPP_TOKEN")
    for part in chunks(text, 3500):
        try:
            r = requests.post(f"https://graph.facebook.com/v20.0/{pid}/messages", timeout=20,
                              headers={"Authorization": f"Bearer {tok}"},
                              json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": part}})
            if r.status_code >= 300:
                print("whatsapp send fail:", r.status_code, r.text[:150])
        except Exception as e:
            print("whatsapp send fail:", str(e)[:120])


def wa_work(sender: str, text: str) -> None:
    wa_send(sender, handle_text(f"wa:{sender}", text))


def wa_signature_ok(body: bytes, header: str) -> bool:
    secret = G("WHATSAPP_APP_SECRET")
    if not secret:
        return False
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header or "")


@app.get("/whatsapp")
async def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and G("WHATSAPP_VERIFY_TOKEN") and q.get("hub.verify_token") == G("WHATSAPP_VERIFY_TOKEN"):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verify fail")


@app.post("/whatsapp")
async def wa_hook(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    if not wa_signature_ok(raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "bad signature")
    data = await request.json()
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            for m in (ch.get("value") or {}).get("messages", []) or []:
                sender = m.get("from")
                if not sender:
                    continue
                if m.get("type") == "text":
                    bg.add_task(wa_work, sender, (m.get("text") or {}).get("body", ""))
                else:
                    bg.add_task(wa_send, sender, "Abhi main sirf likhe hue message samajh sakti hoon. Apna sawal likhkar bhejiye. 🙂")
    return {"ok": True}


@app.get("/")
def home():
    return {"service": "Asha AI bot", "telegram": bool(G("TELEGRAM_BOT_TOKEN")), "whatsapp": bool(G("WHATSAPP_TOKEN"))}


# ---------------- Telegram webhook apne aap set ho (haath se link kholne ki zarurat nahi) ----------------
def _auto_webhook() -> None:
    url = G("PUBLIC_URL") or G("RENDER_EXTERNAL_URL")
    tok, sec = G("TELEGRAM_BOT_TOKEN"), G("TELEGRAM_WEBHOOK_SECRET")
    if not (url and tok and sec):
        print("auto webhook skipped: URL / token / secret me se kuch missing hai")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/setWebhook", timeout=20,
                          json={"url": url.rstrip("/") + "/telegram", "secret_token": sec,
                                "allowed_updates": ["message", "edited_message"]})
        print("telegram setWebhook:", r.status_code, r.text[:150])
    except Exception as e:
        print("setWebhook fail:", str(e)[:120])


threading.Thread(target=_auto_webhook, daemon=True).start()
