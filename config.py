import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'super-secret-key-change-in-production')
    NEWS_API_KEY = os.getenv('NEWS_API_KEY')          # newsapi.org (kept as fallback; free tier is 24h-delayed, localhost-only)
    NEWSDATA_API_KEY = os.getenv('NEWSDATA_API_KEY')  # newsdata.io (real-time, free tier allows production use)
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL', 'sqlite:///site.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False