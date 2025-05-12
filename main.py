#!/usr/bin/env python3
import sys
import re
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright

def fetch_and_parse(url: str) -> dict:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('h1.break-word', timeout=30000)

        # 1) Title
        title = page.locator('h1.break-word').inner_text().strip()

        # 2) Description
        desc_el = page.locator('section[data-test="description"]')
        description = desc_el.inner_text().strip() if desc_el.count() else 'N/A'

        # 3) Budget / Rate
        budget_el = page.locator('span.js-budget')
        if not budget_el.count():
            budget_el = page.locator('strong.mr-2')
        budget = budget_el.first.inner_text().strip() if budget_el.count() else 'N/A'

        # 4) Skills
        skills = page.locator('a[data-test="skill-tag"]').all_inner_texts()

        # 5) Posted time → datetime
        posted_txt = page.locator('span[data-test="posted-on"]').first.inner_text().lower()
        now = datetime.now()
        if 'yesterday' in posted_txt:
            posted_dt = now - timedelta(days=1)
        elif 'hour' in posted_txt:
            hrs = int(re.search(r'\d+', posted_txt).group())
            posted_dt = now - timedelta(hours=hrs)
        elif 'day' in posted_txt:
            days = int(re.search(r'\d+', posted_txt).group())
            posted_dt = now - timedelta(days=days)
        else:
            posted_dt = now

        browser.close()

    return {
        'title': title,
        'description': description,
        'budget': budget,
        'skills': skills,
        'posted_datetime': posted_dt,
    }

def main():
    if len(sys.argv) != 2:
        print("Usage: python job_parser.py <Upwork-job-URL>")
        sys.exit(1)

    url = sys.argv[1]
    try:
        info = fetch_and_parse(url)
    except Exception as e:
        print(f"Error fetching or parsing: {e}")
        sys.exit(1)

    print(f"Title      : {info['title']}")
    print(f"Budget     : {info['budget']}")
    print(f"Posted     : {info['posted_datetime']}")
    print("Skills     :", ", ".join(info['skills']) or "N/A")
    print("\nDescription:")
    print(info['description'] or "N/A")

if __name__ == '__main__':
    main()