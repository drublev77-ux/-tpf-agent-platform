#!/usr/bin/env python3
"""Post the Claude review comment to the PR. Invoked from .github/workflows/claude.yml."""
import json
import os
import urllib.request

with open('/tmp/claude_response.txt', 'r', encoding='utf-8') as f:
    text = f.read().strip()

pr_number = os.environ['PR_NUMBER']
repo = os.environ['REPO']
token = os.environ['GITHUB_TOKEN']

payload = json.dumps({"body": "### Claude review\n\n" + text}).encode('utf-8')

req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
    data=payload,
    method='POST',
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    },
)

with urllib.request.urlopen(req) as resp:
    print(f"Comment posted: HTTP {resp.status}")
