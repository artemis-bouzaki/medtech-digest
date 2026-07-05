"""
Medtech / Health-AI research digest.

Pulls recent papers from arXiv and PubMed, and recent articles from a set of
medtech news RSS feeds. Papers are filtered by keyword watchlist; news items
are NOT keyword-filtered, instead Claude judges relevance during summarisation. Sends the
result as a short HTML digest email.

Configure everything in the CONFIG section below, and set these as
GitHub Actions secrets (Settings -> Secrets and variables -> Actions):
  ANTHROPIC_API_KEY   - your Anthropic API key
  SMTP_HOST           - e.g. smtp.gmail.com
  SMTP_PORT           - e.g. 587
  SMTP_USER           - your email address (sender)
  SMTP_PASSWORD       - app password (NOT your normal password)
  EMAIL_TO            - where the digest should be sent (can be same as SMTP_USER)
"""

import json
import os
import re
import smtplib
import ssl
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

# ============================== CONFIG ======================================

LOOKBACK_DAYS = 5  # how far back to look each run; keep >= your schedule interval

# --- Papers (arXiv + PubMed): keyword-filtered -------------------------------

KEYWORDS = [
    "radiotherapy",
    "pseudo-CT",
    "pseudo CT",
    "MRI-to-CT",
    "MRI to CT",
    "synthetic CT",
    "sacral insufficiency fracture",
    "pelvic insufficiency fracture",
    "insufficiency fracture",
    "statistical shape model",
    "bone mineral density",
    "vertebral fracture",
    "DCE-MRI",
    "breast MRI",
    "pathologic complete response",
    "pCR prediction",
    "radiomics",
    "MRI segmentation",
    "deformable registration",
    "MONAI",
    "clinical AI calibration",
    "federated learning imaging",
]

ARXIV_CATEGORIES = ["eess.IV", "cs.CV", "physics.med-ph"]
PUBMED_EXTRA_TERMS = ["radiotherapy AI", "medical imaging deep learning"]

# --- News (RSS): not keyword-filtered, agent (Claude) judges relevance --------------

RSS_FEEDS = {
    "MobiHealthNews": "https://www.mobihealthnews.com/feed",
    "Fierce Biotech (Medtech)": "https://www.fiercebiotech.com/rss/medtech/xml",
    "MedCity News": "https://medcitynews.com/feed",
    "Medtech Dive": "https://www.medtechdive.com/feeds/news",
}

# What relevant means when agent filters news. Edit this to steer things.
RELEVANCE_BRIEF = textwrap.dedent("""
    Relevant: radiotherapy AI/oncology imaging, medical imaging AI more broadly,
    FDA clearances or funding rounds for imaging/oncology/medical technology AI companies, clinical
    AI deployment/regulatory news, women's health / femtech AI, and healthtech
    startup news relevant to someone considering a founder or early technical
    hire role in this space.
    Not relevant: general hospital operations/policy/insurance news, unrelated
    disease areas with no AI/imaging angle, consumer wellness content, biology/drug discovery startups.
""").strip()

MAX_ITEMS_PER_SOURCE = 40    # raw records pulled per source before filtering/dedup
MAX_PAPERS_IN_EMAIL = 15
MAX_NEWS_IN_EMAIL = 10
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# =============================================================================


def normalize_title(title):
    """Lowercase, strip punctuation/whitespace variance, for dedup matching."""
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


def dedup_items(items):
    """Drop items whose normalized title we've already seen. Keeps first
    occurrence (arXiv/PubMed/news feeds are processed in a fixed order, so
    this is deterministic run to run)."""
    seen = set()
    out = []
    for it in items:
        key = normalize_title(it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def matches_keywords(text, keywords):
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


# --------------------------------- arXiv ------------------------------------

def fetch_arxiv():
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cat_query = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    params = {
        "search_query": cat_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": str(MAX_ITEMS_PER_SOURCE),
    }
    url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as resp:
        raw = resp.read()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)

    results = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns).strip().replace("\n", " ")
        abstract = entry.findtext("atom:summary", default="", namespaces=ns).strip().replace("\n", " ")
        link = entry.findtext("atom:id", default="", namespaces=ns).strip()
        published = entry.findtext("atom:published", default="", namespaces=ns)

        try:
            pub_dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pub_dt = None

        if pub_dt and pub_dt < since:
            continue
        if not matches_keywords(title + " " + abstract, KEYWORDS):
            continue

        results.append({
            "type": "paper",
            "source": "arXiv",
            "title": title,
            "abstract": abstract,
            "url": link,
        })

    return results


# -------------------------------- PubMed ------------------------------------

def fetch_pubmed():
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    date_str = since.strftime("%Y/%m/%d")

    all_terms = KEYWORDS + PUBMED_EXTRA_TERMS
    term_query = " OR ".join(f'"{t}"[Title/Abstract]' for t in all_terms)
    term_query = f"({term_query}) AND ({date_str}[PDAT] : 3000[PDAT])"

    esearch_params = {
        "db": "pubmed",
        "term": term_query,
        "retmax": str(MAX_ITEMS_PER_SOURCE),
        "retmode": "json",
        "sort": "most recent",
    }
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(esearch_params)

    with urllib.request.urlopen(esearch_url, timeout=30) as resp:
        esearch_data = json.loads(resp.read())

    ids = esearch_data.get("esearchresult", {}).get("idlist", [])
    if not ids:
        return []

    efetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(efetch_params)

    with urllib.request.urlopen(efetch_url, timeout=30) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    results = []
    for article in root.findall(".//PubmedArticle"):
        title_el = article.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""

        abstract_parts = article.findall(".//Abstract/AbstractText")
        abstract = " ".join("".join(a.itertext()) for a in abstract_parts).strip()

        pmid_el = article.find(".//PMID")
        pmid = pmid_el.text if pmid_el is not None else ""
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""

        if not matches_keywords(title + " " + abstract, KEYWORDS):
            continue

        results.append({
            "type": "paper",
            "source": "PubMed",
            "title": title,
            "abstract": abstract or "(no abstract available)",
            "url": url,
        })

    return results


# ---------------------------------- RSS -------------------------------------

def _parse_rss_date(date_str):
    """RSS dates are usually RFC 822 (email-style). Fall back gracefully."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None


def fetch_rss_feed(source_name, feed_url):
    """Fetch and parse a single RSS 2.0 feed. No keyword filtering here --
    relevance is judged later by agent (claude). Only filters by LOOKBACK_DAYS."""
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0 (digest-bot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()

    root = ET.fromstring(raw)
    results = []

    # Standard RSS 2.0: <rss><channel><item>...
    for item in root.findall(".//item")[:MAX_ITEMS_PER_SOURCE]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        # strip any HTML tags that sneak into description
        description = re.sub(r"<[^>]+>", " ", description)
        description = re.sub(r"\s+", " ", description).strip()

        pub_date_str = item.findtext("pubDate")
        pub_dt = _parse_rss_date(pub_date_str)
        if pub_dt:
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < since:
                continue

        if not title:
            continue

        results.append({
            "type": "news",
            "source": source_name,
            "title": title,
            "abstract": description[:1500],
            "url": link,
        })

    return results


def fetch_all_news():
    all_news = []
    for name, url in RSS_FEEDS.items():
        try:
            items = fetch_rss_feed(name, url)
            print(f"  {name}: {len(items)} items in window")
            all_news.extend(items)
        except Exception as e:
            print(f"  {name}: FAILED ({e})")
    return all_news


# ------------------------------ agent calls --------------------------------

def anthropic_process(items):
    """
    Single batched Claude call that:
      - writes a one-sentence summary for every 'paper' item (already
        keyword-filtered, so always kept)
      - judges relevance for every 'news' item against RELEVANCE_BRIEF, and
        writes a summary only for the ones it keeps

    Returns (kept_items) with a 'summary' field added, dropping any news
    items Claude judged irrelevant.
    """
    if not items:
        return []

    api_key = os.environ["ANTHROPIC_API_KEY"]

    numbered = "\n\n".join(
        f"[{i}] TYPE: {it['type']}\nSOURCE: {it['source']}\nTITLE: {it['title']}\n"
        f"TEXT: {it['abstract'][:1200]}"
        for i, it in enumerate(items)
    )

    prompt = textwrap.dedent(f"""
        You are helping a radiotherapy/medical-imaging AI researcher triage a
        daily digest. Her focus areas: radiotherapy outcome prediction,
        pseudo-CT synthesis from MRI/PET, pelvic/sacral insufficiency fracture
        detection, statistical shape modelling, breast MRI treatment response
        prediction, clinical AI deployment/calibration, and (for news items
        specifically) healthtech/medtech industry developments relevant to
        someone considering a founder or early technical hire role.

        Below is a numbered list of items. Each is either a research paper
        (TYPE: paper) or a news article (TYPE: news).

        For TYPE: paper items -- these are already filtered as relevant.
        Always keep them. Write ONE plain-English sentence (max 30 words)
        saying what's actually new or useful about it -- not a generic
        rephrase of the abstract. Skip filler like "This paper presents".

        For TYPE: news items -- these are NOT pre-filtered. Judge relevance
        using this brief:
        {RELEVANCE_BRIEF}
        Only keep news items that clearly match. For kept items, write ONE
        plain-English sentence (max 30 words) on why it matters to her
        specifically, not just a rephrase of the headline.

        Return ONLY a JSON array, no preamble, no markdown fences. Each
        element must be an object with exactly these fields:
          "index": the item's number from the list
          "keep": true or false (papers should always be true)
          "summary": your one-sentence summary (omit or use "" if keep is false)

        Items:
        {numbered}
    """).strip()

    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read())

    text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    decisions = json.loads(text)

    by_index = {d["index"]: d for d in decisions}

    kept = []
    for i, it in enumerate(items):
        decision = by_index.get(i)
        if decision is None:
            # Claude didn't return a decision for this item -- fail safe:
            # keep papers (already filtered), drop news (unjudged relevance).
            if it["type"] == "paper":
                it["summary"] = ""
                kept.append(it)
            continue
        if not decision.get("keep"):
            continue
        it["summary"] = decision.get("summary", "")
        kept.append(it)

    return kept


# --------------------------------- Email ------------------------------------

def build_email_html(papers, news):
    date_label = datetime.now().strftime("%A %d %B %Y")

    def render_section(title, items):
        if not items:
            return f'<h3 style="font-size:14px;color:#888;">{title}</h3><p style="font-size:13px;color:#aaa;">Nothing new.</p>'
        rows = []
        for it in items:
            rows.append(f"""
                <div style="margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid #eee;">
                  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">{it['source']}</div>
                  <a href="{it['url']}" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;">{it['title']}</a>
                  <p style="margin:6px 0 0 0;font-size:14px;color:#333;">{it.get('summary', '')}</p>
                </div>
            """)
        return f'<h3 style="font-size:14px;color:#888;margin-top:24px;">{title}</h3>' + "".join(rows)

    body = render_section("Papers", papers) + render_section("News", news)

    return f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;">
      <h2 style="font-size:18px;">Medtech / Health-AI digest -- {date_label}</h2>
      <p style="font-size:13px;color:#888;">{len(papers)} papers, {len(news)} news items.</p>
      {body}
    </body></html>
    """


def send_email(html_body, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.attach(MIMEText(html_body, "html"))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_USER"], os.environ["EMAIL_TO"], msg.as_string())


# ---------------------------------- Main ------------------------------------

def main():
    print("Fetching arXiv...")
    arxiv_items = fetch_arxiv()
    print(f"  {len(arxiv_items)} matching arXiv items")

    print("Fetching PubMed...")
    pubmed_items = fetch_pubmed()
    print(f"  {len(pubmed_items)} matching PubMed items")

    print("Fetching news RSS feeds...")
    news_items = fetch_all_news()
    print(f"  {len(news_items)} total news items in window")

    papers = dedup_items(arxiv_items + pubmed_items)
    news = dedup_items(news_items)

    print(f"After dedup: {len(papers)} papers, {len(news)} news items")

    all_items = papers[:MAX_PAPERS_IN_EMAIL] + news[:MAX_NEWS_IN_EMAIL * 2]  # send extra news for Claude to filter down

    print(f"Sending {len(all_items)} items to Claude for summarizing/filtering...")
    processed = anthropic_process(all_items) if all_items else []

    final_papers = [it for it in processed if it["type"] == "paper"][:MAX_PAPERS_IN_EMAIL]
    final_news = [it for it in processed if it["type"] == "news"][:MAX_NEWS_IN_EMAIL]

    html = build_email_html(final_papers, final_news)
    subject = (
        f"Medtech/Health-AI digest -- {len(final_papers)} papers, "
        f"{len(final_news)} news ({datetime.now().strftime('%d %b')})"
    )

    print("Sending email...")
    send_email(html, subject)
    print("Done.")


if __name__ == "__main__":
    main()
