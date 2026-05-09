#!/usr/bin/env python3
"""CLI chat client for pondr — talks to ws://127.0.0.1:8765."""
import asyncio
import json
import sys
import websockets


async def main():
    url = "ws://127.0.0.1:8765"
    print(f"connecting to {url} … (type messages, ctrl-D to quit)")
    try:
        async with websockets.connect(url) as ws:
            async def reader():
                async for msg in ws:
                    print(f"\n← {msg}\n> ", end="", flush=True)

            async def writer():
                loop = asyncio.get_event_loop()
                while True:
                    print("> ", end="", flush=True)
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("/topic "):
                        await ws.send(json.dumps({"type": "add_topic",
                                                   "topic": line[7:]}))
                    elif line == "/status":
                        await ws.send(json.dumps({"type": "status"}))
                    else:
                        await ws.send(json.dumps({"type": "chat", "text": line}))
            await asyncio.gather(reader(), writer())
    except Exception as e:
        print(f"error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
