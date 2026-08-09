import asyncio
from dotenv import load_dotenv
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock

load_dotenv()

async def main():
    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a helpful assistant that can read, search, write, and edit files. "
            "Only create or modify files inside the 'scratch' folder unless told otherwise."
        ),
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit"],
        model="claude-sonnet-5",
    )

    prompt = input("What do you want the agent to do? ")

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)

if __name__ == "__main__":
    asyncio.run(main())