"""
经济日历模块
提供宏观经济事件和加密货币专属事件
"""
import aiohttp
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class EconomicCalendar:
    """经济日历聚合器，提供宏观经济事件和加密货币事件"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=15)

    async def get_macro_events(self) -> list:
        """
        宏观经济事件（CPI/FOMC/非农/GDP等）
        返回示例:
        [{"time": "2026-03-26T12:30:00Z", "event": "美国CPI同比", "country": "US",
          "importance": "high", "forecast": "2.8%", "previous": "2.9%", "actual": null}]

        使用内置的重要经济事件日历 + 可选的外部API
        """
        # 先尝试从外部API获取
        events = await self._fetch_macro_from_api()

        # 如果外部API失败，使用内置日历
        if not events:
            events = self._builtin_macro_calendar()

        # 按时间排序
        events.sort(key=lambda x: x.get("time", ""))

        return events

    async def _fetch_macro_from_api(self) -> list:
        """
        从外部API获取宏观经济日历
        尝试使用 Investing.com 非官方接口或其他公开数据源
        """
        # 尝试使用 Trading Economics 风格的公开日历数据
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "宏观日历API返回 %d，使用内置日历", resp.status
                        )
                        return []

                    data = await resp.json()
                    events = []

                    for item in data:
                        # 解析ForexFactory格式数据
                        country = item.get("country", "")
                        impact = item.get("impact", "").lower()

                        # 只保留高影响力事件
                        if impact not in ("high", "medium"):
                            continue

                        importance = "high" if impact == "high" else "medium"

                        event_date = item.get("date", "")
                        title = item.get("title", "")
                        forecast = item.get("forecast", "")
                        previous = item.get("previous", "")
                        actual = item.get("actual", None)

                        # 转换为中文事件名
                        title_cn = self._translate_event_name(title)

                        events.append(
                            {
                                "time": event_date,
                                "event": title_cn,
                                "event_en": title,
                                "country": country,
                                "importance": importance,
                                "forecast": forecast if forecast else None,
                                "previous": previous if previous else None,
                                "actual": actual if actual else None,
                                "source": "forexfactory",
                            }
                        )

                    return events

        except Exception as e:
            logger.error("获取宏观经济日历失败: %s", e)
            return []

    def _translate_event_name(self, name: str) -> str:
        """将常见经济事件翻译为中文"""
        translations = {
            "CPI": "消费者价格指数(CPI)",
            "Core CPI": "核心CPI",
            "CPI m/m": "CPI月率",
            "CPI y/y": "CPI年率",
            "Non-Farm Employment Change": "非农就业人数变动",
            "Nonfarm Payrolls": "非农就业数据",
            "Unemployment Rate": "失业率",
            "FOMC Statement": "FOMC声明",
            "FOMC Meeting Minutes": "FOMC会议纪要",
            "Federal Funds Rate": "联邦基金利率",
            "Fed Interest Rate Decision": "美联储利率决议",
            "GDP q/q": "GDP季率",
            "Advance GDP q/q": "GDP初值季率",
            "Prelim GDP q/q": "GDP预估值季率",
            "Final GDP q/q": "GDP终值季率",
            "GDP y/y": "GDP年率",
            "Retail Sales m/m": "零售销售月率",
            "Core Retail Sales m/m": "核心零售销售月率",
            "PPI m/m": "生产者价格指数(PPI)月率",
            "Core PPI m/m": "核心PPI月率",
            "PCE Price Index m/m": "PCE价格指数月率",
            "Core PCE Price Index m/m": "核心PCE价格指数月率",
            "ISM Manufacturing PMI": "ISM制造业PMI",
            "ISM Services PMI": "ISM服务业PMI",
            "ADP Non-Farm Employment Change": "ADP非农就业人数变动",
            "Initial Jobless Claims": "初请失业金人数",
            "Trade Balance": "贸易帐",
            "Consumer Confidence": "消费者信心指数",
            "Michigan Consumer Sentiment": "密歇根消费者信心指数",
            "Durable Goods Orders m/m": "耐用品订单月率",
            "Industrial Production m/m": "工业产出月率",
            "ECB Interest Rate Decision": "欧央行利率决议",
            "BOJ Policy Rate": "日央行利率决议",
            "BOE Official Bank Rate": "英央行利率决议",
            # PMI
            "Flash Manufacturing PMI": "制造业PMI初值",
            "Flash Services PMI": "服务业PMI初值",
            "Manufacturing PMI": "制造业PMI",
            "Services PMI": "服务业PMI",
            "Composite PMI": "综合PMI",
            # 各国前缀
            "French": "法国",
            "German": "德国",
            "Italian": "意大利",
            "Spanish": "西班牙",
            "Chinese": "中国",
            "Japanese": "日本",
            "British": "英国",
            # 央行官员
            "Speaks": "讲话",
            "Gov": "行长",
            "President": "主席",
            "Chair": "主席",
            # 其他
            "Housing Starts": "新屋开工",
            "Building Permits": "建筑许可",
            "Existing Home Sales": "成屋销售",
            "New Home Sales": "新屋销售",
            "Crude Oil Inventories": "原油库存",
            "Natural Gas Storage": "天然气库存",
            "CB Consumer Confidence": "咨商会消费者信心指数",
            "Current Account": "经常帐",
            "Empire State Manufacturing Index": "纽约联储制造业指数",
            "Philly Fed Manufacturing Index": "费城联储制造业指数",
        }

        # 先尝试精确匹配
        if name in translations:
            return translations[name]

        # 组合翻译：先翻译国家前缀，再翻译事件名
        result = name
        country_prefixes = {"French ": "法国", "German ": "德国", "Italian ": "意大利",
                           "Spanish ": "西班牙", "Chinese ": "中国", "Japanese ": "日本",
                           "British ": "英国", "Canadian ": "加拿大", "Australian ": "澳大利亚"}
        country_cn = ""
        event_part = name
        for prefix, cn in country_prefixes.items():
            if name.startswith(prefix):
                country_cn = cn
                event_part = name[len(prefix):]
                break

        # 翻译事件部分
        for key, value in translations.items():
            if key.lower() == event_part.lower():
                return country_cn + value
            if key.lower() in event_part.lower():
                return country_cn + value

        # 处理央行官员讲话
        if "Speaks" in name:
            return name.replace("Speaks", "讲话").replace("Gov ", "行长")

        return name

    def _builtin_macro_calendar(self) -> list:
        """
        内置重要宏观经济事件日历
        基于常规公布时间表生成未来30天的预期事件
        """
        now = datetime.utcnow()
        events = []

        # 定义周期性事件模板
        recurring_events = [
            {
                "event": "美国CPI年率",
                "country": "US",
                "importance": "high",
                "typical_day": 10,  # 每月约10-13日
                "time_of_day": "12:30:00",
            },
            {
                "event": "美国核心CPI月率",
                "country": "US",
                "importance": "high",
                "typical_day": 10,
                "time_of_day": "12:30:00",
            },
            {
                "event": "美国非农就业数据",
                "country": "US",
                "importance": "high",
                "typical_day": 5,  # 每月第一个周五
                "time_of_day": "12:30:00",
            },
            {
                "event": "美联储利率决议(FOMC)",
                "country": "US",
                "importance": "high",
                "typical_day": 0,  # 需要特殊处理
                "time_of_day": "18:00:00",
            },
            {
                "event": "初请失业金人数",
                "country": "US",
                "importance": "medium",
                "typical_day": -1,  # 每周四
                "time_of_day": "12:30:00",
            },
            {
                "event": "美国PPI月率",
                "country": "US",
                "importance": "medium",
                "typical_day": 14,
                "time_of_day": "12:30:00",
            },
            {
                "event": "美国零售销售月率",
                "country": "US",
                "importance": "high",
                "typical_day": 15,
                "time_of_day": "12:30:00",
            },
            {
                "event": "核心PCE价格指数月率",
                "country": "US",
                "importance": "high",
                "typical_day": 28,
                "time_of_day": "12:30:00",
            },
        ]

        # 2026年FOMC会议日期（预估）
        fomc_dates_2026 = [
            "2026-01-28",
            "2026-03-18",
            "2026-05-06",
            "2026-06-17",
            "2026-07-29",
            "2026-09-16",
            "2026-11-04",
            "2026-12-16",
        ]

        # 生成未来60天的事件
        for template in recurring_events:
            if template["typical_day"] == -1:
                # 每周四的事件
                d = now
                for _ in range(9):  # 未来约9周
                    days_until_thursday = (3 - d.weekday()) % 7
                    if days_until_thursday == 0 and d.date() == now.date():
                        days_until_thursday = 7
                    next_thursday = d + timedelta(days=days_until_thursday)
                    event_time = next_thursday.strftime(
                        f"%Y-%m-%dT{template['time_of_day']}Z"
                    )
                    events.append(
                        {
                            "time": event_time,
                            "event": template["event"],
                            "country": template["country"],
                            "importance": template["importance"],
                            "forecast": None,
                            "previous": None,
                            "actual": None,
                            "source": "builtin",
                        }
                    )
                    d = next_thursday + timedelta(days=1)
            elif template["typical_day"] == 0:
                # FOMC特殊处理
                for fomc_date in fomc_dates_2026:
                    fomc_dt = datetime.strptime(fomc_date, "%Y-%m-%d")
                    if fomc_dt > now - timedelta(days=1) and fomc_dt < now + timedelta(
                        days=60
                    ):
                        event_time = f"{fomc_date}T{template['time_of_day']}Z"
                        events.append(
                            {
                                "time": event_time,
                                "event": template["event"],
                                "country": template["country"],
                                "importance": template["importance"],
                                "forecast": None,
                                "previous": None,
                                "actual": None,
                                "source": "builtin",
                            }
                        )
            else:
                # 月度事件
                for month_offset in range(3):
                    event_month = now.month + month_offset
                    event_year = now.year
                    if event_month > 12:
                        event_month -= 12
                        event_year += 1

                    day = min(template["typical_day"], 28)
                    try:
                        event_dt = datetime(event_year, event_month, day)
                    except ValueError:
                        continue

                    if event_dt < now - timedelta(days=1):
                        continue
                    if event_dt > now + timedelta(days=60):
                        continue

                    event_time = event_dt.strftime(
                        f"%Y-%m-%dT{template['time_of_day']}Z"
                    )
                    events.append(
                        {
                            "time": event_time,
                            "event": template["event"],
                            "country": template["country"],
                            "importance": template["importance"],
                            "forecast": None,
                            "previous": None,
                            "actual": None,
                            "source": "builtin",
                        }
                    )

        return events

    async def get_crypto_events(self) -> list:
        """
        加密货币专属事件
        类型: token_unlock / network_upgrade / mainnet_launch /
              halving / airdrop / exchange_listing / regulation
        """
        # 尝试从外部API获取
        events = await self._fetch_crypto_events_api()

        # 合并内置事件
        builtin = self._builtin_crypto_events()
        events.extend(builtin)

        # 去重（按event+time）
        seen = set()
        unique_events = []
        for e in events:
            key = (e.get("event", ""), e.get("time", ""))
            if key not in seen:
                seen.add(key)
                unique_events.append(e)

        # 按时间排序
        unique_events.sort(key=lambda x: x.get("time", ""))

        return unique_events

    async def _fetch_crypto_events_api(self) -> list:
        """
        从CoinMarketCal等API获取加密货币事件
        CoinMarketCal需要API Key，此处使用公开接口
        """
        # 使用 CoinGecko 的 status_updates 作为事件源（免费）
        url = "https://api.coingecko.com/api/v3/events"

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                headers = {"Accept": "application/json"}
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        logger.info(
                            "CoinGecko events API 返回 %d，使用内置事件", resp.status
                        )
                        return []

                    data = await resp.json()
                    events_data = data.get("data", data) if isinstance(data, dict) else data

                    if not isinstance(events_data, list):
                        return []

                    events = []
                    for item in events_data:
                        event_type = self._classify_crypto_event(
                            item.get("type", ""), item.get("title", "")
                        )
                        start_date = item.get("start_date", "")
                        if not start_date:
                            continue

                        events.append(
                            {
                                "time": start_date,
                                "event": item.get("title", ""),
                                "symbol": item.get("symbol", "").upper(),
                                "type": event_type,
                                "importance": self._crypto_event_importance(
                                    event_type
                                ),
                                "details": item.get("description", ""),
                                "source": "coingecko",
                            }
                        )

                    return events

        except Exception as e:
            logger.error("获取加密货币事件失败: %s", e)
            return []

    def _classify_crypto_event(self, event_type: str, title: str) -> str:
        """对加密货币事件进行分类"""
        title_lower = title.lower()
        type_lower = event_type.lower()

        if any(
            kw in title_lower
            for kw in ["unlock", "vesting", "release", "解锁"]
        ):
            return "token_unlock"
        elif any(
            kw in title_lower
            for kw in ["upgrade", "fork", "update", "升级", "硬分叉"]
        ):
            return "network_upgrade"
        elif any(
            kw in title_lower
            for kw in ["mainnet", "launch", "主网"]
        ):
            return "mainnet_launch"
        elif any(kw in title_lower for kw in ["halving", "减半"]):
            return "halving"
        elif any(kw in title_lower for kw in ["airdrop", "空投"]):
            return "airdrop"
        elif any(
            kw in title_lower
            for kw in ["listing", "上线", "上架"]
        ):
            return "exchange_listing"
        elif any(
            kw in title_lower
            for kw in ["regulation", "sec", "法规", "监管", "etf"]
        ):
            return "regulation"
        elif "conference" in type_lower or "event" in type_lower:
            return "conference"
        else:
            return "other"

    def _crypto_event_importance(self, event_type: str) -> str:
        """根据事件类型判断重要性"""
        high_importance = {
            "halving",
            "regulation",
            "network_upgrade",
            "token_unlock",
        }
        medium_importance = {
            "mainnet_launch",
            "exchange_listing",
            "airdrop",
        }

        if event_type in high_importance:
            return "high"
        elif event_type in medium_importance:
            return "medium"
        else:
            return "low"

    def _builtin_crypto_events(self) -> list:
        """
        内置加密货币重大事件日历
        定期更新已知的确定性事件
        """
        now = datetime.utcnow()
        now_str = now.strftime("%Y-%m-%dT00:00:00Z")

        # 内置已知的重大事件
        known_events = [
            # ---- Token Unlock 事件 ----
            {
                "time": "2026-04-01T00:00:00Z",
                "event": "SUI Token Unlock",
                "symbol": "SUI",
                "type": "token_unlock",
                "importance": "high",
                "details": "解锁约1.28亿美元SUI代币",
                "source": "builtin",
            },
            {
                "time": "2026-04-07T00:00:00Z",
                "event": "ARB Token Unlock",
                "symbol": "ARB",
                "type": "token_unlock",
                "importance": "high",
                "details": "Arbitrum大额代币解锁",
                "source": "builtin",
            },
            {
                "time": "2026-04-12T00:00:00Z",
                "event": "APT Token Unlock",
                "symbol": "APT",
                "type": "token_unlock",
                "importance": "medium",
                "details": "Aptos月度代币解锁",
                "source": "builtin",
            },
            {
                "time": "2026-04-16T00:00:00Z",
                "event": "OP Token Unlock",
                "symbol": "OP",
                "type": "token_unlock",
                "importance": "high",
                "details": "Optimism代币解锁",
                "source": "builtin",
            },
            # ---- 网络升级事件 ----
            {
                "time": "2026-04-15T00:00:00Z",
                "event": "Ethereum Pectra 升级",
                "symbol": "ETH",
                "type": "network_upgrade",
                "importance": "high",
                "details": "以太坊Pectra升级，包含EIP-7702等多项改进",
                "source": "builtin",
            },
            # ---- 减半事件 ----
            {
                "time": "2028-04-01T00:00:00Z",
                "event": "Bitcoin Halving (预估)",
                "symbol": "BTC",
                "type": "halving",
                "importance": "high",
                "details": "比特币第五次减半，区块奖励从3.125降至1.5625 BTC",
                "source": "builtin",
            },
            # ---- 监管事件 ----
            {
                "time": "2026-04-15T00:00:00Z",
                "event": "美国加密货币税务申报截止日",
                "symbol": "",
                "type": "regulation",
                "importance": "high",
                "details": "美国纳税人需申报加密货币交易收益",
                "source": "builtin",
            },
        ]

        # 过滤：只返回未来90天内的事件
        cutoff = now + timedelta(days=90)
        filtered = []
        for event in known_events:
            try:
                event_dt = datetime.strptime(
                    event["time"], "%Y-%m-%dT%H:%M:%SZ"
                )
                if now - timedelta(days=1) <= event_dt <= cutoff:
                    filtered.append(event)
            except (ValueError, KeyError):
                continue

        return filtered
