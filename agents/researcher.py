from claude_agent_sdk import AgentDefinition

researcher_agent = AgentDefinition(
    description="Reads and searches files to gather information. Use for any task that involves understanding existing files or code.",
    prompt="You are a research specialist. Read and search files to answer questions. You do not write or edit anything.",
    tools=["Read", "Glob", "Grep"],
    model="sonnet",
)