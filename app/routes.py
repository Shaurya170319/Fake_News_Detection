from flask import Blueprint, render_template, request, jsonify
import requests
import re
from datetime import datetime, timedelta, timezone
from app.utils import predict_news
from config import Config

bp = Blueprint('main', __name__)

# How far back to look for "real-time" corroborating coverage before widening the search.
FRESHNESS_WINDOW_DAYS = 7
NEWSAPI_URL = "https://newsapi.org/v2/everything"
NEWSDATA_URL = "https://newsdata.io/api/1/latest"  # real-time, past-48h feed, free tier allows production use

@bp.route('/')
def index():
    return render_template('index.html')

def _newsapi_request(params):
    """Single NewsAPI (newsapi.org) call. Returns list of articles, or None on error."""
    try:
        resp = requests.get(NEWSAPI_URL, params=params, timeout=10)
        data = resp.json()
        if data.get('status') != 'ok':
            return None
        return data.get('articles', [])
    except requests.exceptions.RequestException:
        return None

def _newsdata_request(params):
    """Single NewsData.io call. Returns a list of articles normalized to the
    same shape the templates expect (title, url, publishedAt, source.name),
    or None on error."""
    try:
        resp = requests.get(NEWSDATA_URL, params=params, timeout=10)
        data = resp.json()
        if data.get('status') != 'success':
            return None
        return [
            {
                'title': a.get('title') or '',
                'url': a.get('link') or '#',
                'publishedAt': a.get('pubDate') or '',
                'source': {'name': a.get('source_name') or a.get('source_id') or 'Unknown source'}
            }
            for a in data.get('results', [])
        ]
    except requests.exceptions.RequestException:
        return None

def extract_entities(raw_text, max_entities=8):
    """
    Pull out likely proper nouns (people, places, orgs) from the pasted text —
    a lightweight stand-in for real NER. Used to check whether a candidate
    article is actually ABOUT this story, not just sharing generic keywords.
    """
    generic_capitalized = {
        'the', 'this', 'that', 'these', 'those', 'a', 'an', 'in', 'on', 'at',
        'for', 'with', 'after', 'before', 'during', 'according', 'however',
        'meanwhile', 'today', 'yesterday', 'tomorrow', 'it', 'its'
    }
    candidates = re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', raw_text)
    entities, seen = [], set()
    for word in candidates:
        lw = word.lower()
        if lw in generic_capitalized or lw in seen:
            continue
        seen.add(lw)
        entities.append(word)
        if len(entities) >= max_entities:
            break
    return entities

def _tag_and_rank(articles, entities):
    """Mark each article as a 'strong' match (entity in the title) or a
    weaker keyword-only match, and sort strong matches first."""
    if not articles:
        return []
    patterns = [re.compile(r'\b' + re.escape(e) + r'\b', re.IGNORECASE) for e in entities]
    tagged = []
    for art in articles:
        title = art.get('title') or ''
        art = dict(art)
        art['_strong_match'] = bool(patterns) and any(p.search(title) for p in patterns)
        tagged.append(art)
    tagged.sort(key=lambda a: not a['_strong_match'])
    return tagged

def _fetch_from_newsdata(raw_text, words):
    """Real-time source: NewsData.io's /latest endpoint only returns
    articles from the past 48 hours, so genuinely same-day stories work here."""
    api_key = Config.NEWSDATA_API_KEY
    if not api_key:
        return None

    base_params = {'apikey': api_key, 'language': 'en', 'size': 5}

    # Stage 1: specific query
    articles = _newsdata_request({**base_params, 'q': ' '.join(words[:12])})

    # Stage 2: broaden the query if nothing turned up
    if not articles and len(words) > 4:
        articles = _newsdata_request({**base_params, 'q': ' '.join(words[:4])})

    return articles

def _fetch_from_newsapi(raw_text, words):
    """Fallback source: newsapi.org. Free tier is delayed ~24h and blocks
    non-localhost use, so this is only used when no NewsData.io key is set."""
    api_key = Config.NEWS_API_KEY
    if not api_key:
        return None

    from_date = (datetime.now(timezone.utc) - timedelta(days=FRESHNESS_WINDOW_DAYS)).strftime('%Y-%m-%d')
    base_params = {'apiKey': api_key, 'language': 'en', 'pageSize': 5}

    # Stage 1: recent + specific
    params = {**base_params, 'q': ' '.join(words[:12]), 'sortBy': 'publishedAt', 'from': from_date}
    articles = _newsapi_request(params)

    # Stage 2: drop the date limit, rank by relevancy instead
    if not articles:
        params = {**base_params, 'q': ' '.join(words[:12]), 'sortBy': 'relevancy'}
        articles = _newsapi_request(params)

    # Stage 3: broaden the query itself
    if not articles and len(words) > 4:
        params = {**base_params, 'q': ' '.join(words[:4]), 'sortBy': 'relevancy'}
        articles = _newsapi_request(params)

    return articles

def fetch_corroborating_articles(raw_text):
    """
    Search for corroborating articles. Used by both the automatic verdict and
    the manual "See Matching Articles" button, so the two always agree.

    Prefers NewsData.io (real-time, free tier allows production use). Falls
    back to NewsAPI (delayed ~24h, dev-only free tier) only if no NewsData.io
    key is configured, so the app keeps working either way.

    Results are tagged 'strong' (the story's own proper nouns appear in the
    article title) vs 'weak' (only generic keyword overlap, e.g. a daily
    news-roundup column) so downstream code doesn't treat the two the same.
    """
    if not Config.NEWSDATA_API_KEY and not Config.NEWS_API_KEY:
        return None

    words = re.sub(r'[^a-zA-Z0-9\s]', '', raw_text).split()
    if not words:
        return []

    entities = extract_entities(raw_text)

    articles = _fetch_from_newsdata(raw_text, words)
    if not articles:
        articles = _fetch_from_newsapi(raw_text, words)

    return _tag_and_rank(articles, entities)[:5]

def combine_verdict(ml_result, articles):
    """Combine ML prediction + article verification into one confident verdict."""
    ml_prediction = ml_result['prediction']
    ml_confidence = ml_result['confidence']

    articles = articles or []
    strong = [a for a in articles if a.get('_strong_match')]
    weak = [a for a in articles if not a.get('_strong_match')]
    has_strong = bool(strong)
    has_weak_only = bool(weak) and not has_strong

    if ml_prediction == 'REAL' and has_strong:
        verdict = 'LIKELY REAL'
        explanation = 'Model predicts real, and articles specifically about this story were found.'
        level = 'high'
    elif ml_prediction == 'REAL' and has_weak_only:
        verdict = 'WEAKLY CORROBORATED'
        explanation = ('Model predicts real, but only loosely related coverage was found — no article '
                        'title specifically names the people/places in this story. Treat with caution, '
                        'and check the sources yourself.')
        level = 'medium'
    elif ml_prediction == 'REAL' and not articles:
        verdict = 'UNVERIFIED'
        explanation = 'Model predicts real, but no related articles were found online.'
        level = 'medium'
    elif ml_prediction == 'FAKE' and has_strong:
        verdict = 'CONFLICTING SIGNALS'
        explanation = 'Model flagged this as fake, but articles specifically about this story exist. Check dates/sources.'
        level = 'medium'
    else:
        verdict = 'LIKELY FAKE'
        explanation = 'Model predicts fake, and no directly corroborating articles were found online.'
        level = 'high'

    if ml_confidence < 70:
        level = 'low'
        explanation += ' Note: model confidence is low — treat prediction cautiously.'

    return {
        'verdict': verdict,
        'explanation': explanation,
        'confidence_level': level,
        'articles_found': len(strong),
        'related_articles_found': len(articles)
    }

@bp.route('/predict', methods=['POST'])
def predict():
    text = request.form.get('news_text')
    if not text:
        return jsonify({'error': 'No text provided'}), 400

    # 1. Run the ML model
    result = predict_news(text)

    # 2. Auto-verify online in the background (doesn't block the page,
    #    but happens before rendering so the combined verdict is ready)
    articles = fetch_corroborating_articles(text)
    combined = combine_verdict(result, articles)

    return render_template('result.html',
                         text=text,
                         result=result,
                         combined=combined)

@bp.route('/verify-online', methods=['POST'])
def verify_online():
    raw_text = request.form.get('news_text', '')
    if not raw_text:
        return jsonify({'error': 'No text provided'}), 400

    if not Config.NEWSDATA_API_KEY and not Config.NEWS_API_KEY:
        return jsonify({'error': 'No news API key configured (set NEWSDATA_API_KEY or NEWS_API_KEY)'}), 500

    articles = fetch_corroborating_articles(raw_text)
    return jsonify({'articles': articles or []})