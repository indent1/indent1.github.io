import os
import requests
import datetime
import time
import akshare as ak
import pandas as pd

# ================= 核心1：抓取A股大涨股票（新浪/网易双引擎防拦截） =================
def get_surge_stocks():
    print("📈 正在潜入【新浪/网易】接口抓取A股数据 (绕过东财防火墙)...")
    
    pool_1 = "300308,300502,300394,002463,300476,601138,688012,002371,688072,600584,002156,688041,688256,688498,688630,300567,300456,603283,603893,000066,000034"
    pool_2 = "002409,300666,603650,688268,688300,300054,600330,000962,002130,688234,605589,600183,003031,301377,688378"
    pool_3 = "603773,300776,688716,603663,300905,688386,300174,688333,600363,688027,600580,688639,688065,001270,300045,002273,688496,600552,688150"
    pool_4 = "301393,688076,000963,002422,300298,300430,300487,002385,301162,000821,688700,688102,600549,300339,300207,300285,688116,300133,603662"
    pool_5 = "002353,600066,601058,300866,688169,688036,601689,002126,603298,603338,000157,300833,600933,603997,600309,002601,300396,603259,300529,002372,300415,603179,002028,603556,603129,002444,603596,603197,601100,002472,688187"
    pool_6 = "600900,600938,601899,601225,601288,600941,600285,000423,600660,300821,000922,000629"
    
    all_codes_str = f"{pool_1},{pool_2},{pool_3},{pool_4},{pool_5},{pool_6}"
    my_pool_list =[code.strip() for code in all_codes_str.split(",")]
    
    df = None 
    for attempt in range(3):
        try:
            # 🌟 优先使用新浪接口
            df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                print("✅ 成功连接新浪财经接口！")
                break
        except Exception:
            try:
                # 🌟 如果新浪被墙，秒切网易接口作为备胎
                df = ak.stock_zh_a_spot_netease()
                if df is not None and not df.empty:
                    print("✅ 成功连接网易财经接口！")
                    break
            except Exception:
                print(f"⚠️ 第 {attempt+1} 次双引擎均获取失败，休息2秒重试...")
                time.sleep(2)
            
    if df is None or df.empty:
        print("❌ 网络彻底拥堵，新浪/网易均无法连通。取消本次发文。")
        return None
        
    try:
        code_col =[col for col in df.columns if '代码' in col or 'symbol' in col.lower()][0]
        name_col =[col for col in df.columns if '名称' in col or 'name' in col.lower()][0]
        
        # 涨跌幅列名在新浪接口可能叫 'changepercent'，网易叫 'PERCENT'
        change_col =[col for col in df.columns if '涨跌幅' in col or 'percent' in col.lower() or '涨跌' in col][0]

        # 🌟 终极杀招：不管接口返回的股票代码带不带 sh/sz，用正则表达式强行只提取里面的 6 位数字！
        df['纯数字代码'] = df[code_col].astype(str).str.extract(r'(\d{6})')
        
        # 用纯数字代码去匹配我们的 110 只股票池
        my_df = df[df['纯数字代码'].isin(my_pool_list)]
        
        if my_df.empty:
            print("⚠️ 数据清洗后发现股票池为空。可能接口数据格式大变。")
            return None
        
        # 将涨跌幅转换为数字格式进行筛选
        my_df[change_col] = pd.to_numeric(my_df[change_col], errors='coerce')
        
        # ⚠️ 测试时，请把这里的 3.0 临时改成 -10.0
        surge_df = my_df[my_df[change_col] > 5] 
        
        if surge_df.empty:
            return None
            
        stock_list =[]
        for index, row in surge_df.iterrows():
            stock_list.append(f"【{row[name_col]}】 (代码: {row['纯数字代码']}) 涨幅：{row[change_col]}%")
        return stock_list
    except Exception as e:
        print(f"❌ 数据清洗报错：{str(e)}")
        return None

# ================= 核心2：调用 DeepSeek 写研报 =================
def ask_deepseek(stock_list):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "❌ 没找到 DeepSeek 密码！"

    stocks_str = "\n".join(stock_list)
    system_prompt = """你是一位极度严谨的A股百亿私募量化研究总监。
    我会给你一份今日异动的股票名单，里面包含了【它们真实的涨跌幅数据】。
    
    【❗绝对强制指令（防捏造警告）】：
    1. 你必须原封不动地使用我提供给你的“涨跌幅数字”！
    2. 绝对、绝对禁止捏造、修改或夸大涨幅（严禁自己编造20%、涨停等虚假数据,要把原始数据里面的涨幅写进去！）
    3. 如果实际涨幅很小，请客观、冷静地分析其产业逻辑，不准使用“暴涨”、“涨停”、“资金疯狂抢筹”等夸张词汇！违规将被开除！
    
    【你的任务】：
    请针对这些股票，用通俗的大白话进行深度复盘：
    1. 核心产业壁垒是什么？
    2. 近期资金关注的底层逻辑是什么？
    排版必须极其精美，适合作为博客文章发布，多用Emoji，每只股票独立成段。"""
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = {
        "model": "deepseek-chat",
        "messages":[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"请深度复盘：\n{stocks_str}"}],
        "temperature": 0.5
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        return response.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"❌ AI 分析失败：{str(e)}"

# ================= 核心3：自动将AI内容写成博客文章 =================
def write_blog_post(ai_content):
    today_date = datetime.datetime.now().strftime('%Y-%m-%d')
    post_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    
    md_content = f"""---
title: "🚀 【深度复盘】核心资产大涨逻辑拆解 ({today_date})"
date: {post_time}
categories:
    - 量化研报
tags:
    - AI选股
    - 市场复盘
draft: false
---

# 今日异动全景扫描
此报告由 **Python 获取底层真实数据 + DeepSeek 深度逻辑分析** 组合生成。数据绝对真实，拒绝 AI 幻觉！

---

"""
    # 🌟 绝杀：Python 循环拼装！强制将真实涨幅写死在标题上！
    for stock in stock_data_list:
        print(f"🤖 正在呼叫 AI 单独分析：{stock['name']} ...")
        
        # Python 亲自写标题，使用从 akshare 抓来的真实 % 涨幅
        md_content += f"## 🏷️ 【{stock['name']}】({stock['code']}) 真实涨幅：**{stock['change']}%**\n\n"
        
        # 让 AI 仅仅补充下方的文字分析
        ai_analysis = ask_deepseek_single(stock['name'])
        md_content += ai_analysis + "\n\n---\n\n"
        
    md_content += f"*本文由自动化程序于北京时间 {today_date} 自动发布。*"
    
    # 保存成 Markdown 文件
    folder_path = "content/post"
    os.makedirs(folder_path, exist_ok=True)
    file_path = f"{folder_path}/report-{today_date}.md"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✅ 博客文章已成功生成并保存在：{file_path}")

if __name__ == "__main__":
    stock_data_list = get_surge_stocks()
    if stock_data_list:
        build_and_publish_post(stock_data_list)
    else:
        print("今日无大涨股票，停更。")
