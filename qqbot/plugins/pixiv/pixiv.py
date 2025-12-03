import aiohttp
import asyncio
import json
import urllib.parse
import random
import sys
from urllib.parse import urlparse
import os
import re

# ====== 重要配置（必须修改） ======
# 1. 本地代理地址
PROXY = "http://127.0.0.1:7897"  # ← 请修改为您的实际代理地址
USE_PROXY = True

# 2. Cloudflare Workers反向代理地址
PROXY_URL = "https://quiet-hill-31f3.math89423.workers.dev/"  # ← 请替换为您的实际Workers地址

# 3. 【关键】从浏览器获取Pixiv Cookie
#    如何获取：
#    1. 登录 https://www.pixiv.net
#    2. 按F12打开开发者工具
#    3. 在Application > Cookies中找到pixiv.net的Cookie
#    4. 复制整个Cookie字符串（包含PHPSESSID, device_token等）
#    5. 填入下方（示例格式：PHPSESSID=xxx; device_token=yyy; ...）
PIXIV_COOKIE = "PHPSESSID=14916444_EuNtNE3Yd2ZZ50A7UzivUlxP7O2hLP7s; device_token=ccd49454e972c3b547f1db56a3560575; p_ab_id=1; p_ab_id_2=1"  # ← 必须修改！

# ====== 核心函数 ======
async def search_pixiv_by_tag(tags: list, max_results=10) -> dict:
    """
    通过角色标签搜索Pixiv图片（Cookie认证版）
    """
    # 将标签用空格连接并URL编码
    search_tag = " ".join(tags)
    encoded_tag = urllib.parse.quote(search_tag)
    
    # 构造搜索URL（使用最新API路径）
    url = f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}"
    params = {
        "word": search_tag,
        "order": "date_d",
        "mode": "all",
        "p": 1,
        "s_mode": "s_tag",
        "type": "all",
        "lang": "zh",
        "version": "AAf5504c58-09e9-4e95-8c74-411a9311a4f1"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.pixiv.net/tags/{encoded_tag}/artworks",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": PIXIV_COOKIE,  # ← 关键：使用浏览器Cookie认证
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    try:
        proxy = PROXY if USE_PROXY else None
        
        async with aiohttp.ClientSession() as session:
            # 第一步：搜索作品
            async with session.get(url, headers=headers, params=params, proxy=proxy) as response:
                if response.status != 200:
                    error_text = await response.text()
                    # 尝试提取有意义的错误信息
                    try:
                        error_json = json.loads(error_text)
                        error_msg = error_json.get("error", {}).get("message", error_text[:200])
                    except:
                        error_msg = error_text[:200]
                    raise Exception(f"搜索API失败，状态码: {response.status}, 详情: {error_msg}")
                
                data = await response.json()
                
                # 检查返回数据
                if not data.get("body") or not data["body"].get("illustManga"):
                    raise Exception("API返回空数据，可能标签无效或Cookie失效")
                
                # 提取有效作品（过滤广告和无效条目）
                results = [
                    item for item in data["body"]["illustManga"]["data"] 
                    if item and isinstance(item, dict) and "id" in item and item.get("isAdContainer", 0) == 0
                ]
                
                if not results:
                    raise Exception("未找到有效作品，请尝试其他标签或检查Cookie是否有效")
                
                print(f"✅ 找到 {len(results)} 个相关作品，正在获取图片详情...")
                
                # 随机选择一个作品
                selected = random.choice(results[:max_results])
                illust_id = selected["id"]
                
                # 第二步：获取作品详情（包含原始图片URL）
                illust_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
                illust_headers = {
                    **headers,
                    "Referer": f"https://www.pixiv.net/artworks/{illust_id}"
                }
                
                async with session.get(illust_url, headers=illust_headers, proxy=proxy) as illust_response:
                    if illust_response.status != 200:
                        error_text = await illust_response.text()
                        raise Exception(f"获取作品详情失败，状态码: {illust_response.status}, 响应: {error_text[:200]}")
                    
                    illust_data = await illust_response.json()
                    if illust_data.get("error"):
                        raise Exception(f"作品详情API错误: {illust_data['message']}")
                    
                    illust_body = illust_data["body"]
                    
                    # 提取原始图片URL
                    original_img_url = illust_body["urls"]["original"]
                    regular_img_url = illust_body["urls"]["regular"]
                    
                    # 通过反向代理替换图片URL
                    proxy_original_url = replace_image_domain(original_img_url)
                    proxy_preview_url = replace_image_domain(regular_img_url)
                    
                    return {
                        "image_url": proxy_original_url,
                        "pid": str(illust_id),
                        "title": illust_body["title"],
                        "author": illust_body["userName"],
                        "author_id": illust_body["userId"],
                        "work_url": f"https://www.pixiv.net/artworks/{illust_id}",
                        "preview_url": proxy_preview_url
                    }
                
    except Exception as e:
        raise Exception(f"搜索失败: {str(e)}")

def replace_image_domain(url: str) -> str:
    """将Pixiv图片域名替换为代理域名"""
    if not url.startswith("http"):
        url = "https:" + url
    
    # 标准化代理URL
    proxy_base = PROXY_URL.rstrip('/') + '/'
    
    # 直接替换域名部分
    if "i.pximg.net" in url:
        return url.replace("https://i.pximg.net", proxy_base.rstrip('/'))
    elif "pixiv.cat" in url:
        return url.replace("https://pixiv.cat", proxy_base.rstrip('/'))
    
    # 通用处理
    path = url.replace("https://", "").replace("http://", "")
    return proxy_base + path

# ====== 命令行接口 ======
def main():
    if len(sys.argv) < 2:
        print("使用方法: python pixiv_pic.py <tag1> <tag2> ...")
        print("示例: python pixiv_pic.py 鸣潮")
        print("\n⚠️ 重要配置说明 ⚠️")
        print("1. 必须设置有效的Pixiv Cookie (PIXIV_COOKIE变量)")
        print("   - 登录pixiv.net后，从浏览器开发者工具复制完整Cookie")
        print("2. 代理配置 (PROXY): 确保代理软件已启动")
        print("3. Cloudflare Workers (PROXY_URL): 必须正确部署")
        print("\n📚 如何获取Cookie:")
        print("   a) Chrome: F12 → Application → Cookies → https://www.pixiv.net")
        print("   b) 复制整个Cookie字符串（包含PHPSESSID, device_token等）")
        print("   c) 填入代码中的PIXIV_COOKIE变量")
        sys.exit(1)
    
    # 检查Cookie是否已配置
    if "您的会话ID" in PIXIV_COOKIE or len(PIXIV_COOKIE) < 50:
        print("\n❌ 错误: 未配置有效的Pixiv Cookie!")
        print("请按照以下步骤配置:")
        print("1. 登录 https://www.pixiv.net")
        print("2. 按F12打开开发者工具")
        print("3. 转到Application > Cookies > https://www.pixiv.net")
        print("4. 复制整个Cookie字符串（包含PHPSESSID, device_token等）")
        print("5. 替换代码中的PIXIV_COOKIE变量值")
        sys.exit(1)
    
    tags = sys.argv[1:]
    print(f"🔍 正在搜索标签: {', '.join(tags)}")
    print("⏳ 请稍候...（需要网络连接，请确保代理已启动）")
    print(f"🌐 代理地址: {PROXY}")
    print(f"🛡️  代理服务: {PROXY_URL}")
    
    try:
        result = asyncio.run(search_pixiv_by_tag(tags))
        
        # 打印美化结果
        print("\n" + "="*50)
        print(f"🎨 作品标题: {result['title']}")
        print(f"👤 作者: {result['author']} (ID: {result['author_id']})")
        print(f"🆔 作品ID: {result['pid']}")
        print(f"🔗 作品链接: {result['work_url']}")
        print("-"*50)
        print(f"🖼️  预览图: {result['preview_url']}")
        print(f"💾 原图: {result['image_url']}")
        print("="*50)
        print("\n✅ 成功获取图片信息！")
        
        # 复制到剪贴板
        try:
            import pyperclip
            pyperclip.copy(result['image_url'])
            print("📋 原图URL已复制到剪贴板")
        except ImportError:
            print("💡 提示: 安装pyperclip可自动复制URL: pip install pyperclip")
            
    except Exception as e:
        print(f"\n❌ 错误: {str(e)}")
        print("\n🔍 问题排查指南:")
        print("1️⃣  Cookie问题 (最常见):")
        print("   - 检查PIXIV_COOKIE是否完整有效")
        print("   - 重新登录Pixiv并更新Cookie")
        print("   - 确保Cookie包含PHPSESSID和device_token")
        print("2️⃣  代理问题:")
        print(f"   - 测试代理: curl -x {PROXY} https://www.pixiv.net")
        print("3️⃣  Cloudflare Workers问题:")
        print(f"   - 测试代理图片: {PROXY_URL}img-original/img/2023/01/01/00/00/00/12345678_p0.jpg")
        print("4️⃣  网络问题:")
        print("   - 确保代理软件全局模式已开启")
        print("   - 尝试重启代理软件")
        sys.exit(1)

if __name__ == "__main__":
    main()