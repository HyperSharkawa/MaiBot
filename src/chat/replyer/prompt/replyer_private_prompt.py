from src.chat.utils.prompt_builder import Prompt


def init_replyer_private_prompt():
    Prompt(
        """{knowledge_prompt}{tool_info_block}{extra_info_block}
    {expression_habits_block}{memory_retrieval}{jargon_explanation}

    你正在和{sender_name}聊天，这是你们之前聊的内容:
    {time_block}
    {dialogue_prompt}

    {reply_target_block}。
    {planner_reasoning}
    {chat_prompt}你正在和{sender_name}聊天,现在请你读读之前的聊天记录，然后给出回复。{keywords_reaction_prompt}
    {moderation_prompt}""",
        "private_replyer_prompt",
    )

    Prompt(
        """{knowledge_prompt}{tool_info_block}{extra_info_block}
{expression_habits_block}{memory_retrieval}{jargon_explanation}

你正在和{sender_name}聊天，这是你们之前聊的内容:
{time_block}
{dialogue_prompt}

你现在想补充说明你刚刚自己的发言内容：{target}，原因是{reason}
请你根据聊天内容，组织一条新回复。注意，{target} 是刚刚你自己的发言，你要在这基础上进一步发言，请按照你自己的角度来继续进行回复。注意保持上下文的连贯性。
{chat_prompt}{keywords_reaction_prompt}
{moderation_prompt}""",
        "private_replyer_self_prompt",
    )

    Prompt(
        """{identity}\n{reply_style}""",
        "private_replyer_prompt_system",
    )
