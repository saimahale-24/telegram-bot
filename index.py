import os
import asyncio
import json
import google.generativeai as genai
from flask import Flask, request
from pymongo import MongoClient
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, 
    MessageHandler, CallbackQueryHandler, filters
)

# --- 1. CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
GEMINI_KEY = os.environ.get("GEMINI_KEY")

app = Flask(__name__)

# Database Setup
client = MongoClient(MONGO_URI)
db = client['agency_db']
users_col = db['users']
logs_col = db['logs']
tasks_col = db['tasks']

# AI Setup
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 2. BUTTONS MENU ---
def get_main_menu(role):
    if role == 'employee':
        keyboard = [
            [InlineKeyboardButton("📝 Log Work", callback_data='btn_log')],
            [InlineKeyboardButton("📋 My Tasks", callback_data='btn_my_tasks')],
            [InlineKeyboardButton("📅 My History", callback_data='btn_history')]
        ]
    elif role == 'owner':
        keyboard = [
            [InlineKeyboardButton("➕ Assign Task", callback_data='btn_assign')],
            [InlineKeyboardButton("📊 Team Progress", callback_data='btn_status')]
        ]
    else:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❓ Help", callback_data='btn_help')]])
    return InlineKeyboardMarkup(keyboard)

# --- 3. BOT COMMANDS ---

async def start(update: Update, context):
    user = update.effective_user
    user_data = users_col.find_one({"user_id": user.id})
    
    if not user_data:
        await update.message.reply_text(
            f"⛔ **Access Denied**\nYour ID is: `{user.id}`\nSend this to the Owner to get access.",
            parse_mode='Markdown'
        )
        return

    role = user_data.get('role')
    await update.message.reply_text(
        f"👋 Welcome {user_data['name']}!",
        reply_markup=get_main_menu(role)
    )

async def button_click(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    user_data = users_col.find_one({"user_id": user_id})

    if data == 'btn_log':
        await query.edit_message_text("✍️ Type your work update now:")
        context.user_data['expecting'] = 'log_entry'

    elif data == 'btn_my_tasks':
        tasks = list(tasks_col.find({"assigned_to_id": user_id, "status": "pending"}))
        if not tasks:
            await query.edit_message_text("✅ You have no pending tasks!", reply_markup=get_main_menu('employee'))
            return
        msg = "📋 **Your Pending Tasks:**\n\n"
        for t in tasks:
            msg += f"• {t['task_detail']} (By: {t['assigned_by']})\n"
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=get_main_menu('employee'))

    elif data == 'btn_assign':
        await query.edit_message_text("👉 Type the task naturally.\nExample: 'Assign logo design to Rahul'")
        context.user_data['expecting'] = 'assign_task'

    elif data == 'btn_status':
        await query.edit_message_text("⏳ AI is analyzing team logs...")
        await generate_status_report(update, context)

async def handle_text(update: Update, context):
    user_id = update.effective_user.id
    text = update.message.text
    state = context.user_data.get('expecting')
    user_data = users_col.find_one({"user_id": user_id})

    if not user_data: 
        return

    if state == 'log_entry':
        logs_col.insert_one({
            "user_id": user_id,
            "name": user_data['name'],
            "log": text,
            "date": datetime.now()
        })
        await update.message.reply_text("✅ Work Logged!", reply_markup=get_main_menu(user_data['role']))
        context.user_data['expecting'] = None

    elif state == 'assign_task':
        employees = list(users_col.find({"role": "employee"}))
        emp_names = [e['name'] for e in employees]
        
        prompt = f"""
        Extract the 'task' and the 'employee_name' from this text: "{text}"
        Available Employees: {', '.join(emp_names)}
        Return ONLY valid JSON like: {{"task": "...", "employee_name": "..."}}
        """
        response = model.generate_content(prompt)
        
        try:
            cleaned_json = response.text.replace('```json', '').replace('```', '').strip()
            data = json.loads(cleaned_json)
            target_emp_name = data.get('employee_name')
            task_detail = data.get('task')

            target_emp = users_col.find_one({"name": target_emp_name})
            if target_emp:
                tasks_col.insert_one({
                    "assigned_to_id": target_emp['user_id'],
                    "assigned_to_name": target_emp['name'],
                    "assigned_by": user_data['name'],
                    "task_detail": task_detail,
                    "status": "pending",
                    "created_at": datetime.now()
                })
                await update.message.reply_text(
                    f"✅ Task assigned to **{target_emp['name']}**:\n_{task_detail}_", 
                    parse_mode='Markdown',
                    reply_markup=get_main_menu('owner')
                )
            else:
                await update.message.reply_text(f"❌ Could not find employee named '{target_emp_name}'. Try again.")
        except Exception:
            await update.message.reply_text("❌ AI failed to understand. Please try again.")
        context.user_data['expecting'] = None

    else:
        await update.message.reply_text("Please use the buttons.", reply_markup=get_main_menu(user_data['role']))

async def generate_status_report(update, context):
    logs = list(logs_col.find().sort("date", -1).limit(15))
    if not logs:
        await update.effective_message.reply_text("No logs found.")
        return
    log_text = "\n".join([f"- {l['name']}: {l['log']}" for l in logs])
    prompt = f"Summarize these logs for the agency owner. Group by employee:\n{log_text}"
    response = model.generate_content(prompt)
    await update.effective_message.reply_text(response.text)

# --- 4. THE WEBHOOK (FIXED FOR VERCEL) ---
@app.route('/', methods=['POST'])
def webhook():
    if request.method == "POST":
        # Build the application fresh every time
        application = ApplicationBuilder().token(TOKEN).build()
        
        # Add Handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button_click))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        
        # Decode the update
        update = Update.de_json(request.get_json(force=True), application.bot)
        
        # ERROR FIX: Explicitly initialize the app before processing
        async def process():
            await application.initialize()
            await application.process_update(update)
            await application.shutdown()

        asyncio.run(process())
        
        return "OK"
    return "Bot is running"
