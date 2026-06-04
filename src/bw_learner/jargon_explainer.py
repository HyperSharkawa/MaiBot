import re
import time
from typing import List, Dict, Optional, Any, Tuple

import ahocorasick

from src.common.logger import get_logger
from src.common.database.database_model import Jargon
from src.llm_models.utils_model import LLMRequest
from src.config.config import model_config, global_config
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.bw_learner.jargon_miner import search_jargon
from src.bw_learner.learner_utils import (
    is_bot_message,
    contains_bot_self_name,
    parse_chat_id_list,
    chat_id_list_contains,
)

logger = get_logger("jargon")

# 全局 Aho-Corasick 自动机缓存
_jargon_automaton: Optional[ahocorasick.Automaton] = None
_jargon_metadata: Dict[int, Dict[str, Any]] = {}  # {jargon_id: {content, is_global, chat_id, ...}}
_cache_last_update: float = 0.0
_CACHE_TTL = 300.0  # 缓存有效期 5 分钟


def _build_jargon_automaton() -> Tuple[ahocorasick.Automaton, Dict[int, Dict[str, Any]]]:
    """
    构建黑话 Aho-Corasick 自动机及元数据缓存
    
    使用 Aho-Corasick 算法实现多模式匹配，一次扫描即可匹配所有黑话。
    时间复杂度从 O(n×m) 降至 O(n+m+z)，其中 n=黑话数量，m=文本长度，z=匹配数量。
    
    Returns:
        Tuple[Automaton, Dict]: (自动机对象, {jargon_id: 元数据字典})
    """
    logger.debug("正在构建黑话 Aho-Corasick 自动机...")
    start_time = time.time()
    
    automaton = ahocorasick.Automaton()
    metadata: Dict[int, Dict[str, Any]] = {}
    
    # 查询所有有 meaning 的黑话
    query = Jargon.select().where((Jargon.meaning.is_null(False)) & (Jargon.meaning != ""))
    
    added_count = 0
    for jargon in query:
        content = jargon.content or ""
        if not content or not content.strip():
            continue
        
        # 添加到自动机（小写匹配，大小写不敏感）
        try:
            automaton.add_word(content.lower(), jargon.id)
            added_count += 1
            
            # 保存元数据供后续过滤使用
            metadata[jargon.id] = {
                'content': content,
                'is_global': jargon.is_global,
                'chat_id': jargon.chat_id,
            }
        except Exception as e:
            logger.warning(f"添加黑话到自动机失败: {content}, 错误: {e}")
            continue
    
    # 构建自动机（必须调用才能使用）
    automaton.make_automaton()
    
    build_time = time.time() - start_time
    logger.debug(f"黑话自动机构建完成: {added_count} 条, 耗时 {build_time:.3f}s")
    
    return automaton, metadata


def _get_jargon_automaton() -> Tuple[ahocorasick.Automaton, Dict[int, Dict[str, Any]]]:
    """
    获取黑话 Aho-Corasick 自动机（带缓存）
    
    缓存每 5 分钟刷新一次，以反映数据库更新。
    
    Returns:
        Tuple[Automaton, Dict]: (自动机对象, {jargon_id: 元数据字典})
    """
    global _jargon_automaton, _jargon_metadata, _cache_last_update
    
    current_time = time.time()
    
    # 检查缓存是否有效
    if (
        current_time - _cache_last_update < _CACHE_TTL
        and _jargon_automaton is not None
        and _jargon_metadata
    ):
        return _jargon_automaton, _jargon_metadata
    
    # 重建缓存
    _jargon_automaton, _jargon_metadata = _build_jargon_automaton()
    _cache_last_update = current_time
    
    return _jargon_automaton, _jargon_metadata


def _init_explainer_prompts() -> None:
    """初始化黑话解释器相关的prompt"""
    # Prompt：概括黑话解释结果
    summarize_prompt_str = """上下文聊天内容:
{chat_context}

在上下文中提取到的黑话及其含义:
{jargon_explanations}

请根据上述信息，对黑话解释进行概括和整理。
- 如果上下文中有黑话出现，请简要说明这些黑话在上下文中的使用情况
- 将所有黑话解释整理成简洁、易读的一段话
- 输出格式要自然，适合作为回复参考信息
请输出概括后的黑话解释（直接输出一段平文本，不要标题，无特殊格式或markdown格式，不要使用JSON格式）：
"""
    Prompt(summarize_prompt_str, "jargon_explainer_summarize_prompt")


_init_explainer_prompts()


class JargonExplainer:
    """黑话解释器，用于在回复前识别和解释上下文中的黑话"""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.llm = LLMRequest(
            model_set=model_config.model_task_config.tool_use,
            request_type="jargon.explain",
        )

    def match_jargon_from_messages(self, messages: List[Any]) -> List[Dict[str, str]]:
        """
        通过直接匹配数据库中的jargon字符串来提取黑话

        Args:
            messages: 消息列表

        Returns:
            List[Dict[str, str]]: 提取到的黑话列表，每个元素包含content
        """
        start_time = time.time()

        if not messages:
            return []

        # 收集所有消息的文本内容
        message_texts: List[str] = []
        for msg in messages:
            # 跳过机器人自己的消息
            if is_bot_message(msg):
                continue

            msg_text = (
                getattr(msg, "display_message", None) or getattr(msg, "processed_plain_text", None) or ""
            ).strip()
            if msg_text:
                message_texts.append(msg_text)

        if not message_texts:
            return []

        # 合并所有消息文本
        combined_text = " ".join(message_texts)
        combined_text_lower = combined_text.lower()  # 小写化用于匹配

        # 获取 Aho-Corasick 自动机和元数据
        automaton, metadata = _get_jargon_automaton()

        # 使用 Aho-Corasick 一次扫描匹配所有黑话
        matched_jargon: Dict[str, Dict[str, str]] = {}
        query_time = time.time()

        for end_index, jargon_id in automaton.iter(combined_text_lower):
            # 从元数据中获取黑话信息
            meta = metadata.get(jargon_id)
            if not meta:
                continue
            
            content = meta['content']
            
            # 跳过包含机器人昵称的词条
            if contains_bot_self_name(content):
                continue
            
            # 检查 chat_id（如果 all_global=False）
            if not global_config.expression.all_global_jargon:
                if meta['is_global']:
                    # 全局黑话，包含
                    pass
                else:
                    # 检查 chat_id 列表是否包含当前 chat_id
                    chat_id_list = parse_chat_id_list(meta['chat_id'])
                    if not chat_id_list_contains(chat_id_list, self.chat_id):
                        continue
            
            # 对纯英文黑话进行单词边界检查（避免部分匹配）
            if not re.search(r"[\u4e00-\u9fff]", content):  # 纯英文/数字
                # 检查匹配位置的前后字符是否为单词边界
                start_index = end_index - len(content) + 1
                
                # 检查前边界
                if start_index > 0:
                    prev_char = combined_text[start_index - 1]
                    if prev_char.isalnum() or prev_char == '_':
                        continue  # 前面是字母数字或下划线，不是单词边界
                
                # 检查后边界
                if end_index + 1 < len(combined_text):
                    next_char = combined_text[end_index + 1]
                    if next_char.isalnum() or next_char == '_':
                        continue  # 后面是字母数字或下划线，不是单词边界
            
            # 匹配成功，记录（去重）
            if content not in matched_jargon:
                matched_jargon[content] = {"content": content}

        match_time = time.time()
        total_time = match_time - start_time
        query_duration = query_time - start_time
        match_duration = match_time - query_time

        logger.debug(
            f"黑话匹配完成: 查询耗时 {query_duration:.3f}s, 匹配耗时 {match_duration:.3f}s, "
            f"总耗时 {total_time:.3f}s, 匹配到 {len(matched_jargon)} 个黑话"
        )

        return list(matched_jargon.values())

    async def explain_jargon(self, messages: List[Any], chat_context: str) -> Optional[str]:
        """
        解释上下文中的黑话

        Args:
            messages: 消息列表
            chat_context: 聊天上下文的文本表示

        Returns:
            Optional[str]: 黑话解释的概括文本，如果没有黑话则返回None
        """
        if not messages:
            return None

        # 直接匹配方式：从数据库中查询jargon并在消息中匹配
        jargon_entries = self.match_jargon_from_messages(messages)

        if not jargon_entries:
            return None

        # 去重（按content）
        unique_jargon: Dict[str, Dict[str, str]] = {}
        for entry in jargon_entries:
            content = entry["content"]
            if content not in unique_jargon:
                unique_jargon[content] = entry

        jargon_list = list(unique_jargon.values())
        logger.info(f"从上下文中提取到 {len(jargon_list)} 个黑话: {[j['content'] for j in jargon_list]}")

        # 查询每个黑话的含义
        jargon_explanations: List[str] = []
        for entry in jargon_list:
            content = entry["content"]

            # 根据是否开启全局黑话，决定查询方式
            if global_config.expression.all_global_jargon:
                # 开启全局黑话：查询所有is_global=True的记录
                results = search_jargon(
                    keyword=content,
                    chat_id=None,  # 不指定chat_id，查询全局黑话
                    limit=1,
                    case_sensitive=False,
                    fuzzy=False,  # 精确匹配
                )
            else:
                # 关闭全局黑话：优先查询当前聊天或全局的黑话
                results = search_jargon(
                    keyword=content,
                    chat_id=self.chat_id,
                    limit=1,
                    case_sensitive=False,
                    fuzzy=False,  # 精确匹配
                )

            if results and len(results) > 0:
                meaning = results[0].get("meaning", "").strip()
                if meaning:
                    jargon_explanations.append(f"- {content}: {meaning}")
                else:
                    logger.info(f"黑话 {content} 没有找到含义")
            else:
                logger.info(f"黑话 {content} 未在数据库中找到")

        if not jargon_explanations:
            logger.info("没有找到任何黑话的含义，跳过解释")
            return None

        # 拼接所有黑话解释
        explanations_text = "\n".join(jargon_explanations)

        # 使用LLM概括黑话解释
        summarize_prompt = await global_prompt_manager.format_prompt(
            "jargon_explainer_summarize_prompt",
            chat_context=chat_context,
            jargon_explanations=explanations_text,
        )

        summary, _ = await self.llm.generate_response_async(summarize_prompt)
        if not summary:
            # 如果LLM概括失败，直接返回原始解释
            return f"上下文中的黑话解释：\n{explanations_text}"

        summary = summary.strip()
        if not summary:
            return f"上下文中的黑话解释：\n{explanations_text}"

        return summary


async def explain_jargon_in_context(chat_id: str, messages: List[Any], chat_context: str) -> Optional[str]:
    """
    解释上下文中的黑话（便捷函数）

    Args:
        chat_id: 聊天ID
        messages: 消息列表
        chat_context: 聊天上下文的文本表示

    Returns:
        Optional[str]: 黑话解释的概括文本，如果没有黑话则返回None
    """
    explainer = JargonExplainer(chat_id)
    return await explainer.explain_jargon(messages, chat_context)


def match_jargon_from_text(chat_text: str, chat_id: str) -> List[str]:
    """直接在聊天文本中匹配已知的jargon，返回出现过的黑话列表

    Args:
        chat_text: 要匹配的聊天文本
        chat_id: 聊天ID

    Returns:
        List[str]: 匹配到的黑话列表
    """
    if not chat_text or not chat_text.strip():
        return []

    query = Jargon.select().where((Jargon.meaning.is_null(False)) & (Jargon.meaning != ""))
    if global_config.expression.all_global_jargon:
        query = query.where(Jargon.is_global)

    query = query.order_by(Jargon.count.desc())

    matched: Dict[str, None] = {}

    for jargon in query:
        content = (jargon.content or "").strip()
        if not content:
            continue

        if not global_config.expression.all_global_jargon and not jargon.is_global:
            chat_id_list = parse_chat_id_list(jargon.chat_id)
            if not chat_id_list_contains(chat_id_list, chat_id):
                continue

        pattern = re.escape(content)
        if re.search(r"[\u4e00-\u9fff]", content):
            search_pattern = pattern
        else:
            search_pattern = r"\b" + pattern + r"\b"

        if re.search(search_pattern, chat_text, re.IGNORECASE):
            matched[content] = None

    logger.info(f"匹配到 {len(matched)} 个黑话")

    return list(matched.keys())


async def retrieve_concepts_with_jargon(concepts: List[str], chat_id: str) -> str:
    """对概念列表进行jargon检索

    Args:
        concepts: 概念列表
        chat_id: 聊天ID

    Returns:
        str: 检索结果字符串
    """
    if not concepts:
        return ""

    results = []
    exact_matches = []  # 收集所有精确匹配的概念
    for concept in concepts:
        concept = concept.strip()
        if not concept:
            continue

        # 先尝试精确匹配
        jargon_results = search_jargon(keyword=concept, chat_id=chat_id, limit=10, case_sensitive=False, fuzzy=False)

        is_fuzzy_match = False

        # 如果精确匹配未找到，尝试模糊搜索
        if not jargon_results:
            jargon_results = search_jargon(keyword=concept, chat_id=chat_id, limit=10, case_sensitive=False, fuzzy=True)
            is_fuzzy_match = True

        if jargon_results:
            # 找到结果
            if is_fuzzy_match:
                # 模糊匹配
                output_parts = [f"未精确匹配到'{concept}'"]
                for result in jargon_results:
                    found_content = result.get("content", "").strip()
                    meaning = result.get("meaning", "").strip()
                    if found_content and meaning:
                        output_parts.append(f"找到 '{found_content}' 的含义为：{meaning}")
                results.append("\n".join(output_parts))  # 换行分隔每个jargon解释
                logger.info(f"在jargon库中找到匹配（模糊搜索）: {concept}，找到{len(jargon_results)}条结果")
            else:
                # 精确匹配
                output_parts = []
                for result in jargon_results:
                    meaning = result.get("meaning", "").strip()
                    if meaning:
                        output_parts.append(f"'{concept}' 为黑话或者网络简写，含义为：{meaning}")
                # 换行分隔每个jargon解释
                results.append("\n".join(output_parts) if len(output_parts) > 1 else output_parts[0])
                exact_matches.append(concept)  # 收集精确匹配的概念，稍后统一打印
        else:
            # 未找到，不返回占位信息，只记录日志
            logger.info(f"在jargon库中未找到匹配: {concept}")

    # 合并所有精确匹配的日志
    if exact_matches:
        logger.info(f"找到黑话: {', '.join(exact_matches)}，共找到{len(exact_matches)}条结果")

    if results:
        return "你了解以下词语可能的含义：\n" + "\n".join(results) + "\n"
    return ""
