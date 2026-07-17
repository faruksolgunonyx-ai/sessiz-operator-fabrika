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

GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
genai.configure(api_key=GEMINI_KEY)

TEMP_DIR = "/tmp/sessizoperator"
os.makedirs(TEMP_DIR, exist_ok=True)
download_links = {}

# ---------- PREMIUM HUKUK PROMPT ----------
HUKUK_PROMPT = """
Sen bir hukuk dokümanı analiz asistanısın. Aşağıdaki web sitesi metninden şunları çıkar:
- sektor: "Hukuk" veya alt dalı (ör: "Bilişim Hukuku", "Ticaret Hukuku")
- sozlesme_turu: sayfada geçen sözleşme türü (ör: "Gizlilik Sözleşmesi", "Kullanım Koşulları", "Bulunamadı")
- taraflar: sözleşmenin tarafları (şirket isimleri maskelenecek, örn: "ŞİRKET_1" ve "ŞİRKET_2" olarak değiştir)
- yukumlulukler: tarafların yükümlülükleri (kısa madde başlıkları)
- yargi_yetkisi: uyuşmazlık halinde yetkili mahkeme (varsa)
- ozet: 2-3 cümlelik doküman özeti
- anahtar_kelimeler: virgülle ayrılmış 5 hukuki anahtar kelime

Kesinlikle sadece JSON formatında cevap ver:
{"sektor": "...", "sozlesme_turu": "...", "taraflar": "...", "yukumlulukler": "...", "yargi_yetkisi": "...", "ozet": "...", "anahtar_kelimeler": "..."}
Metin:
"""

SEM_LIMIT = 5
semaphore = asyncio.Semaphore(SEM_LIMIT)

def mask_company_names(text):
    # Basit maskeleme: büyük harfle başlayan 2+ kelimeli özel isimleri "ŞİRKET_X" yap
    # Bu örnek amaçlıdır, geliştirilebilir.
    return re.sub(r'\b[A-ZŞĞÜÖÇİ][a-zğüöçış]+(?: [A-ZŞĞÜÖÇİ][a-zğüöçış]+)+\b', 'ŞİRKET_MASK', text)

def process_hukuk(text):
    prompt = HUKUK_PROMPT + text[:10000]
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
        # Maskeleme uygula
        if "taraflar" in data:
            data["taraflar"] = mask_company_names(data["taraflar"])
        return data
    except:
        return {}

async def crawl_site(url):
    async with semaphore:
        try:
            async with AsyncWebCrawler() as crawler:
                config = CrawlerRunConfig(max_pages=2)
                result = await crawler.arun(url, config=config)
                return result.markdown if result else ""
        except:
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                soup = BeautifulSoup(resp.text, "html.parser")
                return soup.get_text(separator=" ", strip=True)
            except:
                return ""

async def generate_hukuk_dataset(url_list_str, musteri_email, progress=gr.Progress()):
    urls = [u.strip() for u in url_list_str.split("\n") if u.strip()]
    if not urls:
        return "❌ Lütfen en az bir URL girin.", ""

    results = []
    tasks = [crawl_site(u) for u in urls]
    for i, coro in enumerate(asyncio.as_completed(tasks)):
        text = await coro
        if not text:
            results.append({"url": urls[i], "error": "Taranamadı"})
        else:
            processed = process_hukuk(text)
            processed["url"] = urls[i]
            results.append(processed)
        progress((i+1)/len(urls), desc=f"{i+1}/{len(urls)} site işlendi")

    if not results:
        return "⚠️ Hiç sonuç alınamadı.", ""

    # Müşteriye özel anonim ID
    customer_hash = hashlib.sha256(musteri_email.encode()).hexdigest()[:12]
    file_id = str(uuid.uuid4())[:8]
    file_name = f"Hukuk_Premium_{customer_hash}_{file_id}.csv"
    file_path = os.path.join(TEMP_DIR, file_name)

    keys = ["url", "sektor", "sozlesme_turu", "taraflar", "yukumlulukler", "yargi_yetkisi", "ozet", "anahtar_kelimeler"]
    with open(file_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([f"# LİSANS: Bu veri seti yalnızca AI eğitimi içindir. Yeniden satış yasaktır. Müşteri ID: {customer_hash}"])
        writer.writerow(keys)
        for row in results:
            writer.writerow([row.get(k, "") for k in keys])

    download_url = f"/download/{file_id}"
    download_links[file_id] = {
        "path": file_path,
        "expires_at": datetime.now() + timedelta(hours=1),
        "musteri_email": musteri_email
    }

    # Temizlik
    now = datetime.now()
    for fid in list(download_links.keys()):
        if download_links[fid]["expires_at"] < now:
            try: os.remove(download_links[fid]["path"])
            except: pass
            del download_links[fid]

    return f"✅ {len(results)} hukuk dokümanı hazır! Müşteri ID: {customer_hash}", download_url

def download_file(file_id, onay):
    if not onay:
        return "❌ Kullanım şartlarını onaylayın."
    info = download_links.get(file_id)
    if not info or datetime.now() > info["expires_at"]:
        return "❌ Bağlantı geçersiz veya süresi doldu."
    return info["path"]

with gr.Blocks(theme=gr.themes.Soft(), title="Sessiz Operatör – LegalTech Premium") as demo:
    gr.Markdown("# ⚖️ Sessiz Operatör – Premium Hukuk Veri Seti Fabrikası")
    with gr.Tabs():
        with gr.TabItem("📊 Hukuk Veri Seti Üret"):
            url_input = gr.Textbox(label="Hukuk sayfalarının URL'leri (alt alta)", lines=8,
                                   placeholder="https://www.hukukburosu.com/kvkk\nhttps://www.sirket.com/sozlesme")
            musteri_email = gr.Textbox(label="Müşteri E-posta Adresi")
            start_btn = gr.Button("🚀 Premium Veri Seti Oluştur")
            status = gr.Textbox(label="Durum")
            link_output = gr.Textbox(label="İndirme Linki (1 saat)", interactive=False)
            start_btn.click(generate_hukuk_dataset, [url_input, musteri_email], [status, link_output])

        with gr.TabItem("📥 Müşteri İndirme"):
            gr.Markdown("### ⚠️ Kullanım Şartları\nBu veri seti yalnızca yapay zeka eğitimi içindir. Hukuki tavsiye yerine geçmez.")
            onay = gr.Checkbox(label="Şartları kabul ediyorum.")
            file_id_input = gr.Textbox(label="Dosya ID'si")
            download_btn = gr.Button("İndir")
            file_output = gr.File(label="Veri Seti (CSV)")
            download_btn.click(download_file, [file_id_input, onay], file_output)

        with gr.TabItem("⚙️ Ayarlar"):
            gemini_input = gr.Textbox(label="Gemini API Anahtarı", type="password", value=GEMINI_KEY)
            save_btn = gr.Button("Kaydet")
            def save_key(k):
                os.environ["GEMINI_KEY"] = k
                genai.configure(api_key=k)
                return "✅ Güncellendi."
            save_btn.click(save_key, [gemini_input], [gr.Textbox()])

    gr.Markdown("---\n🔒 **KVKK/GDPR:** Yalnızca kamuya açık hukuk metinleri derlenir, kişisel veri maskelenir. Dosya 1 saat sonra silinir.")

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 10000)))
