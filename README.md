# Nihongo.AI — Daily Japanese Reading Bot 🌸

Welcome to **Nihongo.AI**! This is a complete, production-ready Telegram bot designed to help users practice Japanese reading comprehension every single day.

The bot automatically generates a short Japanese passage (JLPT N5–N4 level) with furigana, asks a multiple-choice question, and provides explanations in both Japanese and English. It also tracks user streaks, sends gentle reminders, and adapts to the user's reading level over time.

---

## 🌟 Features

- **Daily Quizzes**: A fresh Japanese reading passage delivered every day at 9:00 AM SGT.
- **AI-Powered**: Uses OpenAI to generate unique, safe, and engaging passages.
- **Furigana Support**: Automatically adds furigana to all kanji (e.g., 学校(がっこう)).
- **Smart Reminders**: Sends gentle nudges at 12 PM, 6 PM, and 9 PM SGT if the user hasn't answered.
- **Weekly Summaries**: Delivers a progress report every Friday with accuracy, streaks, and focus areas.
- **Difficulty Adaptation**: Automatically adjusts the difficulty (N5 vs N4) based on the user's recent accuracy.
- **Admin Controls**: Allows admins to regenerate and resend today's quiz if needed.

---

## 📂 Project Structure

Here is a quick overview of how the code is organized:

- `run.py` — The main script to start the bot.
- `nihongo_ai/bot.py` — Wires everything together and starts the Telegram bot.
- `nihongo_ai/config.py` — Loads environment variables and stores all settings.
- `nihongo_ai/database.py` — Handles saving and loading data using SQLite.
- `nihongo_ai/handlers.py` — Contains all the logic for commands (like `/start`) and button presses.
- `nihongo_ai/models.py` — Defines the data structures for Users, Quizzes, and Answers.
- `nihongo_ai/quiz_generator.py` — Connects to OpenAI to generate the daily Japanese passages.
- `nihongo_ai/scheduler.py` — Manages the timing for daily quizzes, reminders, and weekly summaries.

---

## 🚀 Step 1: Create Your Telegram Bot

Before running the code, you need to create a bot on Telegram and get a special "Token".

1. Open Telegram and search for **@BotFather** (the official bot creator).
2. Send the message `/newbot` to BotFather.
3. Choose a name for your bot (e.g., "Nihongo AI").
4. Choose a username for your bot. It must end in `bot` (e.g., `NihongoAIPracticeBot`).
5. BotFather will give you a **Token** that looks something like this: `1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ`.
6. **Copy this Token and keep it secret!** You will need it in the next steps.

---

## 💻 Step 2: How to Run Locally (For Testing)

If you want to test the bot on your own computer before putting it on the internet, follow these steps:

### Prerequisites
- You need **Python 3.9 or newer** installed on your computer.
- You need an **OpenAI API Key**. You can get one from the [OpenAI Platform](https://platform.openai.com/api-keys).

### Setup Instructions

1. **Open your terminal** (Command Prompt on Windows, Terminal on Mac/Linux).
2. **Navigate to the project folder**:
   ```bash
   cd path/to/nihongo-ai-bot
   ```
3. **Install the required packages**:
   ```bash
   pip install -r requirements.txt
   ```
4. **Set up your environment variables**:
   - Copy the `.env.example` file and rename it to `.env`.
   - Open the `.env` file in a text editor.
   - Paste your Telegram Bot Token next to `TELEGRAM_BOT_TOKEN=`.
   - Paste your OpenAI API Key next to `OPENAI_API_KEY=`.
   - (Optional) Add your Telegram Chat ID next to `ADMIN_CHAT_IDS=` if you want admin powers. You can find your Chat ID by messaging `@userinfobot` on Telegram.
5. **Start the bot**:
   ```bash
   python run.py
   ```
6. **Test it out!** Open Telegram, find your bot, and type `/start`.

---

## ☁️ Step 3: How to Deploy on Railway (24/7 Hosting)

To keep your bot running 24/7 even when your computer is off, you can host it on **Railway**. Railway is very beginner-friendly.

### 1. Upload your code to GitHub
1. Create a free account on [GitHub](https://github.com/).
2. Create a new repository (make it Private if you want).
3. Upload all the files from this folder to your new GitHub repository. **Do NOT upload the `.env` file or the `nihongo_ai.db` file.** (The `.gitignore` file is already set up to prevent this).

### 2. Connect to Railway
1. Go to [Railway.app](https://railway.app/) and sign in with your GitHub account.
2. Click the **"New Project"** button.
3. Select **"Deploy from GitHub repo"**.
4. Choose the repository you just created.
5. Railway will start building your project, but it will fail at first because it doesn't have your secret tokens yet. That's normal!

### 3. Set Environment Variables on Railway
1. Click on your newly created project in the Railway dashboard.
2. Go to the **"Variables"** tab.
3. Click **"New Variable"** and add the following:
   - **Name**: `TELEGRAM_BOT_TOKEN` | **Value**: *(Paste your Telegram token here)*
   - **Name**: `OPENAI_API_KEY` | **Value**: *(Paste your OpenAI key here)*
   - **Name**: `ADMIN_CHAT_IDS` | **Value**: *(Paste your Chat ID here)*
4. Once you add these variables, Railway will automatically restart your bot.

### 4. Add Persistent Storage (Important!)
By default, Railway deletes files every time it restarts. To save your users' progress and streaks, you need to add a persistent volume.

1. In your Railway project, go to the **"Settings"** tab.
2. Scroll down to the **"Volumes"** section.
3. Click **"Add Volume"**.
4. Set the Mount Path to `/app/data`.
5. Go back to the **"Variables"** tab and add one more variable:
   - **Name**: `DATA_DIR` | **Value**: `/app/data`
6. Railway will restart one last time. Your bot is now live and will remember user data forever!

---

## ⏰ How Scheduling Works

The bot uses a built-in scheduler to run tasks automatically based on **Singapore Time (SGT)**.

- **9:00 AM SGT**: The bot generates a brand new Japanese passage using OpenAI and sends it to all active users.
- **12:00 PM, 6:00 PM, 9:00 PM SGT**: The bot checks who hasn't answered today's quiz and sends them a gentle reminder message.
- **Friday 8:00 PM SGT**: The bot calculates everyone's accuracy for the week and sends a personalized weekly summary.

Because the bot runs continuously, it must be hosted on a platform like Railway to ensure these scheduled events happen on time.

---

## 🛠️ Troubleshooting Tips

- **The bot isn't responding to `/start`**: Make sure your `TELEGRAM_BOT_TOKEN` is correct and that the bot script is actually running (check the Railway logs).
- **The bot says "Sorry — I couldn't generate today's full passage"**: This means the OpenAI API took too long to respond. The bot automatically sends a backup mini-quiz and will try again in 10 minutes.
- **Users are losing their streaks when the bot restarts**: You forgot to set up the Persistent Volume on Railway. Follow Step 3.4 above to fix this.
- **How do I test the daily quiz without waiting until 9 AM?**: If you added your Chat ID to `ADMIN_CHAT_IDS`, you can type `/reset_today` in the bot. This will immediately generate and send a new quiz.

---

*Enjoy learning Japanese with Nihongo.AI! がんばって！* 🌸
