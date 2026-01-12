import feedparser
import requests
import os
import time
import json

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RSS_FEEDS = [
    "https://moxie.foxnews.com/google-publisher/tech.xml", 
    "https://hnrss.org/newest?points=100", 
]

# --- 核心修改：先列出可用模型，不再盲猜 ---
def check_available_models():
    print("🔍 正在查询可用模型列表...")
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            models = response.json().get('models', [])
            print("✅ 你的 API Key 支持以下模型：")
            valid_names = [m['name'].replace('models/', '') for m in models if 'generateContent' in m['supportedGenerationMethods']]
            print(valid_names)
            return valid_names
        else:
            print(f"❌ 无法获取模型列表: {response.text}")
            return []
    except Exception as e:
        print(f"❌ 网络检查失败: {e}")
        return []

def clean_content_jina(url):
    jina_url = f"https://r.jina.ai/{url}"
    try:
        response = requests.get(jina_url, timeout=20)
        if response.status_code == 200:
            print(f"✅ Jina 抓取成功 (长度: {len(response.text)})")
            return response.text
    except Exception as e:
        print(f"❌ Jina 请求错误: {e}")
    return None

def analyze_via_rest_api(text, title, model_name="gemini-1.5-flash-001"):
    if len(text) < 200: return "SKIP"

    # 使用传入的 model_name
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    prompt = f"""
    你是一位半导体行业的情报官。请阅读新闻：
    标题：{title}
    内容：
    {text[:8000]} 

    任务：
    1. 假如内容是关于具体市场数据、芯片技术参数、重大并购或人事变动的，请用中文总结（100字以内）。
    2. 假如内容是泛泛而谈的观点、教程或无关内容，直接回复 "SKIP"。
    """

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            try:
                return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            except:
                return "SKIP"
        else:
            # 打印详细错误，方便调试
            print(f"❌ Gemini API 报错: {response.status_code} - {response.text}")
            return "SKIP"
    except Exception as e:
        print(f"❌ 请求失败: {e}")
        return "SKIP"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def main():
    print("🚀 任务开始 (诊断模式)...")
    
    # 1. 先自检，看看到底有什么模型可用
    available_models = check_available_models()
    
    # 2. 智能选择模型：优先用 flash-001，如果没有，就用列表里的第一个
    target_model = "gemini-1.5-flash-001"
    if available_models:
        if "gemini-1.5-flash-001" in available_models:
            target_model = "gemini-1.5-flash-001"
        elif "gemini-1.5-flash" in available_models:
            target_model = "gemini-1.5-flash"
        elif "gemini-pro" in available_models:
            target_model = "gemini-pro"
        else:
            # 如果常用名都没有，就取第一个能用的
            target_model = available_models[0]
            
    print(f"🎯 最终决定使用的模型是: {target_model}")

    # 3. 开始跑任务
    for feed_url in RSS_FEEDS:
        print(f"📡 检查源: {feed_url}")
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:2]:
            print(f"📄 文章: {entry.title}")
            content = clean_content_jina(entry.link)
            if not content: continue
            
            analysis = analyze_via_rest_api(content, entry.title, model_name=target_model)
            
            if analysis and "SKIP" not in analysis:
                print("💡 发送消息...")
                msg = f"🚨 *{entry.title}*\n\n{analysis}\n\n🔗 [原文链接]({entry.link})"
                send_telegram(msg)
                time.sleep(2)
            else:
                print("🗑️ 跳过")

if __name__ == "__main__":
    main()
