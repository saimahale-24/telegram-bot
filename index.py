import os
import asyncio
import json
import logging
from flask import Flask, request
from pymongo import MongoClient
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, 
    MessageHandler, CallbackQueryHandler, filters
)
from openai import OpenAI  # <--- NEW LIBRARY

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY") # <--- NEW KEY NAME

# Setup Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.ERROR)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- DATABASE CONNECTION ---
try:
    client = MongoClient(MONGO_URI)
    db = client['agency_db']
    users_col = db['users']
    logs_col = db['logs']
    tasks_col = db['tasks']
except Exception as e:
    logger.error(f"DATABASE CONNECT ERROR: {e}")

# --- AI SETUP (OPENROUTER) ---
# We use the OpenAI client but point it to OpenRouter's URL
ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

# HELPER: Call AI
def ask_ai(prompt):
    try:
        completion = ai_client.chat.completions.create(
            # YOU CAN CHANGE THIS MODEL NAME TO ANYTHING (e.g., "anthropic/claude-3-haiku")
            model="nvidia/nemotron-3-nano-30b-a3b:free", 
            messages=[
                {"role": "system", "content": "You are a helpful agency assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"AI ERROR: {e}")
        return "⚠️ AI Error. Please try again."

# --- HELPER FUNCTIONS ---
def get_main_menu(role):
    if role == 'employee':
        keyboard = [
            [InlineKeyboardButton("📝 Log Work", callback_data='btn_log')],
            [InlineKeyboardButton("📋 My Tasks", callback_data='btn_my_tasks')],
            [InlineKeyboardButton("📅 My History", callback_data='btn_history')]
        ]
    elif role == 'owner' or role == 'admin':
        keyboard = [
            [InlineKeyboardButton("➕ Assign Task", callback_data='btn_assign')],
            [InlineKeyboardButton("📊 Team Progress", callback_data='btn_status')]
        ]
    else:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data='btn_help')]])
    return InlineKeyboardMarkup(keyboard)

# --- BOT COMMANDS ---

async def start(update: Update, context):
    try:
        user = update.effective_user
        user_data = users_col.find_one({"user_id": user.id})
        
        if not user_data:
            await update.message.reply_text(f"⛔ **Access Denied**\nID: `{user.id}`", parse_mode='Markdown')
            return

        users_col.update_one({"user_id": user.id}, {"$set": {"state": None}})
        role = user_data.get('role')
        await update.message.reply_text(f"👋 Welcome {user_data['name']}!", reply_markup=get_main_menu(role))
    
    except Exception as e:
        await update.message.reply_text(f"🔥 **Start Error:**\n{str(e)}")

async def button_click(update: Update, context):
    try:
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = query.from_user.id
        
        if data == 'btn_log':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": "log_entry"}})
            await query.edit_message_text("Type your work update now:")

        elif data == 'btn_assign':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": "assign_task"}})
            await query.edit_message_text("Type task (e.g. 'Assign logo to Rahul')")

        elif data == 'btn_my_tasks':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
            tasks = list(tasks_col.find({"assigned_to_id": user_id, "status": "pending"}))
            if not tasks:
                await query.edit_message_text("✅ No pending tasks!", reply_markup=get_main_menu('employee'))
                return
            msg = "📋 **Your Pending Tasks:**\n\n" + "\n".join([f"• {t['task_detail']}" for t in tasks])
            await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=get_main_menu('employee'))

        elif data == 'btn_status':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
            await query.edit_message_text("⏳ AI is reading logs...")
            await generate_status_report(update, context)
            
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔥 Button Error: {e}")

async def handle_text(update: Update, context):
    try:
        user_id = update.effective_user.id
        text = update.message.text
        user_data = users_col.find_one({"user_id": user_id})
        if not user_data: return
        state = user_data.get('state')

        if state == 'log_entry':
            logs_col.insert_one({
                "user_id": user_id, "name": user_data['name'], "log": text, "date": datetime.now()
            })
            users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
            await update.message.reply_text("✅ Logged!", reply_markup=get_main_menu(user_data['role']))

        elif state == 'assign_task':
            employees = list(users_col.find({"role": "employee"}))
            emp_names = [e['name'] for e in employees]
            
            # --- OPENROUTER CALL ---
            prompt = f"Extract 'task' and 'employee_name' from: '{text}'. Employees: {emp_names}. Return JSON."
            response_text = ask_ai(prompt)
            
            # Clean JSON
            cleaned = response_text.replace('```json', '').replace('```', '').strip()
            data = json.loads(cleaned)
            
            target = users_col.find_one({"name": data.get('employee_name')})
            if target:
                tasks_col.insert_one({
                    "assigned_to_id": target['user_id'], "assigned_to_name": target['name'],
                    "assigned_by": user_data['name'], "task_detail": data.get('task'),
                    "status": "pending", "created_at": datetime.now()
                })
                users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
                await update.message.reply_text(f"✅ Assigned to {target['name']}", reply_markup=get_main_menu('owner'))
            else:
                await update.message.reply_text("❌ Employee not found.", reply_markup=get_main_menu('owner'))
        else:
            await update.message.reply_text("Please use buttons.", reply_markup=get_main_menu(user_data['role']))

    except Exception as e:
        await update.message.reply_text(f"🔥 Text Error: {e}")

async def generate_status_report(update, context):
    try:
        logs = list(logs_col.find().sort("date", -1).limit(15))
        role = 'owner' # Default
        
        if not logs:
            await update.effective_message.reply_text("📭 No logs.", reply_markup=get_main_menu(role))
            return

        log_text = "\n".join([f"- {l['name']}: {l['log']}" for l in logs])
        
        # --- OPENROUTER CALL ---
        response_text = ask_ai(f"Summarize logs:\n{log_text}")
        
        await update.effective_message.reply_text(response_text, reply_markup=get_main_menu(role))
    except Exception as e:
         await update.effective_message.reply_text(f"🔥 AI Error: {e}")

# --- WEBHOOK ---
@app.route('/', methods=['POST'])
def webhook():
    if request.method == "POST":
        application = ApplicationBuilder().token(TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button_click))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        
        update = Update.de_json(request.get_json(force=True), application.bot)
        
        async def process():
            await application.initialize()
            await application.process_update(update)
            await application.shutdown()

        try:
            asyncio.run(process())
        except Exception as e:
            logger.error(f"WEBHOOK CRASH: {e}")
            return "Error", 500
        
        return "OK"
    return "Bot is running"

