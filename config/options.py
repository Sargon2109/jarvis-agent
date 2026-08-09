from claude_agent_sdk import ClaudeAgentOptions
from agents.researcher import researcher_agent
from agents.writer import writer_agent

def build_orchestrator_options():
    return ClaudeAgentOptions(
        system_prompt=(
            "You are an orchestrator. Break the user's request into subtasks and "
            "delegate each subtask to the right specialist subagent using the Task tool. "
            "Do not do the work yourself — delegate it."
        ),
        model="claude-sonnet-5",
        allowed_tools=["Task"],
        permission_mode="acceptEdits",
        agents={
            "researcher": researcher_agent,
            "writer": writer_agent,
        },
    )