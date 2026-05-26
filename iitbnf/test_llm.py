# test_llm.py  — run with: python test_llm.py
import sys
sys.path.insert(0, '.')
from app import app

with app.app_context():
    from llm import llm_generate, llm_stream

    print("Testing generate()...")
    result = llm_generate(
        "Facts:\n  name = Dr. A Sharma\n  attendance_pct = 88.5\n"
        "  days_present = 212\n  working_days = 239\n\n"
        "Question: What is the attendance?\nAnswer:",
        max_tokens=80
    )
    print(f"Result: {result}\n")

    print("Testing stream()...")
    tokens = []
    for token in llm_stream("Say: ready", max_tokens=10):
        tokens.append(token)
    print(f"Stream: {''.join(tokens)}")