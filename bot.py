"""
Telegram bot এন্ট্রিপয়েন্ট।

কমান্ড:
    /edit <instructions>   - পুরো pipeline চালায়, শেষে push confirm করতে বলে
                              (config.yaml-এ require_push_confirmation: false করলে auto-push হবে)
    /history                - শেষ ১০টা রান দেখায়
    /undo                   - শেষ push হওয়া branch টা local+remote থেকে মুছে দেয়

ডিজাইন নোট:
    - একাধিক /edit একসাথে চলতে পারে (ThreadPoolExecutor) — বট ব্লক হয় না।
    - প্রতিটা রান আলাদা status মেসেজে লাইভ আপডেট হয়।
    - push হওয়ার আগে PipelineResult মেমরিতে (_pending_results) রাখা হয়,
      কনফার্ম বাটনে চাপ দিলে তখন আসল commit/push হয়।
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask

import telebot
from telebot import types

from config import CFG
import db
from pipeline import orchestrate_prepare, confirm_and_push, PipelineResult
from git_utils import discard_branch

# ==========================================
# Render & UptimeRobot HTTP Keep-Alive Server
# ==========================================
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive and running on Render!", 200

def run_web_server():
    # Render পরিবেশ থেকে PORT রিড করবে, না থাকলে ডিফল্ট 8080 ব্যবহার করবে
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    """ডিমন থ্রেডে ওয়েব সার্ভার চালু রাখবে যাতে বট ব্লক না হয়"""
    server_thread = threading.Thread(target=run_web_server)
    server_thread.daemon = True
    server_thread.start()

# ==========================================
# Telegram Bot & Thread Pools
# ==========================================
bot = telebot.TeleBot(CFG.telegram_token)
executor = ThreadPoolExecutor(max_workers=4)

# run_id -> {"result": PipelineResult, "prompt": str, "chat_id": int}
_pending_results = {}
_pending_lock = threading.Lock()

# একই সময়ে একই রিপোতে দুইটা git অপারেশন যেন না চলে (working copy শেয়ার্ড)
_repo_lock = threading.Lock()


def _is_allowed(user_id):
    return not CFG.allowed_user_ids or user_id in CFG.allowed_user_ids


def _run_pipeline_job(chat_id, user_id, prompt, status_msg_id):
    def status(text):
        try:
            bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=text[:4000])
        except Exception:
            pass

    run_id = db.create_run(chat_id, user_id, prompt)

    try:
        with _repo_lock:
            from git_utils import sync_default_branch
            status("🔄 git pull চলছে...")
            sync_default_branch()

            result = orchestrate_prepare(prompt, status)

        if not result.ok:
            db.update_run(run_id, status="failed", detail=result.reason)
            status(f"❌ থেমে গেছে।\nকারণ: {result.reason}")
            return

        db.set_run_files(run_id, list(result.final_codes.keys()))

        if not CFG.require_push_confirmation:
            with _repo_lock:
                branch = confirm_and_push(result, f"AI Edit: {prompt}")
            db.update_run(run_id, status="pushed", branch=branch, detail=result.approval_reason)
            status(
                f"✅ সম্পন্ন ও push হয়ে গেছে!\nBranch: {branch}\n"
                f"ফাইল: {', '.join(result.final_codes.keys())}\n"
                f"Approval: {result.approval_reason}"
            )
            return

        # confirmation দরকার — ফলাফল মেমরিতে রেখে বাটন দেখাই
        with _pending_lock:
            _pending_results[run_id] = {"result": result, "prompt": prompt, "chat_id": chat_id}

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("✅ Push করো", callback_data=f"push:{run_id}"),
            types.InlineKeyboardButton("❌ বাদ দাও", callback_data=f"discard:{run_id}"),
        )
        summary_lines = "\n".join(f"  • {s['file']} ({s['review']})" for s in result.summaries)
        bot.send_message(
            chat_id,
            f"🧾 পরিবর্তন প্রস্তুত — push করার আগে দেখে নিন:\n\n"
            f"Diagnosis: {result.diagnosis}\n\n"
            f"ফাইল:\n{summary_lines}\n\n"
            f"Approval agent-এর মন্তব্য: {result.approval_reason}",
            reply_markup=markup,
        )
        status("⏳ আপনার confirm-এর অপেক্ষায় (উপরের বাটনে চাপ দিন)।")

    except Exception:
        import traceback
        err = traceback.format_exc()
        db.update_run(run_id, status="error", detail=err[:2000])
        status(f"❌ Unexpected error:\n{err[:1500]}")
        try:
            with _repo_lock:
                from git_utils import sync_default_branch
                sync_default_branch()
        except Exception:
            pass


@bot.message_handler(commands=["edit"])
def handle_edit(message):
    if not _is_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ আপনি এই বট ব্যবহার করার অনুমতিপ্রাপ্ত না।")
        return

    prompt = message.text.partition(" ")[2].strip()
    if not prompt:
        bot.reply_to(message, "Usage: /edit <instructions>")
        return

    status_msg = bot.reply_to(message, "🔄 কিউতে যোগ হলো, শুরু হচ্ছে...")
    executor.submit(_run_pipeline_job, message.chat.id, message.from_user.id, prompt, status_msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("push:") or call.data.startswith("discard:"))
def handle_confirmation(call):
    action, run_id_str = call.data.split(":", 1)
    run_id = int(run_id_str)

    with _pending_lock:
        pending = _pending_results.pop(run_id, None)

    if not pending:
        bot.answer_callback_query(call.id, "এই রান আর পাওয়া যাচ্ছে না (মেয়াদ শেষ বা আগেই হ্যান্ডল হয়েছে)।")
        return

    if not _is_allowed(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ অনুমতি নেই।")
        return

    result: PipelineResult = pending["result"]
    prompt = pending["prompt"]

    if action == "discard":
        bot.answer_callback_query(call.id, "বাতিল করা হলো।")
        db.update_run(run_id, status="discarded")
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text="❌ বাতিল করা হয়েছে, কিছু push হয়নি।")
        return

    bot.answer_callback_query(call.id, "Push করা হচ্ছে...")
    try:
        with _repo_lock:
            branch = confirm_and_push(result, f"AI Edit: {prompt}")
        db.update_run(run_id, status="pushed", branch=branch, detail=result.approval_reason)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ Push হয়ে গেছে!\nBranch: {branch}\nফাইল: {', '.join(result.final_codes.keys())}",
        )
    except Exception as e:
        db.update_run(run_id, status="error", detail=str(e))
        bot.edit_message_text(
            chat_id=call.message.chat.id, message_id=call.message.message_id, text=f"❌ Push ব্যর্থ হয়েছে: {e}"
        )


@bot.message_handler(commands=["history"])
def handle_history(message):
    if not _is_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ অনুমতি নেই।")
        return
    runs = db.list_runs(message.chat.id, limit=10)
    if not runs:
        bot.reply_to(message, "কোনো রান পাওয়া যায়নি।")
        return
    lines = []
    for r in runs:
        lines.append(f"#{r['id']} [{r['status']}] {r['prompt'][:60]}  (branch: {r['branch'] or '-'})")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["undo"])
def handle_undo(message):
    if not _is_allowed(message.from_user.id):
        bot.reply_to(message, "⛔ অনুমতি নেই।")
        return
    last = db.get_last_pushed_run(message.chat.id)
    if not last or not last["branch"]:
        bot.reply_to(message, "Undo করার মতো কোনো push করা branch পাওয়া যায়নি।")
        return
    branch = last["branch"]
    try:
        with _repo_lock:
            from git_utils import undo_pushed_branch
            undo_pushed_branch(branch)
        db.update_run(last["id"], status="undone")
        bot.reply_to(message, f"↩️ Branch '{branch}' local ও remote দুই জায়গা থেকেই মুছে দেওয়া হয়েছে।")
    except Exception as e:
        bot.reply_to(message, f"❌ Undo ব্যর্থ হয়েছে: {e}")


@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    bot.reply_to(
        message,
        "🤖 Multi-Agent AI Code Editor\n\n"
        "/edit <instructions>  — pipeline চালায়\n"
        "/history               — শেষ রানগুলো দেখায়\n"
        "/undo                  — শেষ push করা branch বাতিল করে\n\n"
        "উদাহরণ:\n/edit navbar মোবাইলে ঠিকভাবে দেখাচ্ছে না, দেখো তো কি সমস্যা",
    )


if __name__ == "__main__":
    db.init_db()
    
    # Render-এর জন্য ব্যাকগ্রাউন্ডে Web Server চালু করা
    print("🌐 Web server চালু করা হচ্ছে (Port 8080)...")
    keep_alive()

    print("🤖 Bot চলছে...")
    bot.infinity_polling(skip_pending=True)

