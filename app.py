"""
LINE Webhook Server - 社長メモ用
LINEから送られたメッセージを受信し、Supabaseに永続保存。
同期スクリプトで取得可能にする。レポートをLINEに送信する機能も備える。

v2: /tmp保存 → Supabase永続化（Renderリスタートでデータ消失を防止）
"""
import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, abort
import requests as http_requests

app = Flask(__name__)

# Supabase設定（環境変数から取得）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")  # service_role key

# フォールバック: Supabase未設定時は従来の/tmp保存
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/line_inbox")
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.json")
USER_FILE = os.path.join(DATA_DIR, "user.json")

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
API_KEY = os.environ.get("API_KEY", "")

JST = timezone(timedelta(hours=9))


def use_supabase():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


# --- Supabase版のメッセージ操作 ---

def sb_insert_message(msg):
    resp = http_requests.post(
        f"{SUPABASE_URL}/rest/v1/line_messages",
        headers=supabase_headers(),
        json=msg,
        timeout=10,
    )
    return resp.status_code in (200, 201)


def sb_get_unsynced():
    resp = http_requests.get(
        f"{SUPABASE_URL}/rest/v1/line_messages?synced=eq.false&order=created_at.asc",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    return []


def sb_mark_synced(message_ids):
    for mid in message_ids:
        http_requests.patch(
            f"{SUPABASE_URL}/rest/v1/line_messages?message_id=eq.{mid}",
            headers=supabase_headers(),
            json={"synced": True},
            timeout=10,
        )


# --- 従来の/tmp版（フォールバック） ---

def load_messages():
    if os.path.exists(MESSAGES_FILE):
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_messages(messages):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


# --- ユーザーID管理 ---

def save_user_id(user_id):
    # Supabase使用時も/tmpに保存（軽量なので問題なし）
    # + 環境変数にフォールバックあり
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USER_FILE, "w") as f:
        json.dump({"user_id": user_id}, f)


def load_user_id():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            uid = json.load(f).get("user_id", "")
            if uid:
                return uid
    return os.environ.get("LINE_USER_ID", "")


# --- LINE API ---

def verify_signature(body, signature):
    digest = hmac.new(
        CHANNEL_SECRET.encode(), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def reply_to_line(reply_token, text):
    if not CHANNEL_ACCESS_TOKEN:
        return
    http_requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        },
        json={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        },
        timeout=5,
    )


def push_to_line(user_id, text):
    if not CHANNEL_ACCESS_TOKEN or not user_id:
        return False
    chunks = []
    while text:
        chunks.append(text[:5000])
        text = text[5000:]
    messages = [{"type": "text", "text": c} for c in chunks[:5]]
    resp = http_requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        },
        json={"to": user_id, "messages": messages},
        timeout=10,
    )
    return resp.status_code == 200


# --- エンドポイント ---

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(403)

    data = json.loads(body)

    if use_supabase():
        for event in data.get("events", []):
            user_id = event.get("source", {}).get("userId", "")
            if user_id:
                save_user_id(user_id)

            if event["type"] == "message" and event["message"]["type"] == "text":
                msg_id = event["message"]["id"]
                ts = event["timestamp"] / 1000
                dt = datetime.fromtimestamp(ts, tz=JST)
                sb_insert_message({
                    "message_id": msg_id,
                    "datetime": dt.strftime("%Y-%m-%d %H:%M"),
                    "text": event["message"]["text"],
                })
                reply_to_line(event.get("replyToken", ""), "memo ok")
    else:
        # 従来の/tmp保存
        messages = load_messages()
        existing_ids = {m.get("message_id") for m in messages if m.get("message_id")}
        new_count = 0

        for event in data.get("events", []):
            user_id = event.get("source", {}).get("userId", "")
            if user_id:
                save_user_id(user_id)

            if event["type"] == "message" and event["message"]["type"] == "text":
                msg_id = event["message"]["id"]
                if msg_id in existing_ids:
                    continue
                existing_ids.add(msg_id)

                ts = event["timestamp"] / 1000
                dt = datetime.fromtimestamp(ts, tz=JST)
                messages.append({
                    "message_id": msg_id,
                    "datetime": dt.strftime("%Y-%m-%d %H:%M"),
                    "text": event["message"]["text"],
                })
                new_count += 1
                reply_to_line(event.get("replyToken", ""), "memo ok")

        if new_count > 0:
            save_messages(messages)

    return jsonify({"status": "ok"})


@app.route("/messages", methods=["GET"])
def get_messages():
    if request.headers.get("X-API-Key") != API_KEY:
        abort(403)

    if use_supabase():
        return jsonify(sb_get_unsynced())
    else:
        return jsonify(load_messages())


@app.route("/messages/clear", methods=["POST"])
def clear_messages_endpoint():
    if request.headers.get("X-API-Key") != API_KEY:
        abort(403)

    if use_supabase():
        data = request.get_json(silent=True) or {}
        message_ids = data.get("message_ids", [])
        if message_ids:
            sb_mark_synced(message_ids)
        return jsonify({"status": "synced", "count": len(message_ids)})
    else:
        save_messages([])
        return jsonify({"status": "cleared"})


@app.route("/send", methods=["POST"])
def send_message():
    if request.headers.get("X-API-Key") != API_KEY:
        abort(403)
    data = request.get_json()
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "text is required"}), 400
    user_id = load_user_id()
    if not user_id:
        return jsonify({"error": "no user registered yet. Send a message first."}), 400
    ok = push_to_line(user_id, text)
    return jsonify({"status": "sent" if ok else "failed"})


@app.route("/health", methods=["GET"])
def health():
    mode = "supabase" if use_supabase() else "file"
    return jsonify({"status": "ok", "storage": mode})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
