"""
LINE Webhook Server - 社長メモ用
LINEから送られたメッセージを受信・蓄積し、同期スクリプトで取得可能にする。
レポートをLINEに送信する機能も備える。
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

DATA_DIR = os.environ.get("DATA_DIR", "/tmp/line_inbox")
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.json")
USER_FILE = os.path.join(DATA_DIR, "user.json")

CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
API_KEY = os.environ.get("API_KEY", "")

JST = timezone(timedelta(hours=9))


def load_messages():
    if os.path.exists(MESSAGES_FILE):
        with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_messages(messages):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(MESSAGES_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def save_user_id(user_id):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USER_FILE, "w") as f:
        json.dump({"user_id": user_id}, f)


def load_user_id():
    if os.path.exists(USER_FILE):
        with open(USER_FILE, "r") as f:
            return json.load(f).get("user_id", "")
    return ""


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
    """プッシュメッセージでLINEに送信（ユーザーの操作なしで送れる）"""
    if not CHANNEL_ACCESS_TOKEN or not user_id:
        return False
    # LINEは1メッセージ5000文字制限。長い場合は分割
    chunks = []
    while text:
        chunks.append(text[:5000])
        text = text[5000:]
    messages = [{"type": "text", "text": c} for c in chunks[:5]]  # 最大5吹き出し
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


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        abort(403)

    data = json.loads(body)
    messages = load_messages()
    new_count = 0

    for event in data.get("events", []):
        # ユーザーIDを保存（Push送信用）
        user_id = event.get("source", {}).get("userId", "")
        if user_id:
            save_user_id(user_id)

        if event["type"] == "message" and event["message"]["type"] == "text":
            ts = event["timestamp"] / 1000
            dt = datetime.fromtimestamp(ts, tz=JST)
            messages.append(
                {
                    "datetime": dt.strftime("%Y-%m-%d %H:%M"),
                    "text": event["message"]["text"],
                }
            )
            new_count += 1
            reply_to_line(event.get("replyToken", ""), "memo ok")

    if new_count > 0:
        save_messages(messages)

    return jsonify({"status": "ok"})


@app.route("/messages", methods=["GET"])
def get_messages():
    if request.headers.get("X-API-Key") != API_KEY:
        abort(403)
    return jsonify(load_messages())


@app.route("/messages/clear", methods=["POST"])
def clear_messages():
    if request.headers.get("X-API-Key") != API_KEY:
        abort(403)
    save_messages([])
    return jsonify({"status": "cleared"})


@app.route("/send", methods=["POST"])
def send_message():
    """レポートなどをLINEに送信するエンドポイント"""
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
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
