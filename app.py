import asyncio
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse
from typing import List, Dict, Optional, Tuple

import gradio as gr
import requests
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
import spacy
import langdetect
import textstat
import dateparser
import html2text
import tldextract
import phonenumbers

# ------------------- LOG YAPILANDIRMASI -------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/sessizoperator.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("SilentOperator")

# ------------------- AYARLAR -------------------
TEMP_DIR = "/tmp/sessizoperator"
os.makedirs(TEMP_DIR, exist_ok=True)
download_links = {}

# SQLite veritabanı (indirme logları için)
DB_FILE = os.path.join(TEMP_DIR, "downloads.db")
conn = sqlite3.connect(DB_FILE)
conn.execute('''CREATE TABLE IF NOT EXISTS download_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    format TEXT NOT NULL,
    ip_address TEXT,
    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)''')
conn.commit()
conn.close()

# ------------------- SPAÇY MODELLERİ -------------------
nlp_models = {}
def load_spacy_model(model_name: str):
    """SpaCy modelini yükle veya indir."""
    if model_name not in nlp_models:
        try:
            nlp_models[model_name] = spacy.load(model_name)
        except OSError:
            os.system(f"python -m spacy download {model_name}")
            nlp_models[model_name] = spacy.load(model_name)
    return nlp_models[model_name]

nlp_en_sm = load_spacy_model("en_core_web_sm")
nlp_en_md = load_spacy_model("en_core_web_md")
nlp_de = load_spacy_model("de_core_news_sm")
nlp_fr = load_spacy_model("fr_core_news_sm")

# Hukuk terimleri için Entity Ruler (İngilizce)
ruler = nlp_en_sm.add_pipe("entity_ruler", after="ner")
patterns = [
    {"label": "LEGAL_TERM", "pattern": "plaintiff"},
    {"label": "LEGAL_TERM", "pattern": "defendant"},
    {"label": "LEGAL_TERM", "pattern": "tortfeasor"},
    {"label": "LEGAL_TERM", "pattern": "lessor"},
    {"label": "LEGAL_TERM", "pattern": "lessee"},
    {"label": "LEGAL_TERM", "pattern": "indemnification"},
    {"label": "LEGAL_TERM", "pattern": "arbitration"},
    {"label": "LEGAL_TERM", "pattern": "jurisdiction"},
    {"label": "LEGAL_TERM", "pattern": "force majeure"},
    {"label": "LEGAL_TERM", "pattern": "subpoena"},
    {"label": "LEGAL_TERM", "pattern": "injunction"},
    {"label": "LEGAL_TERM", "pattern": "litigation"},
    {"label": "LEGAL_TERM", "pattern": "settlement"},
    {"label": "LEGAL_TERM", "pattern": "discovery"},
    {"label": "LEGAL_TERM", "pattern": "deposition"},
]
ruler.add_patterns(patterns)

# ------------------- KİŞİSEL VERİ MASKELEME DESENLERİ -------------------
PATTERNS = {
    "EMAIL": re.compile(r'\b[\w\.-]+@[\w\.-]+\.\w+\b'),
    "PHONE_TR": re.compile(r'(\+?90)?[\s-]?\(?5\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}'),
    "PHONE_INT": re.compile(r'(\+?\d{1,3}[\s-]?)?\(?\d{2,4}\)?[\s-]?\d{2,4}[\s-]?\d{2,4}[\s-]?\d{0,4}'),
    "TC_KIMLIK": re.compile(r'\b[1-9]\d{10}\b'),
    "IBAN": re.compile(r'\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b'),
    "IP": re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'),
    "CREDIT_CARD": re.compile(r'\b(?:\d{4}[\s-]?){3}\d{4}\b'),
    "SSN": re.compile(r'\b\d{3}-\d{2}-\d{4}\b'),
    "PASSPORT": re.compile(r'\b[A-Z0-9]{6,12}\b'),
    "DOB": re.compile(r'\b\d{2}/\d{2}/\d{4}\b'),
}

# ------------------- DOMAIN KARA VE BEYAZ LİSTELERİ -------------------
DOMAIN_BLACKLIST = {
    "pinterest.com", "youtube.com", "facebook.com", "twitter.com",
    "instagram.com", "tiktok.com", "reddit.com", "linkedin.com",
    "wikipedia.org", "amazon.com", "ebay.com"
}

DOMAIN_WHITELIST = [
    ".gov", ".edu", ".law", ".court", ".legal", ".justice"
]

# ------------------- SEKTÖR ANAHTAR KELİMELERİ (GENİŞLETİLMİŞ) -------------------
SECTOR_KEYWORDS = {
    "Legal": ["law", "attorney", "court", "legal", "jurisdiction", "privacy", "gdpr", "contract",
              "sözleşme", "hukuk", "avukat", "plaintiff", "defendant", "litigation", "settlement",
              "injunction", "subpoena", "force majeure", "indemnification"],
    "Finance": ["bank", "finance", "investment", "stock", "credit", "loan", "insurance", "mortgage",
                "crypto", "blockchain", "fintech", "capital", "equity", "dividend", "bond"],
    "Healthcare": ["hospital", "medical", "health", "patient", "doctor", "pharma", "clinical",
                   "surgery", "diagnosis", "treatment", "therapy", "prescription"],
    "Technology": ["software", "api", "cloud", "data", "saas", "ai", "ml", "app", "web",
                   "mobile", "cybersecurity", "devops", "microservices", "container"],
    "Real Estate": ["property", "real estate", "mortgage", "lease", "tenant", "landlord",
                    "broker", "appraisal", "closing", "escrow", "title"],
}

# ------------------- EŞZAMANLILIK SINIRI -------------------
SEM_LIMIT = 5
semaphore = asyncio.Semaphore(SEM_LIMIT)

# ------------------- METİN KALİTE SKORLAMA -------------------
class TextQualityScorer:
    @staticmethod
    def calculate_score(text: str) -> Dict:
        score = {
            "flesch_reading_ease": 0.0,
            "readability_grade": 0,
            "avg_sentence_length": 0,
            "vocabulary_diversity": 0.0,
            "overall_quality": 0.0
        }
        try:
            score["flesch_reading_ease"] = round(textstat.flesch_reading_ease(text), 2)
            score["readability_grade"] = textstat.text_standard(text)
            sentences = re.split(r'[.!?]+', text)
            words = text.split()
            if len(sentences) > 0 and len(words) > 0:
                score["avg_sentence_length"] = round(len(words) / len(sentences), 2)
                score["vocabulary_diversity"] = round(len(set(words)) / len(words), 4)
            score["overall_quality"] = round(
                (score["flesch_reading_ease"] / 100) * 0.4 +
                (min(score["avg_sentence_length"], 30) / 30) * 0.3 +
                score["vocabulary_diversity"] * 0.3, 4
            )
        except:
            pass
        return score

# ------------------- DİL TESPİTİ -------------------
def detect_language(text: str) -> str:
    try:
        return langdetect.detect(text)
    except:
        return "unknown"

# ------------------- ROBOTS.TXT KONTROLÜ -------------------
def check_robots_txt(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        resp = requests.get(robots_url, timeout=5)
        if "Disallow: /" in resp.text:
            logger.info(f"robots.txt disallows: {url}")
            return False
    except Exception as e:
        logger.warning(f"robots.txt check failed for {url}: {str(e)}")
    return True

# ------------------- DOMAIN FİLTRELEME -------------------
def is_domain_allowed(url: str) -> bool:
    domain = urlparse(url).netloc.lower()
    # Beyaz listede var mı kontrol et
    for whitelist_domain in DOMAIN_WHITELIST:
        if whitelist_domain in domain:
            return True
    # Kara listede yoksa izin ver
    return not any(blocked in domain for blocked in DOMAIN_BLACKLIST)

# ------------------- MASKELEME -------------------
def mask_personal_data(text: str) -> Tuple[str, int]:
    masked_count = 0
    for pattern in PATTERNS.values():
        matches = pattern.findall(text)
        masked_count += len(matches)
        text = pattern.sub('[MASKED]', text)
    return text, masked_count

def ner_anonymize(text: str, lang: str = "en") -> Tuple[str, bool]:
    # Dil modelini seç
    if lang.startswith("de"):
        nlp = nlp_de
    elif lang.startswith("fr"):
        nlp = nlp_fr
    else:
        nlp = nlp_en_md
    
    doc = nlp(text[:200000])
    masked_chars = 0
    for ent in doc.ents:
        if ent.label_ in ["PERSON", "ORG", "GPE", "LOC"]:
            text = text.replace(ent.text, f"[{ent.label_}]")
            masked_chars += len(ent.text)
    overmasked = (masked_chars / max(len(text), 1)) > 0.05
    return text, overmasked

# ------------------- SEKTÖR TESPİTİ -------------------
def detect_sector(text: str) -> Tuple[str, float]:
    text_lower = text.lower()
    scores = {}
    for sector, keywords in SECTOR_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[sector] = score
    if not scores:
        return "Other", 0.0
    best = max(scores, key=scores.get)
    confidence = scores[best] / len(SECTOR_KEYWORDS[best])
    return best, round(confidence, 4)

# ------------------- WEB ARAMA -------------------
def search_web(query: str, num: int = 10) -> List[str]:
    api_key = os.environ.get("BING_API_KEY")
    if api_key:
        urls = []
        endpoint = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        for offset in range(0, min(num, 50), 10):
            params = {"q": query, "count": 10, "offset": offset, "mkt": "en-US"}
            try:
                resp = requests.get(endpoint, headers=headers, params=params).json()
                for item in resp.get("webPages", {}).get("value", []):
                    urls.append(item["url"])
            except Exception as e:
                logger.error(f"Bing API error: {str(e)}")
        logger.info(f"Bing API returned {len(urls)} URLs for query: {query}")
        return urls[:num]
    else:
        return search_duckduckgo(query, num)

def search_duckduckgo(query: str, num: int = 10) -> List[str]:
    urls = []
    try:
        resp = requests.get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.select(".result__url"):
            href = link.get("href")
            if href:
                urls.append(href)
        logger.info(f"DuckDuckGo returned {len(urls)} URLs for query: {query}")
    except Exception as e:
        logger.error(f"DuckDuckGo search error: {str(e)}")
    return urls[:num]

# ------------------- VERİ SETİ ARAMA -------------------
def search_datasets(query: str) -> List[Dict]:
    results = []
    # HuggingFace
    try:
        resp = requests.get(f"https://huggingface.co/api/datasets?search={quote_plus(query)}&limit=20").json()
        for item in resp:
            tags = item.get("tags", [])
            license_ok = any(t in ["cc0-1.0", "mit", "apache-2.0", "openrail", "bsd-3-clause"] for t in tags)
            if license_ok:
                results.append({
                    "source": "HuggingFace",
                    "title": item.get("id", ""),
                    "url": f"https://huggingface.co/datasets/{item['id']}",
                    "license": ", ".join(tags),
                    "downloads": item.get("downloads", 0),
                    "likes": item.get("likes", 0)
                })
    except Exception as e:
        logger.warning(f"HuggingFace search error: {str(e)}")
    
    # GitHub Topics
    try:
        gh_token = os.environ.get("GITHUB_TOKEN")
        headers = {"Authorization": f"token {gh_token}"} if gh_token else {}
        resp = requests.get(
            f"https://api.github.com/search/repositories?q={quote_plus(query)}+dataset&per_page=10",
            headers=headers
        ).json()
        for item in resp.get("items", []):
            results.append({
                "source": "GitHub",
                "title": item["full_name"],
                "url": item["html_url"],
                "license": item.get("license", {}).get("name", "Unknown"),
                "stars": item.get("stargazers_count", 0)
            })
    except Exception as e:
        logger.warning(f"GitHub search error: {str(e)}")
    
    return results

# ------------------- SEMANTİC SCHOLAR ARAMA -------------------
def search_academic(query: str) -> List[Dict]:
    """Akademik makale araması."""
    results = []
    try:
        resp = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/search?query={quote_plus(query)}&limit=20&fields=title,url,year"
        ).json()
        for paper in resp.get("data", []):
            if paper.get("url"):
                results.append({
                    "source": "SemanticScholar",
                    "title": paper["title"],
                    "url": paper["url"],
                    "year": paper.get("year", 0)
                })
    except Exception as e:
        logger.warning(f"Semantic Scholar error: {str(e)}")
    return results

# ------------------- TARAMA -------------------
async def crawl_site(url: str) -> Optional[str]:
    async with semaphore:
        try:
            async with AsyncWebCrawler() as crawler:
                config = CrawlerRunConfig(max_pages=2)
                result = await crawler.arun(url, config=config)
                return result.markdown if result else None
        except Exception as e:
            logger.warning(f"Crawl4AI failed for {url}: {str(e)}")
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.ignore_images = True
                return h.handle(resp.text)
            except Exception as e2:
                logger.error(f"BS4 fallback failed for {url}: {str(e2)}")
                return None

# ------------------- İNDİRME LOGLAMA -------------------
def log_download(file_id: str, customer_email: str, format: str, ip_address: str = ""):
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        "INSERT INTO download_logs (file_id, customer_email, format, ip_address) VALUES (?, ?, ?, ?)",
        (file_id, customer_email, format, ip_address)
    )
    conn.commit()
    conn.close()
    logger.info(f"Download logged: {file_id} - {customer_email} - {format}")

# ------------------- ANA BORU HATTI -------------------
async def generate_dataset(query: str, dataset_type: str, package: str, customer_email: str,
                          language_filter: str = "all", progress=gr.Progress()) -> Tuple[str, str]:
    num_samples = int(package.split()[0])
    logger.info(f"Starting dataset generation: {query} - {package} - {customer_email}")
    
    urls = search_web(query, num_samples)
    datasets = search_datasets(query)
    academic = search_academic(query)
    
    results = []
    # Web taraması
    valid_urls = [u for u in urls if is_domain_allowed(u) and check_robots_txt(u)][:num_samples]
    tasks = [crawl_site(u) for u in valid_urls]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        text = await coro
        if text:
            lang = detect_language(text)
            if language_filter != "all" and not lang.startswith(language_filter):
                continue
            
            masked_text, mask_count = mask_personal_data(text)
            anonymized_text, overmasked = ner_anonymize(masked_text, lang)
            sector, confidence = detect_sector(anonymized_text)
            quality = TextQualityScorer.calculate_score(anonymized_text)
            
            results.append({
                "url": valid_urls[i],
                "sector": sector,
                "confidence": confidence,
                "language": lang,
                "sample_text": anonymized_text[:2000].replace("\n", " "),
                "char_count": len(text),
                "word_count": len(text.split()),
                "masked_items": mask_count,
                "overmasked": overmasked,
                "readability_grade": quality["readability_grade"],
                "vocabulary_diversity": quality["vocabulary_diversity"],
                "overall_quality": quality["overall_quality"],
                "source": "web",
                "robots_allowed": check_robots_txt(valid_urls[i])
            })
        progress((i+1)/len(valid_urls), desc=f"Web: {i+1}/{len(valid_urls)}")
    
    # Dataset sonuçları
    for ds in datasets:
        results.append({
            "url": ds["url"],
            "sector": "Other",
            "confidence": 0.0,
            "language": "en",
            "sample_text": f"{ds['title']} (License: {ds.get('license', 'Unknown')})",
            "char_count": 0,
            "word_count": 0,
            "masked_items": 0,
            "overmasked": False,
            "readability_grade": 0,
            "vocabulary_diversity": 0.0,
            "overall_quality": 0.0,
            "source": ds["source"],
            "robots_allowed": True
        })
    
    # Akademik sonuçlar
    for paper in academic:
        results.append({
            "url": paper["url"],
            "sector": "Academic",
            "confidence": 0.8,
            "language": "en",
            "sample_text": f"{paper['title']} (Year: {paper.get('year', 'N/A')})",
            "char_count": 0,
            "word_count": 0,
            "masked_items": 0,
            "overmasked": False,
            "readability_grade": 0,
            "vocabulary_diversity": 0.0,
            "overall_quality": 0.0,
            "source": paper["source"],
            "robots_allowed": True
        })
    
    if not results:
        logger.warning("No results found")
        return "⚠️ No results found. Try different keywords.", ""
    
    # Dosya oluştur
    customer_hash = hashlib.sha256(customer_email.encode()).hexdigest()[:12]
    file_id = str(uuid.uuid4())[:8]
    file_name = f"Dataset_{customer_hash}_{file_id}.csv"
    file_path = os.path.join(TEMP_DIR, file_name)
    
    keys = ["url", "sector", "confidence", "language", "sample_text", "char_count",
            "word_count", "masked_items", "overmasked", "readability_grade",
            "vocabulary_diversity", "overall_quality", "source", "robots_allowed"]
    
    with open(file_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([f"# LICENSE: AI training only. Customer ID: {customer_hash}"])
        writer.writerow([f"# Generated by Silent Operator | Query: {query} | Date: {datetime.now().isoformat()}"])
        writer.writerow(keys)
        for row in results:
            writer.writerow([row.get(k, "") for k in keys])
    
    # JSONL çıktısı
    jsonl_path = file_path.replace(".csv", ".jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    
    download_url = f"/download/{file_id}"
    download_links[file_id] = {
        "path": file_path,
        "jsonl_path": jsonl_path,
        "expires_at": datetime.now() + timedelta(hours=1),
        "customer_email": customer_email,
        "query": query,
        "count": len(results)
    }
    
    logger.info(f"Dataset generated: {file_id} - {len(results)} samples")
    return f"✅ {len(results)} samples ready! Customer ID: {customer_hash}", download_url

def download_file(file_id: str, consent: bool, format_choice: str) -> Optional[str]:
    if not consent:
        return "❌ You must accept the terms."
    info = download_links.get(file_id)
    if not info or datetime.now() > info["expires_at"]:
        return "❌ Link expired or invalid."
    
    log_download(file_id, info["customer_email"], format_choice)
    
    if format_choice == "CSV":
        return info["path"]
    else:
        return info.get("jsonl_path", info["path"])

# ------------------- İSTATİSTİKLER -------------------
def get_statistics() -> dict:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.execute("SELECT COUNT(*), COUNT(DISTINCT customer_email) FROM download_logs")
    total_downloads, unique_customers = cursor.fetchone()
    conn.close()
    return {
        "total_downloads": total_downloads,
        "unique_customers": unique_customers,
        "active_links": len(download_links)
    }

# ------------------- GRADIO ARAYÜZ -------------------
with gr.Blocks(title="Silent Operator – Enterprise Dataset Factory", css="""
    .disclaimer { font-size: 12px; color: #666; }
    .stats { background: #f5f5f5; padding: 10px; border-radius: 5px; }
""") as demo:
    gr.Markdown("# 🤫 Silent Operator – Kurumsal Veri Seti Fabrikası")
    
    with gr.Tabs():
        with gr.TabItem("📊 Generate Dataset"):
            with gr.Row():
                with gr.Column(scale=2):
                    query_input = gr.Textbox(
                        label="Konu / Anahtar Kelimeler",
                        placeholder="English legal contracts GDPR compliance",
                        lines=3
                    )
                with gr.Column(scale=1):
                    dataset_type = gr.Dropdown(
                        ["Classification", "NER", "Summarization", "Sentiment Analysis"],
                        label="Dataset Type",
                        value="Classification"
                    )
            
            with gr.Row():
                package = gr.Dropdown(
                    ["100 samples", "500 samples", "2500 samples", "10000 samples"],
                    label="Package",
                    value="100 samples"
                )
                language_filter = gr.Dropdown(
                    ["all", "en", "de", "fr", "tr"],
                    label="Language Filter",
                    value="all"
                )
            
            customer_email = gr.Textbox(label="Client Email", placeholder="client@example.com")
            
            with gr.Row():
                start_btn = gr.Button("🚀 Generate Dataset", variant="primary", size="lg")
            
            status = gr.Textbox(label="Status", interactive=False)
            link_output = gr.Textbox(label="Download Link (1 hour)", interactive=False)
            
            gr.Markdown("### 💡 Tips")
            gr.Markdown("""
            - Use specific keywords for better results (e.g., "California privacy law contracts")
            - Combine multiple keywords with spaces
            - Language filter helps narrow results
            - Larger packages may take longer to generate
            """)
        
        with gr.TabItem("📥 Client Download"):
            gr.Markdown("### ⚠️ Terms of Use")
            gr.Markdown("""
            By downloading this dataset, you agree that:
            - It will be used **only for AI training purposes**
            - Resale, redistribution, or illegal use is strictly prohibited
            - The dataset may contain [MASKED] fields for compliance
            - You assume all legal responsibility for its use
            """)
            
            consent = gr.Checkbox(label="I accept the terms and conditions.")
            file_id_input = gr.Textbox(label="File ID (from download link)")
            format_choice = gr.Radio(["CSV", "JSONL"], label="Format", value="CSV")
            download_btn = gr.Button("⬇️ Download Dataset", size="lg")
            file_output = gr.File(label="Dataset File")
            
            download_btn.click(
                download_file,
                inputs=[file_id_input, consent, format_choice],
                outputs=[file_output]
            )
        
        with gr.TabItem("📈 Statistics"):
            stats_btn = gr.Button("🔄 Refresh Statistics")
            stats_output = gr.JSON(label="System Statistics")
            stats_btn.click(get_statistics, outputs=stats_output)
        
        with gr.TabItem("⚙️ Settings"):
            gr.Markdown("### API Keys")
            bing_key = gr.Textbox(label="Bing API Key", type="password")
            github_token = gr.Textbox(label="GitHub Token (for API access)", type="password")
            
            save_btn = gr.Button("💾 Save Settings")
            def save_settings(bing: str, gh: str):
                if bing:
                    os.environ["BING_API_KEY"] = bing
                if gh:
                    os.environ["GITHUB_TOKEN"] = gh
                return "✅ Settings saved successfully."
            
            save_btn.click(save_settings, inputs=[bing_key, github_token], outputs=[gr.Textbox(label="Result")])
    
    # Ana iş akışı
    start_btn.click(
        generate_dataset,
        inputs=[query_input, dataset_type, package, customer_email, language_filter],
        outputs=[status, link_output]
    )
    
    gr.Markdown("---")
    gr.Markdown("""
    🔒 **GDPR/KVKK Compliance:** All personal data is automatically masked. robots.txt is respected.
    Only publicly available datasets with permissive licenses are included. Files are deleted 1 hour after generation.
    
    📧 **Support:** For custom datasets, contact us at support@silentoperator.com
    """, elem_classes="disclaimer")

if __name__ == "__main__":
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 10000)),
        theme=gr.themes.Soft()
    ) )
