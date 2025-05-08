import re
import logging
from typing import Optional, Tuple
from dataclasses import dataclass

from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from playwright.async_api import async_playwright

import config

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot and dispatcher
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# Constants
UPWORK_DOMAINS = ('upwork.com', 'www.upwork.com')
REQUEST_TIMEOUT_MS = 15_000  # Playwright timeout in milliseconds
CACHE_TIMEOUT = 300  # 5 minutes

# Data classes and in-memory stores
@dataclass
class UserPreferences:
    skills: set
    min_budget: Optional[int] = None
    preferred_duration: Optional[str] = None

USER_PREF_STORE: dict[int, UserPreferences] = {}
EXPECTING_FIELD: dict[int, str] = {}        # user_id -> "skills"|"budget"|"duration"
JOB_URLS: dict[str, str] = {}               # job_id -> full URL


async def fetch_upwork_job_with_browser(url: str, timeout: int = REQUEST_TIMEOUT_MS) -> str:
    """Fetch the raw HTML of an Upwork job page using a headless browser."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, timeout=timeout)
        content = await page.content()
        await browser.close()
        return content


async def parse_upwork_job(url: str) -> Tuple[str, int, set, Optional[str]]:
    """Parse Upwork job page using Playwright to avoid 403s."""
    try:
        html = await fetch_upwork_job_with_browser(url)
        soup = BeautifulSoup(html, 'html.parser')

        # Extract title
        title_tag = soup.find('h1')
        title = title_tag.get_text(strip=True) if title_tag else "No title"

        # Extract budget
        budget = 0
        budget_el = soup.select_one('.air3-text-heading-large')
        if budget_el:
            text = budget_el.get_text(strip=True)
            budget = int(re.sub(r"[^\d]", "", text)) if text else 0

        # Extract skills
        skills_els = soup.select('.air3-chip-text')
        skills = {e.get_text(strip=True).lower() for e in skills_els if e.get_text(strip=True)}

        # Extract duration
        duration = None
        dur_el = soup.select_one('[data-test="duration"]')
        if dur_el:
            duration = dur_el.get_text(strip=True)

        return title, budget, skills, duration

    except Exception as e:
        logger.error(f"Error parsing Upwork job via Playwright: {e}")
        raise


async def get_user_preferences(user_id: int) -> Optional[UserPreferences]:
    return USER_PREF_STORE.get(user_id)


async def check_duration(job_duration: Optional[str], preferred_duration: Optional[str]) -> bool:
    if not preferred_duration or not job_duration:
        return True
    return preferred_duration.lower() in job_duration.lower()


async def calculate_match(job_skills: set, user_skills: set) -> int:
    if not job_skills:
        return 0
    common = job_skills & user_skills
    return int((len(common) / len(job_skills)) * 100)


async def build_response(**kw) -> str:
    verdict = (
        "🟢 Отличный вариант!" if kw['match_percent'] >= 70 else
        "🔴 Не подходит" if kw['match_percent'] <= 30 else
        "🟡 Требует уточнения"
    )
    budget_status = "✅" if kw['budget_ok'] else "❌"
    duration_status = "✅" if kw['duration_ok'] else "❌"
    skills_list = ', '.join(kw['skills']) if kw['skills'] else 'Не указаны'

    return (
        f"<b>{verdict}</b>\n\n"
        f"📌 <b>{kw['title']}</b>\n"
        f"🔗 {kw['url']}\n\n"
        f"💰 <b>Бюджет:</b> ${kw['budget']} {budget_status}\n"
        f"⏳ <b>Срок:</b> {kw['duration'] or 'Не указан'} {duration_status}\n"
        f"🎯 <b>Совпадение навыков:</b> {kw['match_percent']}%\n"
        f"📊 <b>Требуемые навыки:</b> {skills_list}"
    )


async def build_actions_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Use job_id in callback_data to stay under Telegram’s 64-byte limit."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Взять заказ", callback_data=f"accept:{job_id}"),
            InlineKeyboardButton(text="👎 Пропустить", callback_data="skip"),
        ],
        [
            InlineKeyboardButton(text="🔍 Открыть вакансию", url=JOB_URLS[job_id]),
        ],
    ])


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🔍 <b>Upwork Job Analyzer Bot</b>\n\n"
        "Я помогу анализировать вакансии:\n"
        "1. Проверю навыки\n"
        "2. Сравню бюджет\n"
        "3. Проанализирую сроки\n\n"
        "Используйте:\n"
        "/set_skills, /set_budget, /set_duration\n"
        "Затем отправьте ссылку на вакансию",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("set_skills"))
async def set_skills(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "skills"
    await message.answer(
        "📝 Введите ваши навыки через запятую:\n"
        "<i>Напр.: Python, Django, PostgreSQL</i>",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("set_budget"))
async def set_budget(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "budget"
    await message.answer(
        "💰 Введите минимальный бюджет в $:\n"
        "<i>Напр.: 500</i>",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("set_duration"))
async def set_duration(message: Message):
    EXPECTING_FIELD[message.from_user.id] = "duration"
    await message.answer(
        "⏳ Введите предпочитаемые сроки (например: ‘1–3 weeks’):",
        parse_mode=ParseMode.HTML
    )


@dp.message(lambda msg: msg.from_user.id in EXPECTING_FIELD)
async def handle_pref_input(message: Message):
    user_id = message.from_user.id
    field = EXPECTING_FIELD.pop(user_id)
    text = message.text.strip()

    prefs = USER_PREF_STORE.get(user_id) or UserPreferences(skills=set())

    if field == "skills":
        skills = {s.strip().lower() for s in text.split(",") if s.strip()}
        prefs.skills = skills
        await message.answer(f"✅ Навыки сохранены: {', '.join(skills)}")
    elif field == "budget":
        try:
            b = int(re.sub(r"[^\d]", "", text))
            prefs.min_budget = b
            await message.answer(f"✅ Минимальный бюджет: ${b}")
        except ValueError:
            EXPECTING_FIELD[user_id] = "budget"
            return await message.answer("⚠️ Введите число цифрами.")
    elif field == "duration":
        prefs.preferred_duration = text
        await message.answer(f"✅ Предпочтительные сроки: {text}")

    USER_PREF_STORE[user_id] = prefs


@dp.message(F.text.regexp(r'^https?://'))
async def analyze_job(message: Message):
    url = message.text.strip()
    if not any(d in url for d in UPWORK_DOMAINS):
        return await message.answer("⚠️ Отправьте ссылку на Upwork.")

    # extract short job_id for callback_data
    m = re.search(r'/jobs/~([^/?]+)', url)
    if not m:
        return await message.answer("⚠️ Не удалось распознать ID вакансии.")
    job_id = m.group(1)
    JOB_URLS[job_id] = url

    processing = await message.answer("🔍 Анализирую вакансию...")
    try:
        title, budget, job_skills, duration = await parse_upwork_job(url)
        prefs = await get_user_preferences(message.from_user.id)
        if not prefs or not prefs.skills:
            return await processing.edit_text("⚠️ Сначала установите навыки (/set_skills)")

        match_pct = await calculate_match(job_skills, prefs.skills)
        budget_ok   = (budget >= prefs.min_budget) if prefs.min_budget else True
        duration_ok = await check_duration(duration, prefs.preferred_duration)

        resp = await build_response(
            url=url, title=title, budget=budget, duration=duration,
            skills=job_skills, match_percent=match_pct,
            budget_ok=budget_ok, duration_ok=duration_ok
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
    url = JOB_URLS.get(job_id)
    await callback.message.edit_reply_markup(None)
    if url:
        await callback.message.answer(f"🎉 Вы приняли заказ: {url}")
        # Optionally: del JOB_URLS[job_id]
    else:
        await callback.message.answer("⚠️ Не удалось найти ссылку для этого заказа.")
    await callback.answer()


@dp.callback_query(F.data == "skip")
async def skip_job(callback: CallbackQuery):
    await callback.message.edit_reply_markup(None)
    await callback.message.answer("⏭ Вы пропустили этот заказ. Ищем дальше...")
    await callback.answer()


if __name__ == "__main__":
    dp.run_polling(bot)