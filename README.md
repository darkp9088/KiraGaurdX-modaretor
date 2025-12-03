<!-- Banner -->
<p align="center">
  <img src="https://github.com/darkp9088/KiraGaurdX-modaretor/blob/main/modaretor.png" width="750" alt="Moderator Bot Banner">
</p>

# 🛡️ Moderator Bot  
A powerful, production-ready Telegram group moderation bot with **anti-spam**, **auto-warnings**, **welcome system**, and **full admin tools**.

---

## 📌 Features

### 🧹 Automatic Moderation
- Deletes spam, links, promotions  
- Profanity filter  
- Message frequency spam detector  
- Auto warn → mute → ban system  

### 👮 Admin Tools
- /ban, /unban, /unbanall  
- /mute, /unmute, /tmute  
- /warn, /warnings (auto ban at 3 warns)  
- /info for detailed user info  

### 👋 Welcome & Goodbye System
- Custom welcome message  
- Photo/video support  
- Goodbye message  
- Option to toggle on/off  

### 🎛 Interactive Menu
- Toggle anti-link & anti-spam  
- Set welcome message  
- View ban logs  
- Group settings  
- Help menu  

### 💾 Database (SQLite)
- Stores warns  
- Stores bans  
- Stores mute time  
- Chat settings  
- Welcome settings  
- Sticker/keyword filters  

### 🔁 Background Tasks
- Auto unmute expired users  
- Clean logs periodically  

---

## 🧰 Tech Stack
- Python  
- python-telegram-bot  
- SQLite  
- aiohttp  
- aiosqlite  
- Deep Translator  

---
⚠️ If You Face Any Problem

If you find any issue, bug, or error while using this Moderator Bot, feel free to:

Open an Issue in the repository

Report a bug

Suggest improvements

Request new features

## 📦 Installation

```bash
git clone https://github.com/darkp9088/Advance-modaretor
cd moderator-bot
pip install -r requirements.txt

python3 -m venv env
source env/bin/activate


Run:

python main.py

