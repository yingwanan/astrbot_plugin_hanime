import re
import aiohttp
import asyncio
from collections import OrderedDict
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image

class HanimePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 配置：搜索结果显示数量
        self.max_results = 5
        
        # 缓存设置：最大缓存用户数
        self.max_cache_size = 50
        # 使用 OrderedDict 实现 LRU 缓存
        self.search_cache = OrderedDict()
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": "https://hanime1.me/"
        }
        self.session = None

    async def initialize(self):
        """插件初始化时创建全局 Session"""
        self.session = aiohttp.ClientSession(headers=self.headers)
        logger.info(f"[{self.__class__.__name__}] ClientSession initialized.")

    async def terminate(self):
        """插件卸载/停止时关闭 Session"""
        if self.session:
            await self.session.close()
            logger.info(f"[{self.__class__.__name__}] ClientSession closed.")

    def _update_cache(self, user_id, data):
        """更新缓存，维护 LRU 策略"""
        if user_id in self.search_cache:
            self.search_cache.move_to_end(user_id)
        self.search_cache[user_id] = data
        
        # 如果超过最大限制，移除最久未使用的条目 (FIFO 行为配合 move_to_end 变为 LRU)
        if len(self.search_cache) > self.max_cache_size:
            self.search_cache.popitem(last=False)

    async def _fetch_video_detail(self, url, idx):
        """
        辅助函数：访问详情页，提取高清封面和确切标题
        """
        if not self.session:
            return None

        try:
            # 设置超时防止卡顿
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                soup = BeautifulSoup(html, "lxml")
                
                # 提取标题
                og_title = soup.find("meta", property="og:title")
                title = og_title["content"] if og_title else "未知标题"
                
                # 提取封面 (优先 poster)
                video_tag = soup.find("video", id="player")
                cover_url = ""
                if video_tag and video_tag.has_attr("poster"):
                    cover_url = video_tag["poster"]
                
                # 兜底提取封面
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
            logger.error(f"Parse detail error for {url}: {e}")
            return None

    # ---------------- 指令: 搜索 (/lf) ----------------
    @filter.command("lf")
    async def search_hanime(self, event: AstrMessageEvent, keyword: str):
        """搜索 Hanime1: /lf <关键词>"""
        if not keyword:
            yield event.plain_result("请输入关键词，例如：/lf 某个番剧")
            return
        
        if not self.session:
            # 极少见的情况，防止初始化失败导致 crash
            self.session = aiohttp.ClientSession(headers=self.headers)

        yield event.plain_result(f"🔍 正在搜索 '{keyword}' 并解析封面，请稍候...")

        search_url = f"https://hanime1.me/search?query={keyword}"
        
        try:
            # 1. 获取搜索列表
            async with self.session.get(search_url) as resp:
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

            raw_items = results_div.find_all("div", class_="col-xs-6")
            candidate_urls = []
            
            # 3. 筛选链接 (过滤广告)
            for item in raw_items:
                if len(candidate_urls) >= self.max_results:
                    break
                    
                a_tag = item.find("a", class_="overlay")
                if not a_tag:
                    continue
                
                href = a_tag.get("href")
                # 核心过滤：必须包含 /watch?v=
                if not href or "/watch?v=" not in href:
                    continue
                    
                if not href.startswith("http"):
                    href = "https://hanime1.me" + href
                
                candidate_urls.append(href)

            if not candidate_urls:
                yield event.plain_result("未找到相关视频 (已过滤广告)。")
                return

            # 4. 并发预加载详情页
            tasks = []
            for i, url in enumerate(candidate_urls):
                tasks.append(self._fetch_video_detail(url, i))
            
            details_results = await asyncio.gather(*tasks)
            
            valid_items = []
            for res in details_results:
                if res:
                    valid_items.append(res[1])
            
            if not valid_items:
                yield event.plain_result("解析视频详情失败，请稍后重试。")
                return

            # 5. 更新缓存 (LRU)
            user_id = event.get_sender_id()
            self._update_cache(user_id, valid_items)
            
            # 6. 构建消息
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
        
        # 检查缓存
        if user_id not in self.search_cache:
            yield event.plain_result("请先使用 /lf <关键词> 进行搜索。")
            return

        # 刷新 LRU 位置
        self.search_cache.move_to_end(user_id)
        items = self.search_cache[user_id]

        if not index.isdigit():
            yield event.plain_result("请输入有效的数字编号。")
            return
        
        idx = int(index) - 1
        if idx < 0 or idx >= len(items):
            yield event.plain_result("编号超出范围。")
            return

        target = items[idx]
        detail_url = target["url"]
        title = target["title"]

        yield event.plain_result(f"正在解析 '{title}' 直链...")

        try:
            if not self.session:
                self.session = aiohttp.ClientSession(headers=self.headers)

            async with self.session.get(detail_url) as resp:
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
            source_tag = video_tag.find("source")
            if source_tag:
                video_src = source_tag.get("src")
        
        if not video_src:
            # 正则已移至顶部引用
            match = re.search(r'https?://[^\s"\']+\.m3u8', html)
            if match:
                video_src = match.group(0)

        if video_src:
            yield event.plain_result(f"🎬 {title}\n\n直链地址:\n{video_src}\n\n(复制链接到浏览器或下载器即可观看/下载)")
        else:
            yield event.plain_result("未解析到视频直链，请重试或更换视频。")
