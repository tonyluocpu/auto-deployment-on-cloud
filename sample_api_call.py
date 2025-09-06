import os
import requests

# Option 1: safer (use export OPENROUTER_API_KEY="yourkey")
API_KEY = os.getenv("OPENROUTER_API_KEY")

# Option 2: testing only
# API_KEY = "sk-or-v1-5427..."  

if not API_KEY:
    raise ValueError("No OpenRouter API key found. Set OPENROUTER_API_KEY in your environment.")

API_URL = "https://openrouter.ai/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "HTTP-Referer": "http://localhost",  # required by OpenRouter
    "X-Title": "LLM Test App"
}

def ask_llm(question: str) -> str:
    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": question}
        ]
    }
    response = requests.post(API_URL, headers=headers, json=payload)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]

if __name__ == "__main__":
    while True:
        user_input = input("Ask me anything (or type 'quit' to exit): ")
        if user_input.lower() in ["quit", "exit"]:
            break
        print("Answer:", ask_llm(user_input))
