import os
import requests
import datetime
import time
import akshare as ak
import pandas as pd

def get_surge_stocks():
    print("📈 正在抓取A股大涨股票...")
    
    # 🌟 你的 110 只核心武器库（全量装载）
    pool_1 = "300308,300502,300394,002463,300476,601138,688012,002371,688072,600584,002156,688041,688256,688498,688630,300567,300456,603283,603893,000066,000034"
    pool_2 = "002409,300666,603650,688268,688300,300054,600330,000962,002130,688234,605589,600183,003031,301377,688378"
    pool_3 = "603773,300776,688716,603663,300905,688386,300174,688333,600363,688027,600580,688639,688065,001270,300045,002273,688496,600552,688150"
    pool_4 = "301393,688076,000963,002422,300298,300430,300487,002385,301162,000821,688700,688102,600549,300339,300207,300285,688116,300133,603662"
    pool_5 = "002353,600066,601058,300866,688169,688036,601689,002126,603298,603338,000157,300833,600933,603997,600309,002601,300396,603259,300529,002372,300415,603179,002028,603556,603129,002444,603596,603197,601100,002472,688187"
    pool_6 = "600900,600938,601899,601225,601288,600941,600285,000423,600660,300821,000922,000629"
    
    all_codes_str = f"{pool_1},{pool_2},{pool_3},{pool_4},{pool_5},{pool_6}"
    my_pool_list = [code.strip() for code in all_codes_str.split(",")]
    
    # 🌟 修复 Bug 的关键：预先给一个空箱子，并且加上“空箱子提前下班”的指令
    df = None 
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            break
        except Exception:
            print(f"⚠️ 第 {attempt+1} 次获取A股数据失败，休息2秒重试...")
            time.sleep(2)
            
    # 如果3次都失败了，就直接下班，绝不往下走！
    if df is None or df.empty:
        print("❌ 网络拥堵，3次获取A股数据均失败。取消本次发文。")
        return None
        
    df['代码'] = df['代码'].astype(str).str.zfill(6)
    my_df = df[df['代码'].isin(my_pool_list)]
    surge_df = my_df[my_df['涨跌幅'] > 3.0] # 涨幅>3%的股票
    
    if surge_df.empty:
        return None
        
    stock_list =[]
    for index, row in surge_df.iterrows():
        stock_list.append(f"【{row['名称']}】 (代码: {row['代码']}) 今日涨幅：{row['涨跌幅']}%")
    return stock_list

def ask_deepseek(stock_list):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return "❌ 没找到 DeepSeek 密码！"

    stocks_str = "\n".join(stock_list)
    system_prompt = """你是一位顶级的A股百亿私募量化研究总监。
    请针对以下今日大涨的股票，用通俗易懂的大白话进行深度复盘：
    1. 核心壁垒是什么？2. 暴涨逻辑是什么？
    排版必须极其精美，适合作为博客文章发布，多用Emoji。"""
    
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

# ================= 核心魔法：将AI内容写成博客文章 =================
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
此报告由 **GitHub Actions + DeepSeek** 全自动生成，抓取全市场核心标的进行深度分析。

---

{ai_content}

---
*本文由自动化程序于北京时间 {today_date} 自动发布。*
"""
    
    folder_path = "content/post"
    os.makedirs(folder_path, exist_ok=True)
    file_path = f"{folder_path}/report-{today_date}.md"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✅ 博客文章已成功生成并保存在：{file_path}")

if __name__ == "__main__":
    stock_list = get_surge_stocks()
    if stock_list:
        ai_content = ask_deepseek(stock_list)
        if "❌" not in ai_content:
            write_blog_post(ai_content)
        else:
            print(ai_content)
    else:
        print("今日无大涨股票，或网络抓取失败，今日停更不发文。")
