# -*- coding: utf-8 -*-
import logging, os, re, sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Optional, List, Tuple
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ==== НАСТРОЙКИ ====
BOT_TOKEN = "<<<СЮДА ТОКЕН>>>"
ADMIN_ID = 963586834
TZ = ZoneInfo("Europe/Kaliningrad")
DB = "assistant.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("assistant")

# ==== HEALTH ====
class Health(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

def start_health():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Health)
    Thread(target=srv.serve_forever, daemon=True).start()

# ==== БД ====
def db(): return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(chat_id INTEGER PRIMARY KEY, is_auth INTEGER NOT NULL DEFAULT 0, key_used TEXT);
        CREATE TABLE IF NOT EXISTS access_keys(key TEXT PRIMARY KEY, used_by_chat_id INTEGER);
        CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('once','daily','monthly')),
            run_at_utc TEXT, hour INTEGER, minute INTEGER, day_of_month INTEGER
        );
        """)
        have = {r[0] for r in c.execute("SELECT key FROM access_keys")}
        to_add = [(f"VIP{i:03d}",) for i in range(1,101) if f"VIP{i:03d}" not in have]
        if to_add: c.executemany("INSERT INTO access_keys(key) VALUES(?)", to_add)
        c.commit()

def is_auth(chat_id:int)->bool:
    with db() as c:
        r=c.execute("SELECT is_auth FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        return bool(r and r[0])

def use_key(chat_id:int, text:str)->bool:
    key=re.sub(r"\s+","",text).upper()
    if not re.fullmatch(r"VIP\d{3}", key): return False
    with db() as c:
        r=c.execute("SELECT key,used_by_chat_id FROM access_keys WHERE key=?", (key,)).fetchone()
        if not r or (r[1] and r[1]!=chat_id): return False
        c.execute("INSERT INTO users(chat_id,is_auth,key_used) VALUES(?,?,?) "
                  "ON CONFLICT(chat_id) DO UPDATE SET is_auth=excluded.is_auth, key_used=excluded.key_used",
                  (chat_id,1,key))
        c.execute("UPDATE access_keys SET used_by_chat_id=? WHERE key=?", (chat_id,key))
        c.commit()
        return True

def keys_left()->int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM access_keys WHERE used_by_chat_id IS NULL").fetchone()[0]

@dataclass
class Task:
    id:int; chat_id:int; title:str; type:str
    run_at_utc:Optional[datetime]; hour:Optional[int]; minute:Optional[int]; day_of_month:Optional[int]

def row2task(r:Tuple)->Task:
    return Task(r[0],r[1],r[2],r[3], datetime.fromisoformat(r[4]) if r[4] else None, r[5],r[6],r[7])

def add_task(chat_id:int,title:str,typ:str,run_at_utc:Optional[datetime],h:Optional[int],m:Optional[int],d:Optional[int])->int:
    with db() as c:
        cur=c.execute("INSERT INTO tasks(chat_id,title,type,run_at_utc,hour,minute,day_of_month) VALUES(?,?,?,?,?,?,?)",
                      (chat_id,title,typ, run_at_utc.isoformat() if run_at_utc else None, h,m,d))
        c.commit(); return cur.lastrowid

def get_task(tid:int)->Optional[Task]:
    with db() as c:
        r=c.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE id=?", (tid,)).fetchone()
        return row2task(r) if r else None

def list_tasks(chat_id:int)->List[Task]:
    with db() as c:
        rows=c.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks WHERE chat_id=?", (chat_id,)).fetchall()
        return [row2task(r) for r in rows]

def delete_task(tid:int):
    with db() as c: c.execute("DELETE FROM tasks WHERE id=?", (tid,)); c.commit()

# ==== ПАРСИНГ ====
MONTHS={"января":1,"февраля":2,"марта":3,"апреля":4,"мая":5,"июня":6,
        "июля":7,"августа":8,"сентября":9,"октября":10,"ноября":11,"декабря":12}

REL=re.compile(r"^\s*через\s+(\d+)\s*(сек(?:унд(?:у|ы)?)?|с|мин(?:ут(?:у|ы)?)?|м|час(?:а|ов)?|ч)\s+(.+)$", re.I)
TOD=re.compile(r"^\s*сегодня\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
TOM=re.compile(r"^\s*завтра\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DAY=re.compile(r"^\s*каждый\s*день\s*в\s*(\d{1,2})[.:](\d{2})\s+(.+)$", re.I)
DNUM=re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)
DTXT=re.compile(r"^\s*(\d{1,2})\s+([а-яА-Я]+)(?:\s+(\d{4}))?(?:\s*в\s*(\d{1,2})[.:](\d{2}))?\s+(.+)$", re.I)

@dataclass
class Parsed:
    typ:str; title:str; run_utc:Optional[datetime]; h:Optional[int]; m:Optional[int]; d:Optional[int]

def parse(text:str, now:datetime)->Optional[Parsed]:
    text=text.strip()
    m=REL.match(text)
    if m:
        n=int(m.group(1)); u=m.group(2).lower(); title=m.group(3).strip()
        if u.startswith("сек") or u=="с": delta=timedelta(seconds=n)
        elif u.startswith("мин") or u=="м": delta=timedelta(minutes=n)
        else: delta=timedelta(hours=n)
        run=(now+delta).astimezone(timezone.utc)
        return Parsed("once", title, run, None,None,None)
    m=TOD.match(text)
    if m:
        h,mi=int(m.group(1)),int(m.group(2)); title=m.group(3).strip()
        run=now.replace(hour=h,minute=mi,second=0,microsecond=0)
        if run<=now: run+=timedelta(days=1)
        return Parsed("once", title, run.astimezone(timezone.utc), None,None,None)
    m=TOM.match(text)
    if m:
        h,mi=int(m.group(1)),int(m.group(2)); title=m.group(3).strip()
        run=(now+timedelta(days=1)).replace(hour=h,minute=mi,second=0,microsecond=0)
        return Parsed("once", title, run.astimezone(timezone.utc), None,None,None)
    m=DAY.match(text)
    if m:
        h,mi=int(m.group(1)),int(m.group(2)); title=m.group(3).strip()
        return Parsed("daily", title, None, h,mi,None)
    m=DNUM.match(text)
    if m:
        d,mo=int(m.group(1)),int(m.group(2)); y=int(m.group(3) or now.year)
        h=int(m.group(4) or 10); mi=int(m.group(5) or 0); title=m.group(6).strip()
        run=datetime(y,mo,d,h,mi,tzinfo=TZ)
        if run<=now and not m.group(3): run=datetime(y+1,mo,d,h,mi,tzinfo=TZ)
        return Parsed("once", title, run.astimezone(timezone.utc), None,None,None)
    m=DTXT.match(text)
    if m:
        d=int(m.group(1)); mon=m.group(2).lower()
        if mon not in MONTHS: return None
        y=int(m.group(3) or now.year); h=int(m.group(4) or 10); mi=int(m.group(5) or 0)
        title=m.group(6).strip(); mo=MONTHS[mon]
        run=datetime(y,mo,d,h,mi,tzinfo=TZ)
        if run<=now and not m.group(3): run=datetime(y+1,mo,d,h,mi,tzinfo=TZ)
        return Parsed("once", title, run.astimezone(timezone.utc), None,None,None)
    return None

# ==== PLAN ====
def fmt(dt_utc:datetime)->str: return dt_utc.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

async def job_once(ctx: ContextTypes.DEFAULT_TYPE):
    t=get_task(ctx.job.data["id"])
    if t: await ctx.bot.send_message(t.chat_id, f"🔔 Напоминание: {t.title}")

async def schedule(app:Application, t:Task):
    jq=app.job_queue
    for j in jq.get_jobs_by_name(f"task_{t.id}"): j.schedule_removal()
    if t.type=="once" and t.run_at_utc and t.run_at_utc>datetime.now(timezone.utc):
        jq.run_once(job_once, when=t.run_at_utc, name=f"task_{t.id}", data={"id":t.id})
    elif t.type=="daily":
        jq.run_daily(job_once, time=dtime(hour=t.hour,minute=t.minute,tzinfo=TZ), name=f"task_{t.id}", data={"id":t.id})
    elif t.type=="monthly":
        async def monthly(ctx: ContextTypes.DEFAULT_TYPE):
            tt=get_task(ctx.job.data["id"])
            if tt and datetime.now(TZ).day==tt.day_of_month:
                await ctx.bot.send_message(tt.chat_id, f"🔔 Напоминание: {tt.title}")
        jq.run_daily(monthly, time=dtime(hour=t.hour,minute=t.minute,tzinfo=TZ), name=f"task_{t.id}", data={"id":t.id})

async def reschedule_all(app:Application):
    with db() as c:
        rows=c.execute("SELECT id,chat_id,title,type,run_at_utc,hour,minute,day_of_month FROM tasks").fetchall()
    for r in rows: await schedule(app, row2task(r))

# ==== КОМАНДЫ ====
LAST={}

async def start_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    msg=(
      "👋 Привет, я твой личный ассистент. Помогу оптимизировать рутину.\n\n"
      "Этот бот приватный. Введите ключ в формате ABC123 (например, VIP003).\n\n"
      "Примеры:\n"
      "• через 2 минуты поесть / через 30 секунд позвонить\n"
      "• сегодня в 18:30 попить воды\n"
      "• завтра в 09:00 сходить в зал\n"
      "• каждый день в 07:45 чистить зубы\n"
      "• 30 августа в 10:00 оплатить кредит\n\n"
      "❗ Напоминание «за N минут»: просто поставь время на N минут раньше."
    )
    await u.message.reply_text(msg)

async def affairs_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    chat=u.effective_chat.id
    if not (is_auth(chat) or u.effective_user.id==ADMIN_ID):
        await u.message.reply_text("Бот приватный. Введи ключ (пример: VIP003)."); return
    tasks=list_tasks(chat)
    if not tasks: await u.message.reply_text("У тебя пока нет дел."); return
    now=datetime.now(TZ)
    def next_run(t:Task):
        if t.type=="once" and t.run_at_utc: return t.run_at_utc.astimezone(TZ)
        if t.type=="daily":
            cand=now.replace(hour=t.hour,minute=t.minute,second=0,microsecond=0)
            if cand<=now: cand+=timedelta(days=1); return cand
        y,m=now.year,now.month
        for _ in range(24):
            try:
                cand=datetime(y,m,t.day_of_month,t.hour,t.minute,tzinfo=TZ)
                if cand>now: return cand
                m=1 if m==12 else m+1;  y+=1 if m==1 else 0
            except ValueError:
                m=1 if m==12 else m+1;  y+=1 if m==1 else 0
        return now+timedelta(days=30)
    tasks=sorted(tasks, key=next_run)[:20]
    LAST[chat]=[t.id for t in tasks]
    lines=[]
    for i,t in enumerate(tasks,1):
        if t.type=="once": w=fmt(t.run_at_utc)
        elif t.type=="daily": w=f"каждый день в {t.hour:02d}:{t.minute:02d}"
        else: w=f"каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d}"
        lines.append(f"{i}. {t.title} — {w}")
    await u.message.reply_text("Твои дела:\n"+"\n".join(lines))

async def affairs_delete_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    chat=u.effective_chat.id
    if not (is_auth(chat) or u.effective_user.id==ADMIN_ID):
        await u.message.reply_text("Бот приватный. Введи ключ."); return
    if not c.args or not c.args[0].isdigit():
        await u.message.reply_text("Использование: /affairs_delete <номер>"); return
    idx=int(c.args[0]); ids=LAST.get(chat)
    if not ids or idx<1 or idx>len(ids):
        await u.message.reply_text("Сначала покажи список /affairs и проверь номер."); return
    tid=ids[idx-1]; t=get_task(tid)
    if t: delete_task(t.id); await u.message.reply_text(f"🗑 Удалено: «{t.title}»")
    else: await u.message.reply_text("Это дело уже удалено.")

async def keys_left_cmd(u:Update, c:ContextTypes.DEFAULT_TYPE):
    if u.effective_user.id!=ADMIN_ID: return
    await u.message.reply_text(f"Свободных ключей: {keys_left()} из 100.")

# ==== ТЕКСТ ====
async def handle_text(u:Update, c:ContextTypes.DEFAULT_TYPE):
    chat=u.effective_chat.id; text=(u.message.text or "").strip()

    if not is_auth(chat) and u.effective_user.id!=ADMIN_ID:
        if use_key(chat, text):
            await u.message.reply_text("✅ Доступ подтверждён! Используй /affairs и добавляй дела.")
        else:
            await u.message.reply_text("❌ Неверный ключ. Пример: VIP003.")
        return

    m=re.fullmatch(r"(?i)\s*affairs\s+delete\s+(\d+)\s*", text)
    if m:
        idx=int(m.group(1)); ids=LAST.get(chat)
        if not ids or idx<1 or idx>len(ids):
            await u.message.reply_text("Сначала /affairs."); return
        tid=ids[idx-1]; t=get_task(tid)
        if t: delete_task(t.id); await u.message.reply_text(f"🗑 Удалено: «{t.title}»")
        else: await u.message.reply_text("Это дело уже удалено.")
        return

    now=datetime.now(TZ)
    p=parse(text, now)
    if not p:
        await u.message.reply_text("⚠ Не понял. Пример: «через 5 минут поесть» или «сегодня в 18:30 позвонить».")
        return

    tid=add_task(chat, p.title, p.typ, p.run_utc, p.h, p.m, p.d)
    t=get_task(tid)
    await schedule(c.application, t)
    if t.type=="once": msg=f"Отлично, напомню: «{t.title}» — {fmt(t.run_at_utc)}"
    elif t.type=="daily": msg=f"Отлично, напомню: каждый день в {t.hour:02d}:{t.minute:02d} — «{t.title}»"
    else: msg=f"Отлично, напомню: каждое {t.day_of_month} число в {t.hour:02d}:{t.minute:02d} — «{t.title}»"
    await u.message.reply_text(msg)

# ==== MAIN ====
def main():
    start_health()
    init_db()
    app=Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("affairs", affairs_cmd))
    app.add_handler(CommandHandler("affairs_delete", affairs_delete_cmd))
    app.add_handler(CommandHandler("keys_left", keys_left_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    async def on_start(app_):
        await app_.bot.delete_webhook(drop_pending_updates=True)
        await reschedule_all(app_)
        import telegram, sys
        log.info("Bot started. TZ=%s | PTB=%s | Python=%s", TZ, getattr(telegram,"__version__","?"), sys.version)
    app.post_init=on_start
    app.run_polling()

if __name__=="__main__":
    main()
