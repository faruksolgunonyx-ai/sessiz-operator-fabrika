import asyncio
import csv
import hashlib
import json
import os
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

# Veri seti türleri (öncekiyle aynı)
DATASET_TYPES = {
    "Sektörel Profil Seti": "Bu web sitesindeki şirketin sektörünü ve 1-2 cümlelik açıklamasını çıkar. JSON: {\"sektor\": \"...\", \"ozet\": \"...\"}",
    "Metin Sınıflandırma Seti": "Bu metinden ana sektörü ve 3-5 etiket çıkar. JSON: {\"sektor\": \"...\", \"etiket\": \"...\"}",
    "Duygu Analizi Seti": "Bu metnin genel duygusunu analiz et (pozitif/negatif/nötr). JSON: {\"metin\": \"...\", \"duygu\": \"...\"}",
    "Özetleme Seti": "Bu uzun metni 1-2 cümleyle özetle. JSON: {\"uzun_metin\": \"...\", \"ozet\": \"...\"}",
    "Soru-Cevap Seti": "Bu şirket hakkında 3 soru ve yanıt üret. JSON: {\"soru\": \"...\", \"cevap\": \"...\"}",
    "Varlık Tanıma Seti": "Bu metinden şirket, ürün ve lokasyon adlarını çıkar. JSON: {\"metin\": \"...\", \"varliklar\": \"...\"}",
    "Çok Dilli Set": "Bu metnin İngilizce ve Türkçe özetini çıkar. JSON: {\"metin_tr\": \"...\", \"metin_en\": \"...\"}",
    "Özel Konulu Set": "Bu metinden kullanıcının belirttiği konuyla ilgili bölümleri ayıkla. JSON: {\"konu\": \"...\", \"metin\": \"...\"}"
}

SEM_LIMIT = 5
semaphore = asyncio.Semaphore(SEM_LIMIT)

def process_text(text, dataset_type):
    prompt = DATASET_TYPES[dataset_type] + f"\n\nMetin:\n{text[:10000]}"
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
        return json.loads(raw)
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

async def generate_dataset(url_list_str, dataset_type, musteri_email, progress=gr.Progress()):
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
            processed = process_text(text, dataset_type)
            processed["url"] = urls[i]
            results.append(processed)
        progress((i+1)/len(urls), desc=f"{i+1}/{len(urls)} site işlendi")

    if not results:
        return "⚠️ Hiç sonuç alınamadı.", ""

    # Anonim müşteri kimliği oluştur
    customer_hash = hashlib.sha256(musteri_email.encode()).hexdigest()[:12]
    file_id = str(uuid.uuid4())[:8]
    file_name = f"dataset_{dataset_type.replace(' ', '_')}_{customer_hash}_{file_id}.csv"
    file_path = os.path.join(TEMP_DIR, file_name)

    # CSV yazma
    keys = list(results[0].keys())
    with open(file_path, "w", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Lisans satırı
        writer.writerow([f"# LİSANS: Bu veri seti yalnızca AI eğitimi içindir. Yeniden satış veya yasa dışı kullanım yasaktır. Müşteri ID: {customer_hash}"])
        # Başlık satırı
        writer.writerow(keys)
        # Veri satırları
        for row in results:
            writer.writerow([row.get(k, "") for k in keys])

    download_url = f"/download/{file_id}"
    download_links[file_id] = {
        "path": file_path,
        "expires_at": datetime.now() + timedelta(hours=1),
        "musteri_email": musteri_email
    }

    # Süresi dolanları temizle
    now = datetime.now()
    for fid in list(download_links.keys()):
        if download_links[fid]["expires_at"] < now:
            try:
                os.remove(download_links[fid]["path"])
            except:
                pass
            del download_links[fid]

    return f"✅ {len(results)} örnek hazır! Müşteri ID: {customer_hash}", download_url

def download_file(file_id, onay):
    if not onay:
        return "❌ Lütfen kullanım şartlarını onaylayın."
    info = download_links.get(file_id)
    if not info:
        return "❌ Bağlantı geçersiz."
    if datetime.now() > info["expires_at"]:
        return "❌ Bağlantının süresi doldu (1 saat)."
    return info["path"]

with gr.Blocks(theme=gr.themes.Soft(), title="Sessiz Operatör – Güvenli Teslimat") as demo:
    gr.Markdown("# 🧠 Sessiz Operatör – Müşteriye Özel Veri Seti")

    with gr.Tabs():
        with gr.TabItem("📊 Veri Seti Üret"):
            url_input = gr.Textbox(label="Hedef URL'ler (alt alta)", lines=8)
            dataset_type = gr.Dropdown(choices=list(DATASET_TYPES.keys()), value="Sektörel Profil Seti", label="Veri Seti Türü")
            musteri_email = gr.Textbox(label="Müşteri E-posta Adresi")
            start_btn = gr.Button("🚀 Oluştur")
            status = gr.Textbox(label="Durum")
            link_output = gr.Textbox(label="İndirme Linki (1 saat)", interactive=False)
            start_btn.click(generate_dataset, [url_input, dataset_type, musteri_email], [status, link_output])

        with gr.TabItem("📥 Müşteri İndirme"):
            gr.Markdown("### ⚠️ Kullanım Şartları")
            gr.Markdown("Bu veri seti **sadece yapay zeka modeli eğitimi** içindir. Yeniden satış, dağıtım veya yasa dışı kullanım kesinlikle yasaktır. İndirerek bu şartları kabul etmiş olursunuz.")
            onay = gr.Checkbox(label="Kullanım şartlarını okudum ve kabul ediyorum.")
            file_id_input = gr.Textbox(label="Dosya ID'si")
            download_btn = gr.Button("Dosyayı İndir")
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

    gr.Markdown("---\n🔒 **KVKK/GDPR:** Yalnızca kamuya açık metinler derlenir, kişisel veri işlenmez. Veri seti 1 saat sonra sunucudan silinir.")

if __name__ == "__main__":
    demo.queue().launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 10000)))
