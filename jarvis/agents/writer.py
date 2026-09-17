"""The writer subagent: produces and edits content."""

from .base import FILE_TOOLS, build_agent

writer_agent = build_agent(
    description=(
        "Creates and edits files. Use for general writing that needs no outside "
        "source material — plans, outlines, notes, code. NOT for coursework: "
        "anything graded against a rubric or based on course material goes to "
        "the canvas specialist, which can actually open those documents."
    ),
    prompt=(
        "You are a writing specialist. Produce clear, well-organized content "
        "based on what you're asked to write.\n\n"
        "You cannot see anything the person who delegated to you read — you get "
        "their instruction and nothing else. So if the task depends on source "
        "material you have not been given a path to, say so and ask for it "
        "rather than inventing what it probably said. A draft written against "
        "a guess at a rubric is worse than no draft, because it looks finished."
    ),
    tools=FILE_TOOLS,
    model="sonnet",
)
