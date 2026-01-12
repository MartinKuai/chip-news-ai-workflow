import feedparser
import requests
import os
import google.generativeai as genai
import time

# 1. 从 GitHub Secrets 读取配置
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# 2. 配置 Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-pro')

# 3. 定义你的“一手信息源” (可随时修改)
RSS_FEEDS = [
    # 路透社科技版 (硬新闻)
    "https://moxie.foxnews.com/google-publisher/tech.xml", # 替代源，部分路透源需特定Header
    # Hacker News 热门 (技术趋势)
    "https://hnrss.org/newest?points=100", 
]

def clean_content_jina(url):
    """使用 Jina Reader 将网页转为干净的 Markdown"""
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "Authorization": f"Bearer {os.environ.get('JINA_API_KEY', '')}" # 免费版不需要Key，但预留接口
    }
    try:
        # 设置 User-Agent 防止被某些网站拦截
        response = requests.get(jina_url, timeout=15)
        if response.status_code == 200:
            print(f"Jina 抓取成功，长度: {len(response.text)}")
            return response.text
    except Exception as e:
        print(f"Jina 清洗失败: {e}")
    return None

def analyze_article(text, title):
    """让 Gemini 充当分析师"""
    if len(text) < 200: return "SKIP" # 内容太短直接跳过

    prompt = f"""
    你是一位半导体与科技行业的资深销售工程师。请审阅以下新闻。
    
    标题：{title}
    内容：
    {text[:8000]} 

    任务：
    1. 判断价值：这是否包含具体的**市场数据、技术参数、人事变动或供应链动态**？如果是纯观点/废话，输出 "SKIP"。
    2. 如果有价值，请用**中文**生成简报（不超过 100 字）：
       - 用【】标注核心实体（如【台积电】、【3nm工艺】）。
       - 提炼关键数据。
       - 语气客观专业。
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"Gemini 分析失败: {e}")
        return "SKIP"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown" # 支持粗体等格式
    }
    requests.post(url, json=payload)

def main():
    print("开始执行任务...")
    # 发送一条开始消息 (调试用，稳定后可注释掉)
    # send_telegram("🤖 每日情报抓取任务开始...")
    
    for feed_url in RSS_FEEDS:
        print(f"正在抓取: {feed_url}")
        feed = feedparser.parse(feed_url)
        
        # 每个源只取前 2 条最新的，避免消息轰炸
        for entry in feed.entries[:2]:
            print(f"处理文章: {entry.title}")
            
            # 1. 清洗网页
            content = clean_content_jina(entry.link)
            if not content: continue
            
            # 2. AI 分析
            analysis = analyze_article(content, entry.title)
            
            # 3. 发送结果
            if analysis != "SKIP":
                message = f"📰 *{entry.title}*\n\n{analysis}\n\n🔗 [原文链接]({entry.link})"
                send_telegram(message)
                time.sleep(2) # 避免发送太快被 Telegram 限制
            else:
                print("内容被 AI 判定为无价值，跳过。")

if __name__ == "__main__":

    main()

