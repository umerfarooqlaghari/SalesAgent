import asyncio
import os
import sys

# Ensure we can import from backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agent.graph import get_agent_graph
from backend.database import db_client, save_lead, get_lead, seed_default_api_key, validate_api_key_in_db
from langchain_core.messages import HumanMessage

async def main():
    print("Testing B2B Sales SDR Agent Extension Features (v2)...")
    
    # Connect and seed
    db_client.connect()
    print("MongoDB Atlas & SQLite POS database initialized successfully.")
    
    await seed_default_api_key()
    print("Seeded test API keys in MongoDB.")
    
    # Verify API key validation helper
    is_valid = await validate_api_key_in_db("test_key_abc123")
    print(f"API Key validation check for 'test_key_abc123': {is_valid}")
    
    assert is_valid == True, "API Key validation failed!"

    # Get compiled graph
    graph = await get_agent_graph()
    print("LangGraph SDR Agent compiled successfully.")

    # 1. Test case: Querying SaaS Packages (Inventory query RAG)
    thread_id = "test_verification_thread_v2"
    tenant_id = "alpha_default"
    config = {"configurable": {"thread_id": thread_id, "tenant_id": tenant_id}}
    inputs = {
        "messages": [HumanMessage(content="Hello! What packages do you sell? I want to know pricing and stock details.")],
        "thread_id": thread_id,
        "tenant_id": tenant_id,
    }
    
    print("\n--- TEST CASE 1: Querying Inventory/Packages (POS SQL Tool) ---")
    async for event in graph.astream_events(inputs, config=config, version="v2"):
        kind = event["event"]
        name = event["name"]
        
        if kind == "on_node_start":
            print(f"\n[Node] {name}")
        elif kind == "on_chat_model_stream":
            token = event["data"]["chunk"].content
            if token:
                print(token, end="", flush=True)
        elif kind == "on_tool_start":
            print(f"\n[Tool Call] {name} inputs: {event['data'].get('input')}")
        elif kind == "on_tool_end":
            print(f"\n[Tool Output] {name} output: {str(event['data'].get('output'))[:200]}...")

    # 2. Test case: Checking Order status (Secure POS Query)
    inputs_order = {
        "messages": [HumanMessage(content="Can you verify the status of order #1001 for cto@cloudgrid.io?")],
        "thread_id": thread_id,
        "tenant_id": tenant_id,
    }
    print("\n\n--- TEST CASE 2: Order Status Check (SQL Parameterized Verify) ---")
    async for event in graph.astream_events(inputs_order, config=config, version="v2"):
        kind = event["event"]
        name = event["name"]
        
        if kind == "on_node_start":
            print(f"\n[Node] {name}")
        elif kind == "on_chat_model_stream":
            token = event["data"]["chunk"].content
            if token:
                print(token, end="", flush=True)
        elif kind == "on_tool_start":
            print(f"\n[Tool Call] {name} inputs: {event['data'].get('input')}")
        elif kind == "on_tool_end":
            print(f"\n[Tool Output] {name} output: {event['data'].get('output')}")

    print("\n\nTesting complete! Disconnecting...")
    db_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
