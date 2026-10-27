"""Type identifiers for n8n AI nodes shared by the independent runtimes."""

AI_AGENT_TYPE = "@n8n/n8n-nodes-langchain.agent"
DEEPSEEK_MODEL_TYPE = "@n8n/n8n-nodes-langchain.lmChatDeepSeek"
OPENAI_MODEL_TYPE = "@n8n/n8n-nodes-langchain.lmChatOpenAi"
CALCULATOR_TOOL_TYPE = "@n8n/n8n-nodes-langchain.toolCalculator"
SIMPLE_MEMORY_TYPE = "@n8n/n8n-nodes-langchain.memoryBufferWindow"

AI_MODEL_TYPES = frozenset({DEEPSEEK_MODEL_TYPE, OPENAI_MODEL_TYPE})
AI_AUXILIARY_TYPES = frozenset({*AI_MODEL_TYPES, CALCULATOR_TOOL_TYPE, SIMPLE_MEMORY_TYPE})
