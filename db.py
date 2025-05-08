# db.py

import sqlite3

def get_connection():
    """
    Opens (or creates) the local file upwork_bot.db
    and returns a connection you can run SQL on.
    """
    conn = sqlite3.connect("upwork_bot.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Creates a table 'users' if it doesn't exist already.
    Each user will have:
      - user_id    : Telegram’s numeric ID
      - skills     : comma-separated list of your skills
      - min_budget : your minimum budget preference
    """
    conn = get_connection()
    conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
        user_id    INTEGER PRIMARY KEY,
        skills     TEXT,
        min_budget INTEGER
      )
    """)
    conn.commit()
    conn.close()