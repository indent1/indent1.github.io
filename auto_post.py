import os
import requests
import datetime
import time
import akshare as ak
import pandas as pd

def get_surge_stocks():
    print("📈 正在抓取A股大涨股票...")
    # 这里放你精选的核心股票代码（测试时为了保证有结果，可以用几只最近活跃的）
    my_pool_str = "002463,300476,300394,688498,300666,000962,603773"
    my_pool_list =[code.strip() for code in my_pool_str.split(",")]
    
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            break
        except Exception:
            time.sleep(2)
            
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
    
    response = requests.post(url, headers=headers, json=data, timeout=60)
    return response.json()['choices'][0]['message']['content'].strip()

# ================= 核心魔法：将AI内容写成博客文章 =================
def write_blog_post(ai_content):
    # 获取今天的时间作为标题和文件名
    today_date = datetime.datetime.now().strftime('%Y-%m-%d')
    post_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    
    # 构造 Markdown 博客文章的头部格式（Front Matter）
    # 这里的 categories 和 tags 会在你的博客侧边栏自动生成分类夹！
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
    
    # 将文章保存在 Hugo Stack 主题的指定文件夹里
    # 文件名例如：content/post/report-2026-05-09.md
    folder_path = "content/post"
    os.makedirs(folder_path, exist_ok=True)
    file_path = f"{folder_path}/report-{today_date}.md"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✅ 博客文章已成功生成：{file_path}")

if __name__ == "__main__":
    stock_list = get_surge_stocks()
    if stock_list:
        ai_content = ask_deepseek(stock_list)
        write_blog_post(ai_content)
    else:
        print("今日无大涨股票，不发文。")
