"""The researcher subagent: read-only investigation."""

from claude_agent_sdk import AgentDefinition

researcher_agent = AgentDefinition(
    description=(
        "Reads and searches files to gather information. Use for any subtask that "
        "involves understanding existing files, code, or notes before acting."
    ),
    prompt=(
        "You are a research specialist. Read and search files to answer the "
        "question you're given, then report concise findings. You never write or "
        "edit anything — investigation only."
    ),
    tools=["Read", "Glob", "Grep"],
    model="sonnet",
)
