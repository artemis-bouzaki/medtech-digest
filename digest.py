"""
Medtech / Health-AI research digest.

Pulls recent papers from arXiv and PubMed, filters them against a keyword
watchlist, summarises the relevant ones with Claude, and emails a short
digest. Designed to run twice a week via GitHub Actions.

Configure everything in the CONFIG section below, and set these as
GitHub Actions secrets (Settings -> Secrets and variables -> Actions):
  ANTHROPIC_API_KEY   - your Anthropic API key
  SMTP_HOST           - e.g. smtp.gmail.com
  SMTP_PORT           - e.g. 587
  SMTP_USER           - your email address (sender)
  SMTP_PASSWORD       - app password (NOT your normal password)
  EMAIL_TO            - where the digest should be sent (can be same as SMTP_USER)
"""

import os
import smtplib
import ssl
import textwrap
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import json

# ============================== CONFIG ======================================

# How far back to look for new papers each run (keep this >= your schedule
# interval so you don't miss anything if a run fails).
LOOKBACK_DAYS = 5

# Your watchlist. Case-insensitive substring match against title + abstract.
# Edit this freely -- it's the main lever for relevance.
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

# arXiv categories to search within (physics.med-ph, eess.IV = image/video
# processing, cs.CV = computer vision -- these three cover most medical
# imaging AI preprints).
ARXIV_CATEGORIES = ["eess.IV", "cs.CV", "physics.med-ph"]

# PubMed search terms (broader clinical/journal side). This is OR'd with
# the KEYWORDS list automatically -- edit only if you want extra PubMed-only
# terms.
PUBMED_EXTRA_TERMS = ["radiotherapy AI", "medical imaging deep learning"]

MAX_ITEMS_PER_SOURCE = 40          # how many raw records to pull before filtering
MAX_ITEMS_IN_EMAIL = 20            # cap so the email stays short
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# =============================================================================


def anthropic_summarize(items):
    """Summarize a batch of papers in one Claude call. Returns list of dicts
    with an added 'summary' field, in the same order as input."""
    api_key = os.environ["ANTHROPIC_API_KEY"]

    numbered = "\n\n".join(
        f"[{i}] TITLE: {it['title']}\nABSTRACT: {it['abstract'][:1200]}"
        for i, it in enumerate(items)
    )

    prompt = textwrap.dedent(f"""
        You are helping a radiotherapy/medical-imaging AI researcher triage
        new papers. Their focus areas: radiotherapy outcome prediction, pseudo-CT
        synthesis from MRI/PET, pelvic/sacral insufficiency fracture detection,
        statistical shape modelling, breast MRI treatment response prediction,
        and clinical AI deployment/calibration.

        For each paper below, write ONE plain-English sentence (max 30 words)
        capturing what's actually new or useful about it for someone in this
        field -- not a generic abstract rephrase. Be direct; skip filler like
        "This paper presents".

        Return ONLY a JSON array of strings, one per paper, in the same order,
        no preamble, no markdown fences.

        Papers:
        {numbered}
    """).strip()

    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 2000,
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    text = "".join(block["text"] for block in data["content"] if block["type"] == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    summaries = json.loads(text)

    for it, summary in zip(items, summaries):
        it["summary"] = summary
    return items


def matches_keywords(text, keywords):
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def fetch_arxiv():
    """Query arXiv API for recent papers in the configured categories,
    then filter locally by keyword."""
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
            "source": "arXiv",
            "title": title,
            "abstract": abstract,
            "url": link,
        })

    return results


def fetch_pubmed():
    """Query PubMed via NCBI E-utilities for recent items matching the
    keyword list, then fetch abstracts."""
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

    efetch_params = {
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "xml",
    }
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
            "source": "PubMed",
            "title": title,
            "abstract": abstract or "(no abstract available)",
            "url": url,
        })

    return results


def build_email_html(items):
    date_label = datetime.now().strftime("%A %d %B %Y")
    if not items:
        body = "<p>No new matching papers today.</p>"
    else:
        rows = []
        for it in items:
            rows.append(f"""
                <div style="margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid #eee;">
                  <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">{it['source']}</div>
                  <a href="{it['url']}" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;">{it['title']}</a>
                  <p style="margin:6px 0 0 0;font-size:14px;color:#333;">{it.get('summary', '')}</p>
                </div>
            """)
        body = "".join(rows)

    return f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;">
      <h2 style="font-size:18px;">Medtech / Health-AI digest -- {date_label}</h2>
      <p style="font-size:13px;color:#888;">{len(items)} new items matched your watchlist.</p>
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


def main():
    print("Fetching arXiv...")
    arxiv_items = fetch_arxiv()
    print(f"  {len(arxiv_items)} matching arXiv items")

    print("Fetching PubMed...")
    pubmed_items = fetch_pubmed()
    print(f"  {len(pubmed_items)} matching PubMed items")

    items = (arxiv_items + pubmed_items)[:MAX_ITEMS_IN_EMAIL]

    if items:
        print(f"Summarizing {len(items)} items with Claude...")
        items = anthropic_summarize(items)

    html = build_email_html(items)
    subject = f"Medtech/Health-AI digest -- {len(items)} new items ({datetime.now().strftime('%d %b')})"

    print("Sending email...")
    send_email(html, subject)
    print("Done.")


if __name__ == "__main__":
    main()
