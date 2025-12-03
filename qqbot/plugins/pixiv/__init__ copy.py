import aiohttp
import json
import urllib.parse
import random
import time
import base64
from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import MessageSegment, Bot, Event
from typing import Dict, Any
import asyncio

# ====== 重要配置（必须修改） ======
PROXY = "http://127.0.0.1:7890"  # 本地代理地址
USE_PROXY = True

PROXY_URL = "https://quiet-hill-31f3.math89423.workers.dev/"  # Cloudflare Workers地址

PIXIV_COOKIE = "PHPSESSID=14916444_EuNtNE3Yd2ZZ50A7UzivUlxP7O2hLP7s; device_token=ccd49454e972c3b547f1db56a3560575; p_ab_id=1; p_ab_id_2=1"  # ← 必须修改！

# ====== 核心函数 ======
async def search_pixiv_by_tag(tags: list, max_results=10) -> dict:
    """
    通过角色标签搜索Pixiv图片
    """
    search_tag = " ".join(tags)
    encoded_tag = urllib.parse.quote(search_tag)
    
    url = f"https://www.pixiv.net/ajax/search/artworks/{encoded_tag}"
    params = {
        "word": search_tag,
        "order": "date_d",
        "mode": "all",
        "p": 1,
        "s_mode": "s_tag",
        "type": "all",
        "lang": "zh"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.pixiv.net/tags/{encoded_tag}/artworks",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": PIXIV_COOKIE,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    try:
        proxy = PROXY if USE_PROXY else None
        
        async with aiohttp.ClientSession() as session:
            # 1. 搜索作品
            async with session.get(url, headers=headers, params=params, proxy=proxy, timeout=30) as response:
                if response.status != 200:
                    error_text = await response.text()
                    try:
                        error_json = json.loads(error_text)
                        error_msg = error_json.get("error", {}).get("message", error_text[:200])
                    except:
                        error_msg = error_text[:200]
                    raise Exception(f"搜索API失败，状态码: {response.status}, 详情: {error_msg}")
                
                data = await response.json()
                
                if not data.get("body") or not data["body"].get("illustManga"):
                    raise Exception("API返回空数据，可能标签无效或Cookie失效")
                
                results = [
                    item for item in data["body"]["illustManga"]["data"] 
                    if item and isinstance(item, dict) and "id" in item and item.get("isAdContainer", 0) == 0
                ]
                
                if not results:
                    raise Exception("未找到有效作品，请尝试其他标签或检查Cookie是否有效")
                
                selected = random.choice(results[:max_results])
                illust_id = selected["id"]
                
                # 2. 获取作品详情
                illust_url = f"https://www.pixiv.net/ajax/illust/{illust_id}"
                illust_headers = {
                    **headers,
                    "Referer": f"https://www.pixiv.net/artworks/{illust_id}"
                }
                
                async with session.get(illust_url, headers=illust_headers, proxy=proxy, timeout=30) as illust_response:
                    if illust_response.status != 200:
                        error_text = await illust_response.text()
                        raise Exception(f"获取作品详情失败，状态码: {illust_response.status}, 响应: {error_text[:200]}")
                    
                    illust_data = await illust_response.json()
                    if illust_data.get("error"):
                        raise Exception(f"作品详情API错误: {illust_data['message']}")
                    
                    illust_body = illust_data["body"]
                    original_img_url = illust_body["urls"]["original"]
                    regular_img_url = illust_body["urls"]["regular"]
                    
                    # 3. 构建代理后的图片URL
                    proxy_original_url = replace_image_domain(original_img_url)
                    proxy_preview_url = replace_image_domain(regular_img_url)
                    
                    return {
                        "image_url": proxy_original_url,
                        "pid": str(illust_id),
                        "title": illust_body["title"],
                        "author": illust_body["userName"],
                        "author_id": illust_body["userId"],
                        "work_url": f"https://www.pixiv.net/artworks/{illust_id}",
                        "preview_url": proxy_preview_url,
                        "original_url": original_img_url  # 保留原始URL用于调试
                    }
                
    except Exception as e:
        raise Exception(f"搜索失败: {str(e)}")

def replace_image_domain(url: str) -> str:
    """将Pixiv图片域名替换为代理域名"""
    if not url.startswith("http"):
        url = "https:" + url
    
    proxy_base = PROXY_URL.rstrip('/') + '/'
    
    if "i.pximg.net" in url:
        return url.replace("https://i.pximg.net", proxy_base.rstrip('/'))
    elif "pixiv.cat" in url:
        return url.replace("https://pixiv.cat", proxy_base.rstrip('/'))
    
    path = url.replace("https://", "").replace("http://", "")
    return proxy_base + path

# ====== 图片下载与base64编码函数 ======
async def download_and_encode_image(image_url: str, timeout: int = 30) -> str:
    """
    下载图片并转换为base64编码字符串
    """
    try:
        proxy = PROXY if USE_PROXY else None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(
                image_url, 
                proxy=proxy, 
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                if response.status != 200:
                    raise Exception(f"图片下载失败，状态码: {response.status}")
                
                # 读取图片数据
                image_data = await response.read()
                
                # 转换为base64
                base64_encoded = base64.b64encode(image_data).decode('utf-8')
                
                return base64_encoded
                
    except Exception as e:
        logger.error(f"图片下载或编码失败: {str(e)}")
        raise Exception(f"图片处理失败: {str(e)}")

# ====== Nonebot2插件逻辑（新版语法） ======
# 1. 先创建命令处理器
pixiv_cmd = on_command("pixiv", aliases={"p"}, priority=5, block=True)

# 2. 使用 handle 装饰器添加处理函数
@pixiv_cmd.handle()
async def handle_pixiv_command(bot: Bot, event: Event):
    """处理 /pixiv 命令"""
    # 获取原始消息文本
    raw_message = str(event.get_message()).strip()
    
    # 移除命令前缀，获取参数
    command_length = len("/pixiv")  # 或者使用 len("/p")
    args = raw_message[command_length:].strip()
    
    if not args:
        await bot.send(event, "请提供搜索标签，例如：\n/pixiv 鸣潮\n/p 鸣潮")
        return
    
    tags = [tag.strip() for tag in args.split() if tag.strip()]
    logger.info(f"Pixiv搜索请求: {tags}")
    
    try:
        # 1. 搜索作品
        result = await search_pixiv_by_tag(tags)
        
        # 2. 构建消息内容
        msg_content = (
            f"🎨 作品标题: {result['title']}\n"
            f"👤 作者: {result['author']} (ID: {result['author_id']})\n"
            f"🆔 作品ID: {result['pid']}\n"
            f"🔗 作品链接: {result['work_url']}\n\n"
            f"🖼️ 正在加载图片..."
        )
        
        # 发送初步信息
        await bot.send(event, msg_content)
        
        # 3. 下载并转换图片为base64
        logger.info(f"开始下载图片: {result['image_url']}")
        base64_image = await download_and_encode_image(result['image_url'])
        
        # 4. 发送base64图片
        logger.info("图片下载成功，正在发送...")
        await bot.send(event, MessageSegment.image(base64_image))
        
        logger.info(f"成功返回图片: {result['title']} (PID: {result['pid']})")
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Pixiv搜索失败: {error_msg}")
        
        # 优化错误提示
        if "Cookie" in error_msg or "cookie" in error_msg.lower():
            error_msg = (
                "⚠️ Cookie无效！请重新获取Pixiv Cookie:\n"
                "1. 登录 https://www.pixiv.net\n"
                "2. 按 F12 打开开发者工具\n"
                "3. 进入 Application → Storage → Cookies\n"
                "4. 复制整个 Cookie 内容替换代码中的 PIXIV_COOKIE"
            )
        elif "代理" in error_msg or "proxy" in error_msg.lower():
            error_msg = (
                "⚠️ 代理配置问题！请检查:\n"
                f"- 本地代理: {PROXY}\n"
                f"- Cloudflare 代理: {PROXY_URL}\n"
                "- 确保代理软件正常运行\n"
                "- 尝试直接访问: curl -x http://127.0.0.1:7890 https://www.pixiv.net"
            )
        elif "403" in error_msg or "404" in error_msg:
            error_msg = "⚠️ 网络请求失败，请检查代理设置和Cookie有效性"
        elif "未找到有效作品" in error_msg:
            error_msg = "⚠️ 未找到相关作品，请尝试更通用的标签（如'插画'、'原神'）"
        elif "timeout" in error_msg.lower() or "超时" in error_msg:
            error_msg = (
                "⚠️ 请求超时！可能是网络不稳定或代理延迟过高\n"
                "建议:\n"
                "1. 检查Clash代理是否正常运行\n"
                "2. 尝试更换标签\n"
                "3. 检查Cloudflare Workers是否可用"
            )
        
        await bot.send(event, f"❌ 搜索失败: {error_msg}")

# ====== 添加调试命令 ======
debug_cmd = on_command("debug", priority=5, block=True)

@debug_cmd.handle()
async def debug_command(bot: Bot, event: Event):
    """调试命令"""
    await bot.send(event, f"🔧 调试信息:\n"
                         f"- 代理: {'启用' if USE_PROXY else '禁用'} ({PROXY})\n"
                         f"- Cloudflare Workers: {PROXY_URL}\n"
                         f"- Cookie: {'有效' if PIXIV_COOKIE else '缺失'}")