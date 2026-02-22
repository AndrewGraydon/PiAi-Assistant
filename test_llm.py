#!/usr/bin/env python3
"""
Direct LLM API test — bypasses orchestrator entirely.
Tests the Qwen3-4B binary on port 8000.
IMPORTANT: Does NOT call /api/reset — the binary crashes on second reset.

Usage:
    # Stop orchestrator first so it doesn't compete
    ssh andrew@10.10.0.129 "sudo systemctl stop piAi-orchestrator"
    # Then run
    ssh andrew@10.10.0.129 "cd ~/PiAi-Assistant && python3 test_llm.py"
"""

import sys
import time
import requests

LLM_HOST = "http://localhost:8000"
POLL_INTERVAL = 0.5
MAX_POLL_S = 30


def wait_for_idle(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{LLM_HOST}/api/generate_provider", timeout=3)
            if r.json().get("done"):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def generate(prompt, temperature=0.7, top_k=40):
    if not wait_for_idle(timeout=10):
        print("  ERROR: LLM not idle")
        return None

    try:
        r = requests.post(
            f"{LLM_HOST}/api/generate",
            json={"prompt": prompt, "temperature": temperature, "top-k": top_k},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  ERROR: generate status {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None

    accumulated = ""
    start = time.time()
    while time.time() - start < MAX_POLL_S:
        time.sleep(POLL_INTERVAL)
        try:
            r = requests.get(f"{LLM_HOST}/api/generate_provider", timeout=5)
            data = r.json()
        except Exception:
            continue

        chunk = data.get("response", "")
        if chunk:
            accumulated += chunk
        if data.get("done"):
            break

    elapsed = time.time() - start
    return {"response": accumulated, "elapsed_s": round(elapsed, 1)}


def analyze(result):
    if result is None:
        return

    resp = result["response"]
    elapsed = result["elapsed_s"]
    if not resp:
        print(f"  EMPTY response after {elapsed}s")
        return

    ascii_ratio = sum(1 for c in resp if ord(c) < 128) / len(resp)
    has_chinese = any("\u4e00" <= c <= "\u9fff" for c in resp)

    words = resp.split()
    unique_ratio = len(set(words)) / max(len(words), 1)

    preview = resp[:500].replace("\n", "\\n")
    if len(resp) > 500:
        preview += "..."

    print(f"  {elapsed}s | {len(resp)} chars | {int(ascii_ratio*100)}% ASCII | {int(unique_ratio*100)}% unique words")
    print(f"  >>> {preview}")

    issues = []
    if has_chinese:
        issues.append("CHINESE")
    if ascii_ratio < 0.7:
        issues.append("GARBLED")
    if unique_ratio < 0.3:
        issues.append("REPETITIVE")
    if elapsed > 20:
        issues.append("SLOW")

    if issues:
        print(f"  ISSUES: {', '.join(issues)}")
    else:
        print(f"  OK")


def main():
    print("=" * 60)
    print("LLM API Test (NO reset — generate only)")
    print("=" * 60)

    # Wait for LLM to be ready
    print("\n[Waiting for LLM...]")
    if not wait_for_idle(timeout=15):
        print("  LLM not ready after 15s")
        sys.exit(1)
    print("  LLM is idle and ready")

    # Test: single generate, no reset
    prompts = [
        "Hello",
        "What is 2 plus 2?",
        "What color is the sky?",
    ]

    for i, prompt in enumerate(prompts):
        print(f"\n[Test {i+1}] {repr(prompt)}")
        result = generate(prompt)
        analyze(result)

        if result is None:
            print("  Stopping — LLM appears crashed")
            break

        # Check if LLM is still alive
        time.sleep(1)
        try:
            r = requests.get(f"{LLM_HOST}/api/generate_provider", timeout=3)
            if not r.json().get("done"):
                print("  WARNING: LLM still generating, waiting...")
                wait_for_idle(timeout=10)
        except Exception:
            print("  LLM crashed — stopping tests")
            break

    print("\n" + "=" * 60)
    print("Done")
    print("=" * 60)


if __name__ == "__main__":
    main()
