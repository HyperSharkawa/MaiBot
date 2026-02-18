from src.chat.utils.prompt_builder import Prompt
# from src.chat.memory_system.memory_activator import MemoryActivator


def init_replyer_prompt():
    Prompt(
        """{knowledge_prompt}{tool_info_block}{extra_info_block}
{expression_habits_block}{memory_retrieval}{jargon_explanation}

你正在qq群里聊天，下面是群里正在聊的内容，其中包含聊天记录和聊天中的图片
其中标注 {bot_name} 的发言是你自己的发言，请注意区分:
{time_block}
{dialogue_prompt}

{reply_target_block}。
{planner_reasoning}
{chat_prompt}
{keywords_reaction_prompt}
现在，你说：""",
        "replyer_prompt_0",
    )

    Prompt(
        """{knowledge_prompt}{tool_info_block}{extra_info_block}
{expression_habits_block}{memory_retrieval}{jargon_explanation}

你正在qq群里聊天，下面是群里正在聊的内容，其中包含聊天记录和聊天中的图片
其中标注 {bot_name} 的发言是你自己的发言，请注意区分:
{time_block}
{dialogue_prompt}

{reply_target_block}。
{planner_reasoning}
{chat_prompt}
{keywords_reaction_prompt}
现在，你说：""",
        "replyer_prompt",
    )

    Prompt(
        """{identity}
{reply_style}""",
        "replyer_prompt_system",
    )
