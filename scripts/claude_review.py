#!/usr/bin/env python3
"""Call the Anthropic API to review the PR diff. Invoked from .github/workflows/claude.yml."""
import json
import os
import subprocess
import sys


repo = os.environ['REPO']
pr_number = os.environ.get('PR_NUMBER', '0')
review_prompt = os.environ.get('REVIEW_PROMPT', '').strip()


def read_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


system_prompt = (
    'You are a senior software engineer and code reviewer. '
    'Review the patch for correctness, security, maintainability, and testability. '
    'Be precise, concrete, and actionable. Do not invent facts. '
    'If there are no issues, say so clearly. '
    'Prioritize real risk over stylistic preferences.'
)

user_message = (
    f"Repository: {repo}\n"
    f"PR number: {pr_number if pr_number and pr_number != '0' else 'manual run'}\n\n"
    f"Instruction: {review_prompt}\n\n"
    "Review the following diff and answer in Russian unless the user explicitly asks otherwise.\n\n"
    "Provide: 1) Key findings, 2) Risks, 3) Suggested fixes.\n\n"
    "Diff:\n```diff\n"
    f"{read_file('/tmp/claude.diff')}\n"
    "```"
)

payload = {
    "model": os.environ['ANTHROPIC_MODEL'],
    "max_tokens": 1800,
    "system": system_prompt,
    "messages": [
        {"role": "user", "content": user_message}
    ],
}

proc = subprocess.run(
    [
        'curl', '-sS',
        'https://api.anthropic.com/v1/messages',
        '-H', 'x-api-key: ' + os.environ['ANTHROPIC_API_KEY'],
        '-H', 'anthropic-version: 2023-06-01',
        '-H', 'content-type: application/json',
        '--data-binary', json.dumps(payload),
    ],
    capture_output=True,
    text=True,
)

if proc.returncode != 0:
    print(proc.stderr, file=sys.stderr)
    raise SystemExit(proc.returncode)

raw = proc.stdout.strip()
print(raw)

try:
    data = json.loads(raw)
    result = data['content'][0]['text']
except Exception as exc:
    print(f'Failed to parse response from Anthropic: {exc}', file=sys.stderr)
    print(raw, file=sys.stderr)
    raise

with open('/tmp/claude_response.txt', 'w', encoding='utf-8') as f:
    f.write(result)

with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as out:
    out.write(f"response<<EOF\n{result}\nEOF\n")
