import os
import asyncio
import json
import logging
from flask import Flask, request
from pymongo import MongoClient
from datetime import datetime
from bson.objectid import ObjectId
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, 
    MessageHandler, CallbackQueryHandler, filters
)
from openai import OpenAI

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY")

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
    agencies_col = db['agencies'] # <--- NEW COLLECTION
except Exception as e:
    logger.error(f"DATABASE CONNECT ERROR: {e}")

# --- AI SETUP ---
ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

def ask_ai(prompt):
    try:
        completion = ai_client.chat.completions.create(
            model="google/gemini-pro-1.5", 
            messages=[
                {"role": "system", "content": "You are a helpful agency assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"AI ERROR: {e}")
        return "⚠️ AI Error. Please try again."

# --- MENUS ---
def get_main_menu(role):
    if role == 'employee':
        keyboard = [
            [InlineKeyboardButton("📝 Log Work", callback_data='btn_log')],
            [InlineKeyboardButton("📋 My Tasks", callback_data='btn_my_tasks')], # This will now show "Mark Done" buttons
            [InlineKeyboardButton("📅 My History", callback_data='btn_history')]
        ]
    elif role == 'owner' or role == 'admin':
        keyboard = [
            [InlineKeyboardButton("➕ Assign Task", callback_data='btn_assign')],
            [InlineKeyboardButton("📊 Team Progress", callback_data='btn_status')]
        ]
    else:
        return None
    return InlineKeyboardMarkup(keyboard)

# --- BOT LOGIC ---

async def start(update: Update, context):
    try:
        user = update.effective_user
        user_data = users_col.find_one({"user_id": user.id})
        
        # SCENARIO 1: NEW USER (Stranger)
        if not user_data:
            await update.message.reply_text(
                f"👋 **Welcome, {user.first_name}!**\n\n"
                "You are not part of any agency yet.\n"
                "👉 **Please reply with your Agency Code** to join.\n"
                "(Example: `AGENCY-001`)",
                parse_mode='Markdown'
            )
            return

        # SCENARIO 2: REGISTERED USER
        users_col.update_one({"user_id": user.id}, {"$set": {"state": None}})
        role = user_data.get('role')
        agency_id = user_data.get('agency_id')
        
        await update.message.reply_text(
            f"🏢 **{agency_id} Dashboard**\nWelcome back, {user_data['name']}!", 
            reply_markup=get_main_menu(role)
        )
    
    except Exception as e:
        await update.message.reply_text(f"🔥 Error: {str(e)}")

async def handle_text(update: Update, context):
    try:
        user_id = update.effective_user.id
        text = update.message.text.strip()
        user_data = users_col.find_one({"user_id": user_id})

        # --- CASE A: LOGIN (Stranger sending a Code) ---
        if not user_data:
            # Check if this text is a valid Agency Code
            agency = agencies_col.find_one({"code": text})
            
            if agency:
                # Decide Role: If they are the FIRST user, make them Owner. Else Employee.
                existing_users = users_col.count_documents({"agency_id": text})
                new_role = "owner" if existing_users == 0 else "employee"
                
                users_col.insert_one({
                    "user_id": user_id,
                    "name": update.effective_user.first_name,
                    "agency_id": text, # <--- CRITICAL TAG
                    "role": new_role,
                    "state": None
                })
                await update.message.reply_text(
                    f"✅ **Success!** Joined **{agency['name']}** as {new_role.upper()}.", 
                    reply_markup=get_main_menu(new_role),
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Invalid Agency Code. Please try again.")
            return

        # --- CASE B: LOGGED IN USER ACTIONS ---
        state = user_data.get('state')
        agency_id = user_data.get('agency_id') # <--- GET AGENCY TAG

        if state == 'log_entry':
            logs_col.insert_one({
                "user_id": user_id, 
                "name": user_data['name'], 
                "log": text, 
                "agency_id": agency_id, # <--- SAVE TAG
                "date": datetime.now()
            })
            users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
            await update.message.reply_text("✅ Logged!", reply_markup=get_main_menu(user_data['role']))

        elif state == 'assign_task':
            # Only find employees IN THIS AGENCY
            employees = list(users_col.find({"role": "employee", "agency_id": agency_id}))
            emp_names = [e['name'] for e in employees]
            
            prompt = f"Extract 'task' and 'employee_name' from: '{text}'. Employees: {emp_names}. Return JSON."
            response_text = ask_ai(prompt)
            cleaned = response_text.replace('```json', '').replace('```', '').strip()
            data = json.loads(cleaned)
            
            # Find target employee in THIS AGENCY
            target = users_col.find_one({"name": data.get('employee_name'), "agency_id": agency_id})
            
            if target:
                tasks_col.insert_one({
                    "assigned_to_id": target['user_id'], 
                    "assigned_to_name": target['name'],
                    "assigned_by": user_data['name'], 
                    "task_detail": data.get('task'),
                    "agency_id": agency_id, # <--- SAVE TAG
                    "status": "pending", 
                    "created_at": datetime.now()
                })
                users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
                await update.message.reply_text(f"✅ Assigned to {target['name']}", reply_markup=get_main_menu('owner'))
            else:
                await update.message.reply_text("❌ Employee not found in this agency.", reply_markup=get_main_menu('owner'))
        
        else:
            await update.message.reply_text("Please use buttons.", reply_markup=get_main_menu(user_data['role']))

    except Exception as e:
        await update.message.reply_text(f"🔥 Error: {e}")

async def button_click(update: Update, context):
    try:
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = query.from_user.id
        user_data = users_col.find_one({"user_id": user_id})
        agency_id = user_data.get('agency_id')

        # --- MARK DONE LOGIC ---
        if data.startswith('done_'):
            task_oid = data.split('_')[1] # Extract the Task ID from button
            # Update DB
            tasks_col.update_one({"_id": ObjectId(task_oid)}, {"$set": {"status": "completed"}})
            await query.edit_message_text("✅ **Task Marked as Done!**", parse_mode='Markdown')
            # Show menu again
            await context.bot.send_message(chat_id=user_id, text="What's next?", reply_markup=get_main_menu(user_data['role']))
            return

        # --- STANDARD BUTTONS ---
        if data == 'btn_log':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": "log_entry"}})
            await query.edit_message_text("✍️ Type your work update now:")

        elif data == 'btn_assign':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": "assign_task"}})
            await query.edit_message_text("👉 Type task (e.g. 'Assign logo to Rahul')")

        elif data == 'btn_my_tasks':
            # Find tasks for THIS USER in THIS AGENCY
            tasks = list(tasks_col.find({
                "assigned_to_id": user_id, 
                "agency_id": agency_id, 
                "status": "pending"
            }))
            
            if not tasks:
                await query.edit_message_text("✅ No pending tasks!", reply_markup=get_main_menu('employee'))
                return
            
            # Create a message with "Mark Done" buttons for EACH task
            await query.edit_message_text("📋 **Your Pending Tasks:**\n(Click to complete)")
            
            for t in tasks:
                # Unique button for each task
                keyboard = [[InlineKeyboardButton(f"✅ Done: {t['task_detail']}", callback_data=f"done_{t['_id']}")]]
                await context.bot.send_message(
                    chat_id=user_id, 
                    text=f"• {t['task_detail']} (By: {t['assigned_by']})", 
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        elif data == 'btn_status':
            users_col.update_one({"user_id": user_id}, {"$set": {"state": None}})
            await query.edit_message_text("⏳ AI is analyzing logs...")
            
            # Fetch logs for THIS AGENCY only
            logs = list(logs_col.find({"agency_id": agency_id}).sort("date", -1).limit(15))
            if not logs:
                await query.message.reply_text("📭 No logs found.", reply_markup=get_main_menu('owner'))
                return
            
            log_text = "\n".join([f"- {l['name']}: {l['log']}" for l in logs])
            response_text = ask_ai(f"Summarize logs:\n{log_text}")
            await query.message.reply_text(response_text, reply_markup=get_main_menu('owner'))
            
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🔥 Error: {e}")

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
