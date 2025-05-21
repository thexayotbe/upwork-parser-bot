import re
import logging
from typing import Optional, Tuple, Set
from dataclasses import dataclass

from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from playwright.async_api import async_playwright

import config

# ─── Logging & Bot Setup ─────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# ─── Constants & In-Memory Stores ─────────────────────────────────────────────

UPWORK_DOMAINS    = ('upwork.com', 'www.upwork.com')
REQUEST_TIMEOUT_MS = 15_000      # 15 seconds in milliseconds
CACHE_TIMEOUT      = 300         # 5 minutes

@dataclass
class UserPreferences:
    skills: Set[str]
    min_budget: Optional[int]       = None
    preferred_duration: Optional[str] = None

USER_PREF_STORE: dict[int, UserPreferences] = {}
EXPECTING_FIELD : dict[int, str]      = {}  # maps user_id -> "skills"|"budget"|"duration"
JOB_URLS         : dict[str, str]      = {}  # maps short job_id -> full URL

# ─── SCRAPING HELPERS ────────────────────────────────────────────────────────

async def fetch_upwork_job_with_browser(url: str, timeout: int = REQUEST_TIMEOUT_MS) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=timeout)

        # Дождись всех критичных блоков
        await page.wait_for_selector('h1.m-0.h4', timeout=timeout)
        await page.wait_for_selector('section[data-test="job-description-section"]', timeout=timeout)
        await page.wait_for_selector('section[data-test="skills-section"]', timeout=timeout)

        html = await page.content()
        await browser.close()
        return html

async def parse_upwork_job(
    url: str
) -> Tuple[
    str,             # title
    int,             # budget
    Set[str],        # skills
    Optional[str],   # experience level
    Optional[str],   # project type
    Optional[str],   # location type
    Optional[str]    # posted age (as "duration")
]:
    """Extract title, budget, skills, expertise, project_type, location_type, posted."""
    html = await fetch_upwork_job_with_browser(url)
    soup = BeautifulSoup(html, 'html.parser')

    # 1) Title
    title_el = soup.select_one('h1.m-0.h4')
    title = title_el.get_text(strip=True) if title_el else "No title"

    # 2) Budget (fixed-price)
    budget = 0
    fixed_el = soup.select_one('li[data-cy="fixed-price"] strong')
    if fixed_el:
        text = fixed_el.get_text(strip=True)
        budget = int(re.sub(r"[^\d]", "", text) or 0)

    # 3) Expertise level
    exp_el = soup.select_one('li[data-cy="expertise"] strong')
    experience = exp_el.get_text(strip=True) if exp_el else None

    # 4) Project type (one-time / hourly / etc)
    proj_el = soup.select_one('li[data-cy="briefcase-outlined"] strong')
    project_type = proj_el.get_text(strip=True) if proj_el else None

    # 5) Location type (remote / on-site)
    loc_el = soup.select_one('li[data-cy="local"] strong')
    location_type = loc_el.get_text(strip=True) if loc_el else None

    skills: Set[str] = set()
    skills_section = soup.select_one('section[data-test="skills-section"]')
    if skills_section:
        for skill_tag in skills_section.select('a'):
            skill = skill_tag.get_text(strip=True).lower()
            if skill:
                skills.add(skill)

    # 7) Posted age (use as “duration”)
    posted_el = soup.select_one('.posted-on-line span')
    posted    = posted_el.get_text(strip=True) if posted_el else None

    return title, budget, skills, experience, project_type, location_type, posted





# ─── USER PREF HELPERS ───────────────────────────────────────────────────────

async def get_user_preferences(user_id: int) -> Optional[UserPreferences]:
    return USER_PREF_STORE.get(user_id)

async def check_duration(job_age: Optional[str], preferred: Optional[str]) -> bool:
    if not preferred or not job_age:
        return True
    return preferred.lower() in job_age.lower()

async def calculate_match(job_skills: Set[str], user_skills: Set[str]) -> int:
    if not job_skills:
        return 0
    common = job_skills & user_skills
    return int((len(common) / len(job_skills)) * 100)

# ─── BUILD RESPONSE & KEYBOARD ───────────────────────────────────────────────

async def build_response(**kw) -> str:
    verdict = (
        "🟢 Отличный вариант!" if kw['match_percent'] >= 70 else
        "🔴 Не подходит"         if kw['match_percent'] <= 30 else
        "🟡 Требует уточнения"
    )
    budget_status   = "✅" if kw['budget_ok'] else "❌"
    duration_status = "✅" if kw['duration_ok'] else "❌"
    skills_list     = ', '.join(kw['skills']) if kw['skills'] else 'Не указаны'

    # You can extend this block to show experience, project_type, location_type…
    return (
        f"<b>{verdict}</b>\n\n"
        f"📌 <b>{kw['title']}</b>\n"
        f"🔗 {kw['url']}\n\n"
        f"💰 <b>Бюджет:</b> ${kw['budget']} {budget_status}\n"
        f"⏳ <b>Возраст вакансии:</b> {kw['duration'] or 'Не указано'} {duration_status}\n"
        f"🎯 <b>Совпадение навыков:</b> {kw['match_percent']}%\n"
        f"📊 <b>Требуемые навыки:</b> {skills_list}"
    )

async def build_actions_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Взять заказ", callback_data=f"accept:{job_id}"),
            InlineKeyboardButton(text="👎 Пропустить", callback_data="skip"),
        ],
        [
            InlineKeyboardButton(text="🔍 Открыть вакансию", url=JOB_URLS[job_id]),
        ],
    ])

# ─── HANDLERS ────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🔍 <b>Upwork Job Analyzer Bot</b>\n\n"
        "Я помогу анализировать вакансии:\n"
        "1. Проверю навыки\n"
        "2. Сравню бюджет\n"
        "3. Проанализирую возраст вакансии\n\n"
        "Используйте:\n"
        "/set_skills, /set_budget, /set_duration\n"
        "А потом отправьте ссылку на вакансию",
        parse_mode=ParseMode.HTML
    )

@dp.message(Command("set_skills"))
async def set_skills(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "skills"
    await message.answer("📝 Введите ваши навыки через запятую:")

@dp.message(Command("set_budget"))
async def set_budget(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "budget"
    await message.answer("💰 Введите минимальный бюджет в $:")

@dp.message(Command("set_duration"))
async def set_duration(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "duration"
    await message.answer("⏳ Введите, как давно должна быть вакансия (напр.: 'last week'):")

@dp.message(lambda msg: msg.from_user.id in EXPECTING_FIELD)
async def handle_pref_input(message: Message):
    user_id = message.from_user.id
    field   = EXPECTING_FIELD.pop(user_id)
    text    = message.text.strip()

    prefs = USER_PREF_STORE.get(user_id) or UserPreferences(skills=set())

    if field == "skills":
        prefs.skills = {s.strip().lower() for s in text.split(",") if s.strip()}
        await message.answer(f"✅ Навыки сохранены: {', '.join(prefs.skills)}")
    elif field == "budget":
        try:
            prefs.min_budget = int(re.sub(r"[^\d]", "", text))
            await message.answer(f"✅ Минимальный бюджет: ${prefs.min_budget}")
        except ValueError:
            EXPECTING_FIELD[user_id] = "budget"
            return await message.answer("⚠️ Введите число цифрами.")
    elif field == "duration":
        prefs.preferred_duration = text
        await message.answer(f"✅ Предпочитаемая давность вакансии: {text}")

    USER_PREF_STORE[user_id] = prefs

@dp.message(F.text.regexp(r'^https?://'))
async def analyze_job(message: Message):
    url = message.text.strip()
    if not any(d in url for d in UPWORK_DOMAINS):
        return await message.answer("⚠️ Пожалуйста, отправьте ссылку с upwork.com.")

    # Extract a short job_id for callback_data
    m = re.search(r'/jobs/~([^/?]+)', url)
    if not m:
        return await message.answer("⚠️ Не удалось распознать ID вакансии в ссылке.")
    job_id = m.group(1)
    JOB_URLS[job_id] = url

    processing = await message.answer("🔍 Анализирую вакансию...")
    try:
        # Unpack exactly seven values
        title, budget, job_skills, experience, project_type, location_type, posted = (
            await parse_upwork_job(url)
        )

        prefs = await get_user_preferences(message.from_user.id)
        if not prefs or not prefs.skills:
            return await processing.edit_text("⚠️ Сначала установите навыки (/set_skills)")

        match_pct   = await calculate_match(job_skills, prefs.skills)
        budget_ok   = (budget >= prefs.min_budget) if prefs.min_budget else True
        duration_ok = await check_duration(posted, prefs.preferred_duration)

        resp = await build_response(
            url=url,
            title=title,
            budget=budget,
            skills=job_skills,
            match_percent=match_pct,
            budget_ok=budget_ok,
            duration=posted,
            duration_ok=duration_ok
        )

        await processing.edit_text(
            resp,
            reply_markup=await build_actions_keyboard(job_id),
            parse_mode=ParseMode.HTML
        )

    except Exception as e:
        logger.error(f"Job analysis error: {e}")
        await processing.edit_text("⚠️ Ошибка при анализе вакансии. Попробуйте позже.")

@dp.callback_query(F.data.startswith("accept:"))
async def accept_job(callback: CallbackQuery):
    job_id = callback.data.split(":", 1)[1]
    url    = JOB_URLS.get(job_id)
    await callback.message.edit_reply_markup(None)
    if url:
        await callback.message.answer(f"🎉 Вы приняли заказ: {url}")
    else:
        await callback.message.answer("⚠️ Не удалось найти ссылку для этого заказа.")
    await callback.answer()

@dp.callback_query(F.data == "skip")
async def skip_job(callback: CallbackQuery):
    await callback.message.edit_reply_markup(None)
    await callback.message.answer("⏭ Вы пропустили этот заказ.")
    await callback.answer()

if __name__ == "__main__":
    dp.run_polling(bot)