import os
import requests
import datetime
import time
import akshare as ak
import pandas as pd
import glob

# ================= 核心1：Python 获取真实底层数据（新浪接口防封杀） =================
def get_surge_stocks():
    print("📈 正在潜入【新浪/网易】接口抓取A股真实数据...")
    
    pool_str = "300308,300502,300394,002463,300476,601138,688012,002371,688072,600584,002156,688041,688256,688498,688630,300567,300456,603283,603893,000066,000034,002409,300666,603650,688268,688300,300054,600330,000962,002130,688234,605589,600183,003031,301377,688378,603773,300776,688716,603663,300905,688386,300174,688333,600363,688027,600580,688639,688065,001270,300045,002273,688496,600552,688150,301393,688076,000963,002422,300298,300430,300487,002385,301162,000821,688700,688102,600549,300339,300207,300285,688116,300133,603662,002353,600066,601058,300866,688169,688036,601689,002126,603298,603338,000157,300833,600933,603997,600309,002601,300396,603259,300529,002372,300415,603179,002028,603556,603129,002444,603596,603197,601100,002472,688187,600900,600938,601899,601225,601288,600941,600285,000423,600660,300821,000922,000629"
    my_pool_list =[code.strip() for code in pool_str.split(",")]
    
    df = None 
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot() # 优先新浪
            if df is not None and not df.empty:
                print("✅ 成功连接新浪财经接口！")
                break
        except Exception:
            try:
                df = ak.stock_zh_a_spot_netease() # 备用网易
                if df is not None and not df.empty:
                    print("✅ 成功连接网易财经接口！")
                    break
            except Exception:
                time.sleep(2)
            
    if df is None or df.empty:
        print("❌ 网络彻底拥堵，抓取失败")
        return None
        
    try:
        code_col =[col for col in df.columns if '代码' in col or 'symbol' in col.lower()][0]
        name_col =[col for col in df.columns if '名称' in col or 'name' in col.lower()][0]
        change_col =[col for col in df.columns if '涨跌幅' in col or 'percent' in col.lower() or '涨跌' in col][0]

        df['纯数字代码'] = df[code_col].astype(str).str.extract(r'(\d{6})')
        my_df = df[df['纯数字代码'].isin(my_pool_list)].copy()
        
        if my_df.empty:
            return None

        my_df[change_col] = pd.to_numeric(my_df[change_col], errors='coerce')
        
        # ⚠️ 注意这里！为了让你今天周六能测试出文章，我特意改成了 > -10.0！
        # 等你看到网站上更新了真实数据，你再改回 > 3.0 ！
        surge_df = my_df[my_df[change_col] > -10.0] 
        
        if surge_df.empty:
            return None
            
        stock_data_list =[]
        for index, row in surge_df.iterrows():
            change_val = round(float(row[change_col]), 2)
            stock_data_list.append({
                "name": row[name_col],
                "code": row['纯数字代码'],
                "change": change_val
            })
            
        return stock_data_list[:5] 
        
    except Exception as e:
        print(f"❌ 数据清洗报错：{str(e)}")
        return None

# ================= 核心2：把 AI 当成没有感情的打字员 =================
def ask_deepseek_single(stock_name):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    
    system_prompt = """
    你是一位严谨的A股量化研究员。
    请直接用两段话分析这只股票（绝对不准写标题，绝对不准写任何涨跌幅数字）：
    第一段：【🏰 核心产业壁垒】：(主营业务、护城河)
    第二段：【🔥 近期资金逻辑】：(受益于什么概念或产业链)
    大白话，客观分析，拒绝任何吹捧。
    """
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = {
        "model": "deepseek-chat",
        "messages":[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"请分析：{stock_name}"}],
        "temperature": 0.2
    }
    
    for i in range(3):
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            return response.json()['choices'][0]['message']['content'].strip()
        except Exception:
            time.sleep(2)
    return "❌ AI分析生成失败。"

# ================= 核心3：Python 暴力删除旧文并强制排版 =================
def write_blog_post(stock_data_list):
    today_date = datetime.datetime.now().strftime('%Y-%m-%d')
    post_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    
    folder_path = "content/post"
    os.makedirs(folder_path, exist_ok=True)
    
    # 🌟 核心杀招：删掉以前所有发过的报告，保证你的网站首页只有最新的一篇，绝不看错！
    for old_file in glob.glob(os.path.join(folder_path, "report-*.md")):
        os.remove(old_file)

    md_content = f"""---
title: "🚀 【深度复盘】核心资产大涨逻辑拆解 ({today_date})"
date: {post_time}
categories:
    - 量化研报
tags:
    - AI选股
draft: false
---

# 今日异动全景扫描
此报告由 **Python 获取底层真实数据 + DeepSeek 深度逻辑分析** 组合生成。数据绝对真实，拒绝 AI 幻觉！

---

"""
    for stock in stock_data_list:
        print(f"🤖 正在呼叫 AI 单独分析：{stock['name']} ...")
        # ⚠️ 认准这个带 🏷️ 符号的标题！这才是真代码生成的！
        md_content += f"## 🏷️ 【{stock['name']}】({stock['code']}) 真实涨幅：**{stock['change']}%**\n\n"
        ai_analysis = ask_deepseek_single(stock['name'])
        md_content += ai_analysis + "\n\n---\n\n"
        
    md_content += f"*本文由自动化程序于北京时间 {today_date} 自动发布。*"
    
    file_path = f"{folder_path}/report-{today_date}.md"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✅ 博客文章已成功生成：{file_path}")

if __name__ == "__main__":
    stock_data_list = get_surge_stocks()
    if stock_data_list:
        write_blog_post(stock_data_list)
    else:
        print("今日无符合条件的股票，停更。")
