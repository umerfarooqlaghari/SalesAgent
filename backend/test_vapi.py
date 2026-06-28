import httpx
import json

def test_vapi_custom_llm_integration():
    url = "http://localhost:8008/api/voice/chat/completions"
    
    # Mock Vapi payload structure
    payload = {
        "messages": [
            {
                "role": "user",
                "content": "Hi! Can you tell me what company this is and keep it short?"
            }
        ],
        "call": {
            "id": "integration_test_vapi_call"
        }
    }
    
    print("Sending mock Vapi custom-LLM request to localhost:8008...")
    print(f"Payload: {json.dumps(payload, indent=2)}\n")
    
    try:
        response = httpx.post(url, json=payload, timeout=20.0)
        
        print(f"Response Status Code: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print("Response Payload:")
            print(json.dumps(data, indent=2))
            
            # Verify OpenAI format compatibility
            choices = data.get("choices", [])
            if choices:
                assistant_msg = choices[0].get("message", {}).get("content", "")
                print("\n✅ Integration Test Passed!")
                print(f"Assistant Speaks: '{assistant_msg}'")
                
                # Check for leaks
                if "<thought>" in assistant_msg or "</thought>" in assistant_msg:
                    print("❌ Error: <thought> block leaked into the assistant speech content!")
                elif "SaaSFlow" in assistant_msg:
                    print("❌ Error: Outdated branding 'SaaSFlow' leaked into content!")
                else:
                    print("✅ Verification Check: No thoughts or old branding found. Voice output is clean.")
            else:
                print("❌ Error: Missing choices array in response.")
        else:
            print(f"❌ Server error or failure: {response.text}")
            
    except httpx.ConnectError:
        print("❌ Error: Cannot connect to backend server. Make sure uvicorn is running on http://localhost:8008.")
    except Exception as e:
        print(f"❌ Unexpected Error: {e}")

if __name__ == "__main__":
    test_vapi_custom_llm_integration()
