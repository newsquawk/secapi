#!/usr/bin/env python3
"""
Test script for verifying OpenRouter API connectivity, authentication, and
summary generation using the OpenAI Python SDK before integrating into secapi.

Usage:
    # Option 1: Pass API key directly
    env/bin/python scripts/test_openrouter.py --api-key "sk-or-v1-..."

    # Option 2: Use environment variable
    export OPENROUTER_API_KEY="sk-or-v1-..."
    env/bin/python scripts/test_openrouter.py

    # Option 3: Test a different model (e.g., gpt-4o-mini or gemini)
    env/bin/python scripts/test_openrouter.py --model "openai/gpt-4o-mini"
"""

import os
import sys
import time
import asyncio
import argparse
from typing import Optional
from openai import AsyncOpenAI


async def test_openrouter(
    api_key: str,
    model: str = "deepseek/deepseek-v4.1-flash",
    base_url: str = "https://openrouter.ai/api/v1",
):
    print("=" * 65)
    print("🚀 OPENROUTER CONNECTIVITY & COMPLETION TEST")
    print("=" * 65)
    print(f"Base URL : {base_url}")
    print(f"Model    : {model}")
    print(f"API Key  : {api_key[:10]}...{api_key[-4:] if len(api_key) > 14 else '***'}")
    print("-" * 65)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=15.0,
        default_headers={
            "HTTP-Referer": "https://newsquawk.com",
            "X-Title": "Newsquawk SEC API",
        },
    )

    # Realistic financial prompt matching secapi's exact summary logic
    sample_title = "Holdings Changes (Berkshire Hathaway Q2 2026)"
    sample_data = (
        "New Holdings: Occidental Petroleum (10,000,000 shares, $600M). "
        "Increased: Chubb Ltd (+4.5%, 27,000,000 shares). "
        "Decreased: Apple Inc (-389,000,000 shares, down to 400,000,000 shares). "
        "Closed: Paramount Global (0 shares remaining)."
    )

    prompt = f"""
    Generate a summary of the holdings changes for the fund management in one or two sentences.
    {sample_title}: {sample_data}
    """

    print("Sending test request to OpenRouter...")
    start_time = time.perf_counter()

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional financial analyst. Be concise.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
            timeout=15.0,
        )
        duration_ms = (time.perf_counter() - start_time) * 1000

        content = response.choices[0].message.content
        usage = response.usage

        print("\n✅ SUCCESS: Response received!")
        print("-" * 65)
        print(f"Response ({duration_ms:.1f} ms):")
        print(f"\n{content.strip()}\n")
        print("-" * 65)
        print("Usage Statistics:")
        if usage:
            print(f"  • Prompt tokens     : {usage.prompt_tokens}")
            print(f"  • Completion tokens : {usage.completion_tokens}")
            print(f"  • Total tokens      : {usage.total_tokens}")
        print(f"  • Latency           : {duration_ms:.2f} ms")
        print("=" * 65)
        print("🎉 OpenRouter key and model configuration verified successfully!")
        return True

    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        print(f"\n❌ FAILED after {duration_ms:.1f} ms")
        print(f"Error Type: {type(e).__name__}")
        print(f"Error Message:\n  {e}")
        print("-" * 65)

        err_str = str(e).lower()
        if "401" in err_str or "unauthorized" in err_str:
            print("💡 Hint: Check your API key. It may be invalid or expired.")
        elif "404" in err_str or "not found" in err_str or "model" in err_str:
            print(f"💡 Hint: Model '{model}' may not exist on OpenRouter.")
            print("   Try passing --model 'deepseek/deepseek-chat' or 'openai/gpt-4o-mini'")
        elif "402" in err_str or "credits" in err_str or "balance" in err_str:
            print("💡 Hint: Check your OpenRouter credit balance.")
        print("=" * 65)
        return False


def main():
    parser = argparse.ArgumentParser(description="Test OpenRouter API key and model")
    parser.add_argument(
        "--api-key",
        "-k",
        default=os.getenv("OPENROUTER_API_KEY"),
        help="OpenRouter API key (defaults to $OPENROUTER_API_KEY)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=os.getenv("AI_MODEL", "deepseek/deepseek-v4.1-flash"),
        help="Model ID on OpenRouter (default: deepseek/deepseek-v4.1-flash)",
    )
    parser.add_argument(
        "--base-url",
        default="https://openrouter.ai/api/v1",
        help="OpenRouter API base URL (default: https://openrouter.ai/api/v1)",
    )

    args = parser.parse_args()

    api_key = args.api_key
    if not api_key:
        try:
            api_key = input("Enter your OpenRouter API key (sk-or-v1-...): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.")
            sys.exit(1)

    if not api_key:
        print("Error: No API key provided. Exiting.")
        sys.exit(1)

    success = asyncio.run(
        test_openrouter(
            api_key=api_key,
            model=args.model,
            base_url=args.base_url,
        )
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
