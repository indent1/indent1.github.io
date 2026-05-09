import os
import requests
import datetime
import time
import akshare as ak
import pandas as pd
import glob

# ================= 核心1：Python 获取真实底层数据 =================
def get_surge_stocks():
    print("📈 正在潜入【新浪/网易】接口抓取A股真实数据...")
    
    pool_str = "300308,300502,300394,002463,300476,601138,688012,002371,688072,600584,002156,688041,688256,688498,688630,300567,300456,603283,603893,000066,000034,002409,300666,603650,688268,688300,300054,600330,000962,002130,688234,605589,600183,003031,301377,688378,603773,300776,688716,603663,300905,688386,300174,688333,600363,688027,600580,688639,688065,001270,300045,002273,688496,600552,688150,301393,688076,000963,002422,300298,300430,300487,002385,301162,000821,688700,688102,600549,300339,300207,300285,688116,300133,603662,002353,600066,601058,300866,688169,688036,601689,002126,603298,603338,000157,300833,600933,603997,600309,002601,300396,603259,300529,002372,300415,603179,002028,603556,603129,002444,603596,603197,601100,002472,688187,600900,600938,601899,601225,601288,600941,600285,000423,600660,300821,000922,000629"
    my_pool_list =[code.strip() for code in pool_str.split(",")]
    
    df = None 
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot() 
            if df is not None and not df.empty:
                print("✅ 成功连接新浪财经接口！")
                break
        except Exception:
            try:
                df = ak.stock_zh_a_spot_netease() 
                if df is not None and not df.empty:
                    print("✅ 成功连接网易财经接口！")
                    break
            except Exception:
                time.sleep(2)
            
    if df is None or df.empty:
        return None
        
    try:
        # 寻找代码和名称列
        code_col =[col for col in df.columns if '代码' in col or 'symbol' in col.lower()][0]
        name_col =[col for col in df.columns if '名称' in col or 'name' in col.lower()][0]
        
        # 🌟 绝杀：采用老板亲自提供的极其严谨的涨跌幅精确匹配逻辑！
        possible_change_cols =[
            col for col in df.columns
            if '涨跌幅' in col or '涨幅' in col or 'percent' in col.lower()
        ]
        
        if not possible_change_cols:
            print(f"❌ 找不到涨跌幅列，当前接口返回的列名为：{list(df.columns)}")
            return None
            
        change_col = possible_change_cols[0]
        print(f"🎯 成功锁定真实的百分比列：{change_col}")

        # 提取6位纯数字代码并过滤池子
        df['纯数字代码'] = df[code_col].astype(str).str.extract(r'(\d{6})')
        my_df = df[df['纯数字代码'].isin(my_pool_list)].copy()
        
        if my_df.empty:
            return None

        my_df[change_col] = pd.to_numeric(my_df[change_col], errors='coerce')
        
        # ⚠️ 注意这里：如果今天没票，测试时依然可以临时改成 > -10.0 出结果
        surge_df = my_df[my_df[change_col] > -10.0] 
        
        if surge_df.empty:
            return None
            
        # 🌟 按真实的【涨跌幅百分比】降序排列，取前 5 只！
        surge_df = surge_df.sort_values(by=change_col, ascending=False).head(10)
            
        stock_data_list =[]
        for index, row in surge_df.iterrows():
            change_val = round(float(row[change_col]), 2)
            stock_data_list.append({
                "name": row[name_col],
                "code": row['纯数字代码'],
                "change": f"+{change_val}" if change_val > 0 else f"{change_val}"
            })
            
        return stock_data_list
        
    except Exception as e:
        print(f"❌ 数据清洗报错：{str(e)}")
        return None

# ================= 核心2：单只股票分析引擎 =================
def ask_deepseek_single(stock_name):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    
    system_prompt = """
    你是一位严谨的A股量化研究员。
    请直接用两段话分析这只股票（绝对不准写标题，绝对不准写任何涨跌幅数字）：
    第一段：【🏰 核心产业壁垒】：(主营业务、护城河)
    第二段：【🔥 近期资金逻辑】：(受益于什么宏观政策或产业链爆发)
    大白话，客观分析，拒绝任何吹捧。
    """
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = {
        "model": "deepseek-v4-pro",
        "messages":[{"role": "system", "content": system_prompt}, {"role": "user", "content": f"请分析：{stock_name}"}],
        "temperature": 0.5
    }
    
    for i in range(3):
        try:
            response = requests.post(url, headers=headers, json=data, timeout=30)
            return response.json()['choices'][0]['message']['content'].strip()
        except Exception:
            time.sleep(2)
    return "❌ AI分析生成失败。"

# ================= 核心3：全局表格总结引擎 =================
def ask_deepseek_summary(stock_data_list):
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    
    real_data_str = ""
    for s in stock_data_list:
        real_data_str += f"股票：{s['name']}，真实涨幅：{s['change']}%\n"
        
    system_prompt = f"""
    你是一位顶级的A股策略分析师。
    我会给你今天涨幅 TOP5 异动股票的【真实数据】。
    
    【强制任务】：
    1. 生成一个 Markdown 格式的总结表格。表头必须为：| 股票 | 涨幅 | 核心驱动力 | 风险提示 |
    2. '股票'和'涨幅'这两列，【必须100%照抄】我提供给你的真实数据，绝对不准修改数字！
    3. '核心驱动力'列：用极其精炼的几个字概括（如：AI算力上游、出海红利）。
    4. '风险提示'列：一针见血指出隐患（如：估值过高、客户集中度高）。
    5. 在表格的最后，写一段加粗的【一句话总结】，点评今天整体市场的主线方向。
    
    我提供的真实数据如下：
    {real_data_str}
    """
    
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = {
        "model": "deepseek-v4-pro",
        "messages":[{"role": "system", "content": system_prompt}, {"role": "user", "content": "请输出总结表格和一句话总结，不要任何多余废话。"}],
        "temperature": 0.5 
    }
    
    print("🤖 正在生成文末总结表格...")
    for i in range(3):
        try:
            response = requests.post(url, headers=headers, json=data, timeout=40)
            return response.json()['choices'][0]['message']['content'].strip()
        except Exception:
            time.sleep(2)
    return "❌ 总结表格生成失败。"

# ================= 核心4：强制排版生成博客 =================
def write_blog_post(stock_data_list):
    today_date = datetime.datetime.now().strftime('%Y-%m-%d')
    post_time = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S+08:00')
    
    folder_path = "content/post"
    os.makedirs(folder_path, exist_ok=True)
    
    for old_file in glob.glob(os.path.join(folder_path, "report-*.md")):
        os.remove(old_file)

    md_content = f"""---
title: "🚀 【深度复盘】核心资产涨幅 TOP5 逻辑拆解 ({today_date})"
date: {post_time}
categories:
    - 量化研报
tags:
    - AI选股
draft: false
---

# 今日异动领涨 TOP 5
此报告由 **Python 抓取底层真实数据 + DeepSeek 深度逻辑分析** 组合生成。数据绝对真实，拒绝 AI 幻觉！

---

"""
    for stock in stock_data_list:
        print(f"🤖 正在呼叫 AI 单独分析：{stock['name']} ...")
        # Python 亲自写标题，使用绝对真实的 % 涨幅
        md_content += f"## 🏷️ 【{stock['name']}】({stock['code']}) 真实涨幅：<span style='color:red;'>**{stock['change']}%**</span>\n\n"
        ai_analysis = ask_deepseek_single(stock['name'])
        md_content += ai_analysis + "\n\n---\n\n"
        
    md_content += "## 📌 总结：今日领涨先锋的核心驱动力\n\n"
    summary_content = ask_deepseek_summary(stock_data_list)
    md_content += summary_content + "\n\n"
        
    md_content += f"\n*本文由自动化程序于北京时间 {today_date} 自动发布。*"
    
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
