import aiohttp
import asyncio
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image

@register("astrbot_plugin_hanime", "YourName", "Hanime搜索插件", "1.0.1")
class HanimePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 配置：搜索结果显示数量 (建议不要太大，因为现在会预加载详情页)
        self.max_results = 5 
        self.search_cache = {} 
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": "https://hanime1.me/" # 加上 Referer 防盗链
        }

    async def _fetch_video_detail(self, session, url, idx):
        """
        辅助函数：访问详情页，提取高清封面和确切标题
        返回: (index, data_dict) 或 None
        """
        try:
            async with session.get(url, headers=self.headers, timeout=10) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                soup = BeautifulSoup(html, "lxml")
                
                # 提取标题 (og:title 通常最准确)
                og_title = soup.find("meta", property="og:title")
                title = og_title["content"] if og_title else "未知标题"
                
                # 提取封面 (从 video poster 属性获取，这是最高清且真实的封面)
                video_tag = soup.find("video", id="player")
                cover_url = ""
                if video_tag and video_tag.has_attr("poster"):
                    cover_url = video_tag["poster"]
                
                # 如果没有 poster，尝试 og:image
                if not cover_url:
                    og_image = soup.find("meta", property="og:image")
                    if og_image:
                        cover_url = og_image["content"]

                return idx, {
                    "title": title,
                    "url": url,
                    "cover_url": cover_url
                }
        except Exception as e:
            logger.error(f"Parse detail error: {e}")
            return None

    # ---------------- 指令: 搜索 (/lf) ----------------
    @filter.command("lf")
    async def search_hanime(self, event: AstrMessageEvent, keyword: str):
        """搜索 Hanime1: /lf <关键词>"""
        if not keyword:
            yield event.plain_result("请输入关键词，例如：/lf 某个番剧")
            return

        yield event.plain_result(f"🔍 正在搜索 '{keyword}' 并解析封面，请稍候...")

        search_url = f"https://hanime1.me/search?query={keyword}"
        
        try:
            async with aiohttp.ClientSession() as session:
                # 1. 获取搜索列表
                async with session.get(search_url, headers=self.headers) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"访问失败，状态码: {resp.status}")
                        return
                    html = await resp.text()

                # 2. 初步解析列表
                soup = BeautifulSoup(html, "lxml")
                results_div = soup.find("div", class_="content-padding-new")
                if not results_div:
                     yield event.plain_result("未找到相关结果。")
                     return

                # 获取所有可能的条目
                raw_items = results_div.find_all("div", class_="col-xs-6")
                
                candidate_urls = []
                
                # 3. 筛选出真正的视频链接 (过滤广告)
                for item in raw_items:
                    if len(candidate_urls) >= self.max_results:
                        break
                        
                    a_tag = item.find("a", class_="overlay")
                    if not a_tag:
                        continue
                    
                    href = a_tag.get("href")
                    # 核心过滤逻辑：必须包含 /watch?v= 才是正片，广告通常没有这个特征
                    if not href or "/watch?v=" not in href:
                        continue
                        
                    if not href.startswith("http"):
                        href = "https://hanime1.me" + href
                    
                    candidate_urls.append(href)

                if not candidate_urls:
                    yield event.plain_result("未找到相关视频 (已过滤广告)。")
                    return

                # 4. 并发请求详情页 (为了获取正确的封面图)
                tasks = []
                for i, url in enumerate(candidate_urls):
                    tasks.append(self._fetch_video_detail(session, url, i))
                
                # 等待所有详情页解析完成
                details_results = await asyncio.gather(*tasks)
                
                # 整理结果
                valid_items = []
                for res in details_results:
                    if res:
                        valid_items.append(res[1]) # data_dict
                
                if not valid_items:
                    yield event.plain_result("解析视频详情失败，请稍后重试。")
                    return

                # 5. 缓存并发送消息
                user_id = event.get_sender_id()
                self.search_cache[user_id] = valid_items
                
                msg_chain = [Plain(f"✨ 关键词 '{keyword}' 搜索结果:\n")]

                for idx, data in enumerate(valid_items):
                    title = data["title"]
                    cover = data["cover_url"]
                    
                    msg_chain.append(Plain(f"\n{idx + 1}. {title}\n"))
                    if cover:
                        msg_chain.append(Image.fromURL(cover))
                
                msg_chain.append(Plain("\n💡 发送 /lfxz <编号> 获取视频直链"))
                yield event.chain_result(msg_chain)

        except Exception as e:
            logger.error(f"Search error: {e}")
            yield event.plain_result(f"发生错误: {str(e)}")

    # ---------------- 指令: 选集 (/lfxz) ----------------
    @filter.command("lfxz")
    async def select_video(self, event: AstrMessageEvent, index: str):
        """获取视频直链: /lfxz <编号>"""
        user_id = event.get_sender_id()
        
        if user_id not in self.search_cache or not self.search_cache[user_id]:
            yield event.plain_result("请先使用 /lf <关键词> 进行搜索。")
            return

        if not index.isdigit():
            yield event.plain_result("请输入有效的数字编号。")
            return
        
        idx = int(index) - 1
        if idx < 0 or idx >= len(self.search_cache[user_id]):
            yield event.plain_result("编号超出范围。")
            return

        target = self.search_cache[user_id][idx]
        detail_url = target["url"]
        title = target["title"]

        yield event.plain_result(f"正在解析 '{title}' 直链...")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(detail_url, headers=self.headers) as resp:
                    if resp.status != 200:
                        yield event.plain_result("无法访问视频详情页。")
                        return
                    html = await resp.text()
        except Exception as e:
            yield event.plain_result(f"网络错误: {e}")
            return

        soup = BeautifulSoup(html, "lxml")
        video_tag = soup.find("video", id="player")
        
        video_src = ""
        if video_tag:
            # 优先找 source 标签
            source_tag = video_tag.find("source")
            if source_tag:
                video_src = source_tag.get("src")
        
        # 兜底正则查找
        if not video_src:
            import re
            match = re.search(r'https?://[^\s"\']+\.m3u8', html)
            if match:
                video_src = match.group(0)

        if video_src:
            # 发送直链
            yield event.plain_result(f"🎬 {title}\n\n直链地址:\n{video_src}\n\n(复制链接到浏览器或下载器即可观看/下载)")
        else:
            yield event.plain_result("未解析到视频直链，请重试或更换视频。")
