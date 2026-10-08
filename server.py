"""Local React demo server. JSON-lines carry real agent events to the browser."""
import json
import os
import secrets
import sqlite3
from pathlib import Path
from queue import Queue
from threading import Lock, Thread

from flask import Flask, Response, jsonify, request, session, send_from_directory
from openai import OpenAI

from agent import chat
from database import initialize
from memory import Memory
from orders import submit_order

FRONTEND = Path(__file__).parent / "frontend" / "dist"
app = Flask(__name__, static_folder=str(FRONTEND / "assets"), static_url_path="/assets")
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=16_384, SESSION_COOKIE_SAMESITE="Strict",
                  SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")))
conversations = {}  # Local demo only: memory is cleared when the server restarts.

if os.getenv("VERCEL"):
    initialize()


@app.get("/api/health")
def health_route():
    return jsonify(ok=True, storage="temporary" if os.getenv("VERCEL") else "local")


def conversation():
    if "chat_id" not in session:
        session["chat_id"] = secrets.token_hex(16)
    return conversations.setdefault(session["chat_id"], {"memory": Memory(), "lock": Lock()})


@app.before_request
def same_origin():
    if request.method == "POST" and request.headers.get("Origin") not in (
            None, request.host_url.rstrip("/")):
        return jsonify(error="Cross-origin requests are not allowed."), 403


@app.get("/")
def index():
    conversation()  # Set the session cookie before the first streamed response.
    # Vercel fixes file mtimes; same-size HTML can reuse an ETag across builds.
    response = send_from_directory(FRONTEND, "index.html", conditional=False, etag=False)
    response.headers["Cache-Control"] = "no-store"
    response.headers.pop("Last-Modified", None)
    return response


@app.post("/api/chat")
def chat_route():
    data = request.get_json()
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return jsonify(error="Enter a message of 1–4,000 characters."), 400
    if not os.getenv("OPENAI_API_KEY") or not os.getenv("OPENAI_MODEL"):
        return jsonify(error="API settings missing. Check .env and restart."), 503
    state = conversation()
    if not state["lock"].acquire(blocking=False):
        return jsonify(error="Another request is running. Please wait."), 409
    events = Queue()

    def emit(kind, **payload):
        events.put({"type": kind, **payload})

    def run():
        try:
            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"],
                            base_url=os.getenv("OPENAI_BASE_URL") or None,
                            timeout=30, max_retries=1)
            reply = chat(client, os.environ["OPENAI_MODEL"], state["memory"], text.strip(),
                         on_text=lambda value: emit("text", text=value),
                         on_status=lambda value: emit("status", text=value))
            emit("done", reply=reply, draft=state["memory"].pending_order)
        except Exception:
            # Do not leak provider details or leave a draft after an unexpected failure.
            app.logger.exception("Chat request failed")
            state["memory"] = Memory()
            emit("error", text="Request failed. Conversation reset; please try again.")
        finally:
            state["lock"].release()
            events.put(None)

    Thread(target=run, daemon=True).start()

    def stream():
        while (event := events.get()) is not None:
            yield json.dumps(event) + "\n"

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/order")
def order_route():
    data = request.get_json()
    if not isinstance(data, dict) or data.get("action") not in ("confirm", "cancel"):
        return jsonify(error="Choose confirm or cancel."), 400
    state = conversation()
    with state["lock"]:
        memory = state["memory"]
        draft = memory.pending_order
        if not draft or data.get("order_id") != draft["order_id"]:
            return jsonify(error="Draft no longer current. Prepare a new order.", draft=None), 409
        if data["action"] == "cancel":
            reply = "Order draft cancelled. Nothing was submitted."
            confirmation = None
        else:
            try:
                result = submit_order(draft)  # Always use server-side draft, never client prices.
            except (OSError, sqlite3.Error):
                return jsonify(error="Export failed. Retry with the same order ID.", draft=draft), 503
            reply = result.get("error") or f"Order {result['order_id']} exported locally. No payment taken."
            confirmation = None if result.get("error") else {**draft, "order_id": result["order_id"]}
        memory.pending_order = None
        memory.start_turn(data["action"] + " order")
        memory.add_reply(reply)
        return jsonify(reply=reply, draft=None, confirmation=confirmation)


@app.post("/api/reset")
def reset_route():
    state = conversation()
    with state["lock"]:
        state["memory"] = Memory()
    return jsonify(ok=True)


if __name__ == "__main__":
    initialize()
    app.run(host="127.0.0.1", port=8000, threaded=True, debug=False)
