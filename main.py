import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import query, AssistantMessage, TextBlock, ToolUseBlock
from config.options import build_orchestrator_options

load_dotenv()

async def main():
    options = build_orchestrator_options()
    prompt = input("What do you want the agent to do? ")

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock) and block.name == "Task":
                    print(f"[Delegating to: {block.input.get('subagent_type', '?')}]")

if __name__ == "__main__":
    asyncio.run(main())
