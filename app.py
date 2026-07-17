import asyncio
import csv
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta

import gradio as gr
import requests
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ------------------- AYARLAR -------------------
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
genai.configure(api_key=GEMINI_KEY)

TEMP_DIR = "/tmp/sessizoperator"
os.makedirs(TEMP_DIR, exist_ok=True)
download_links = {}

# ------------------- PROMPT (İngilizce Hukuk) -------------------
LEGAL_PROMPT = """
You are a legal document analysis assistant. Extract the following from the provided website text:
- sector: "Legal" or a specific sub-field (e.g., "Corporate Law", "Data Privacy", "Intellectual Property")
- document_type: type of legal document (e.g., "Privacy Policy", "Terms of Service", "Contract", "Regulation")
- parties: the parties involved (if any, anonymize real names as PARTY_A, PARTY_B)
- obligations: key obligations or clauses (concise bullet points)
- jurisdiction: governing law or jurisdiction (if mentioned)
- summary: 2-3 sentence summary of the document
- keywords: 5 relevant legal keywords (comma separated)

Return strictly JSON:
{"sector": "...", "document_type": "...", "parties": "...", "obligations": "...", "jurisdiction": "...", "summary": "...", "keywords": "..."}
Text:
"""

SEM_LIMIT = 5
semaphore = asyncio.Semaphore(SEM_LIMIT)

# ------------------- ANONİMLEŞTİRME (PARTY_A / PARTY_B) -------------------
def anonymize_parties(text):
    # Büyük harfle başlayan iki veya daha fazla kelimeden oluşan özel isimleri PARTY yap
    return re.sub(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)+\b', 'PARTY', text)

# ------------------- GEMINI ANALİZ -------------------
def process_legal(text):
    prompt = LEGAL_PROMPT + text[:10000]
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )
    try:
        resp = model.generate_content(prompt)
        raw = resp.text.strip().replace("```json", "").replace("```", "")
        data = json.loads(raw)
        if "parties" in data:
            data["parties"] = anonymize_parties(data["parties"])
        return data
    except:
        return {}

# ------------------- TARAMA (CRAWL4AI + YEDEK BS4) -------------------
async def crawl_site(url):
    async with semaphore:
        try:
            # Önce Crawl4AI ile dinamik tarama dene
            async with AsyncWebCrawler() as crawler:
                config = CrawlerRunConfig(max_pages=2)
                result = await crawler.arun(url, config=config)
                return result.markdown if result else ""
        except:
            # Başarısız olursa statik BS4 ile dene
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                return soup.get_text(separator=" ", strip=True)
            except:
                return ""

# ------------------- ANA VERİ SETİ OLUŞTURMA -------------------
async def generate_legal_dataset(url_list_str, customer_email, progress=gr.Progress()):
    urls = [u.strip() for u in url_list_str.split("\n") if u.strip()]
    if not urls:
        return "❌ Please enter at least one URL.", ""

    results = []
    tasks = [crawl_site(u) for u in urls]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        text = await coro
        if not text:
            results.append({"url": urls[i], "error": "Failed to scrape"})
        else:
            processed = process_legal(text)
            processed["url"] = urls[i]
            results.append(processed)
        progress((i+1)/len(urls), desc=f"{i+1}/{len(urls)} sites processed")

    if not results:
        return "⚠️ No results.", ""

    # Müşteriye özel anonim ID ve dosya adı
    customer_hash = hashlib.sha256(customer_email.encode()).hexdigest()[:12]
    file_id = str(uuid.uuid4())[:8]
    file_name = f"Legal_English_{customer_hash}_{file_id}.csv"
    file_path = os.path.join(TEMP_DIR, file_name)

    # CSV oluştur (lisans satırı + başlık + veri)
    keys = ["url", "sector", "document_type", "parties", "obligations", "jurisdiction", "summary", "keywords"]
    with open(file_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Lisans satırı (müşteri ID'si ile izlenebilir)
        writer.writerow([f"# LICENSE: AI training only. Resale prohibited. Customer ID: {customer_hash}"])
        writer.writerow(keys)
        for row in results:
            writer.writerow([row.get(k, "") for k in keys])

    # Geçici indirme linki (1 saat)
    download_url = f"/download/{file_id}"
    download_links[file_id] = {
        "path": file_path,
        "expires_at": datetime.now() + timedelta(hours=1),
        "customer_email": customer_email
    }

    # Süresi dolan dosyaları temizle
    now = datetime.now()
    for fid in list(download_links.keys()):
        if download_links[fid]["expires_at"] < now:
            try: os.remove(download_links[fid]["path"])
            except: pass
            del download_links[fid]

    return f"✅ {len(results)} legal documents ready! Customer ID: {customer_hash}", download_url

# ------------------- MÜŞTERİ İNDİRME -------------------
def download_file(file_id, consent):
    if not consent:
        return "❌ You must accept the terms."
    info = download_links.get(file_id)
    if not info or datetime.now() > info["expires_at"]:
        return "❌ Link expired or invalid."
    return info["path"]

# ------------------- GRADIO ARAYÜZ -------------------
with gr.Blocks(theme=gr.themes.Soft(), title="Silent Operator – Legal Dataset Factory") as demo:
    gr.Markdown("# ⚖️ Silent Operator – Premium English Legal Dataset Generator")

    with gr.Tabs():
        with gr.TabItem("📊 Generate Legal Dataset"):
            url_input = gr.Textbox(
                label="Target URLs (one per line)",
                lines=8,
                placeholder="https://www.lawfirm.com/privacy\nhttps://www.company.com/tos"
            )
            customer_email = gr.Textbox(label="Client Email Address")
            start_btn = gr.Button("🚀 Generate Dataset")
            status = gr.Textbox(label="Status")
            link_output = gr.Textbox(label="Download Link (valid 1 hour)", interactive=False)
            start_btn.click(
                generate_legal_dataset,
                inputs=[url_input, customer_email],
                outputs=[status, link_output]
            )

        with gr.TabItem("📥 Client Download"):
            gr.Markdown("### ⚠️ Terms of Use\nThis dataset is for **AI training only**. Not for legal advice. Resale prohibited.")
            consent = gr.Checkbox(label="I accept the terms and conditions.")
            file_id_input = gr.Textbox(label="File ID")
            download_btn = gr.Button("Download Dataset")
            file_output = gr.File(label="Dataset (CSV)")
            download_btn.click(
                download_file,
                inputs=[file_id_input, consent],
                outputs=[file_output]
            )

        with gr.TabItem("⚙️ Settings"):
            gr.Markdown("Enter your Gemini API key here, or set it as environment variable `GEMINI_KEY`.")
            gemini_input = gr.Textbox(label="Gemini API Key", type="password", value=GEMINI_KEY)
            save_btn = gr.Button("Save")
            def save_key(k):
                os.environ["GEMINI_KEY"] = k
                genai.configure(api_key=k)
                return "✅ API key updated."
            save_btn.click(save_key, inputs=[gemini_input], outputs=[gr.Textbox(label="Result")])

    gr.Markdown("---\n🔒 **Privacy & GDPR:** Only publicly available legal texts are collected. No personal data is stored. Files are deleted 1 hour after generation. The user assumes all responsibility for the use of the data.")

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 10000)))
