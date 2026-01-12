import feedparser
import requests
import os
import time
import json

# 1. 从 GitHub Secrets 读取配置
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 2. 定义 RSS 源
RSS_FEEDS = [
    "https://moxie.foxnews.com/google-publisher/tech.xml", 
    "https://hnrss.org/newest?points=100", 
]

def clean_content_jina(url):
    """使用 Jina Reader 将网页转为 Markdown"""
    jina_url = f"https://r.jina.ai/{url}"
    try:
        response = requests.get(jina_url, timeout=20) # 延长超时时间
        if response.status_code == 200:
            print(f"✅ Jina 抓取成功 (长度: {len(response.text)})")
            return response.text
    except Exception as e:
        print(f"❌ Jina 请求错误: {e}")
    return None

def analyze_via_rest_api(text, title):
    """【核心修改】直接使用 REST API 调用 Gemini 1.5 Flash"""
    if len(text) < 200: return "SKIP"

    # API 端点 (直接写死，最稳妥)
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    headers = {'Content-Type': 'application/json'}
    
    prompt = f"""
    你是一位半导体行业的情报官。请阅读新闻：
    标题：{title}
    内容：
    {text[:8000]} 

    任务：
    1. 假如内容是关于具体市场数据、芯片技术参数、重大并购或人事变动的，请用中文总结（100字以内）。
    2. 假如内容是泛泛而谈的观点、教程或无关内容，直接回复 "SKIP"。
    3. 总结格式：
       - 核心事实：...
       - 关键数据：...
    """

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            # 解析嵌套的 JSON 结构
            try:
                answer = result['candidates'][0]['content']['parts'][0]['text']
                return answer.strip()
            except (KeyError, IndexError):
                print(f"⚠️ API 返回结构异常: {result}")
                return "SKIP"
        else:
            print(f"❌ Gemini API 报错 ({response.status_code}): {response.text}")
            return "SKIP"
            
    except Exception as e:
        print(f"❌ 网络请求失败: {e}")
        return "SKIP"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

def main():
    print("🚀 任务开始 (REST API 版)...")
    
    for feed_url in RSS_FEEDS:
        print(f"📡 正在检查源: {feed_url}")
        feed = feedparser.parse(feed_url)
        
        for entry in feed.entries[:2]:
            print(f"📄 处理文章: {entry.title}")
            
            content = clean_content_jina(entry.link)
            if not content: continue
            
            # 这里调用新的 REST API 函数
            analysis = analyze_via_rest_api(content, entry.title)
            
            if analysis and "SKIP" not in analysis:
                print("💡 发现有价值新闻，正在发送...")
                # 这里的格式调整得更易读
                message = f"🚨 *{entry.title}*\n\n{analysis}\n\n🔗 [原文链接]({entry.link})"
                send_telegram(message)
                time.sleep(2) 
            else:
                print("🗑️ 内容被跳过")

if __name__ == "__main__":
    main()
