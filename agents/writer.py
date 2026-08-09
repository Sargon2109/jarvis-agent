from claude_agent_sdk import AgentDefinition

writer_agent = AgentDefinition(
    description="Creates and edits files. Use for any task that involves producing new content or files.",
    prompt="You are a writing specialist. Create clear, well-organized files based on what you're asked to write. Only write inside the 'scratch' folder unless told otherwise.",
    tools=["Write", "Edit", "Read"],
    model="sonnet",
)