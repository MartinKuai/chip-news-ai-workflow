import feedparser
import requests
import os
import time
import json

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

RSS_FEEDS = [
    # 1. 行业权威
    "https://www.eetimes.com/feed/", 
    
    # 2. 深度工艺与制造
    "https://semiengineering.com/feed/",
    
    # 3. 企业级/服务器硬件 (适合 B2B)
    "https://www.servethehome.com/feed/",
    
    # 4. 市场行情 (TrendForce)
    "https://www.trendforce.com/rss",
    
    # 5. 原有的 Hacker News (可选保留，看技术趋势)
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
    你是一位专业的半导体芯片销售工程师。请阅读这篇来自专业媒体的新闻。
    
    标题：{title}
    内容：
    {text[:8000]} 
    
    【任务判断】
    请判断这篇文章对于“销售工程师”是否有商业或技术价值。
    -如果是以下内容，请直接回复 "SKIP"：
       * 纯粹的消费级数码产品评测（如“iPhone 16 手机壳评测”）。
       * 过于基础的编程教程。
       * 与芯片、半导体供应链、服务器硬件无关的社会新闻。
    
    -如果是以下内容，必须生成总结：
       * 晶圆厂（TSMC, Intel, Samsung）的工艺进展或扩产计划。
       * 关键原厂（NVIDIA, AMD, TI, ADI 等）的新产品发布或财报。
       * 供应链涨价、缺货或库存预警。
       * 具体的 B2B 硬件技术突破（如 CXL, HBM, RISC-V）。

    【总结格式】
    (如果通过筛选，请用中文总结，100字以内)
    🚨 *核心情报*：一句话概括发生了什么。
    📉 *关键数据*：提取文中的金额、制程纳米数、良率或日期。
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

