"""The writer subagent: produces and edits content."""

from claude_agent_sdk import AgentDefinition

writer_agent = AgentDefinition(
    description=(
        "Creates and edits files. Use for any subtask that produces new content — "
        "drafts, documents, or code."
    ),
    prompt=(
        "You are a writing specialist. Produce clear, well-organized content based "
        "on what you're asked to write. Unless told otherwise, only create or edit "
        "files inside the 'scratch' folder."
    ),
    tools=["Write", "Edit", "Read"],
    model="sonnet",
)
