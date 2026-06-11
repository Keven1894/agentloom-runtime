# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a Vulnerability

If you discover a security issue in AgentLoom Runtime, please **do not** open a
public GitHub issue with exploit details.

Instead, email **bguan@fiu.edu** with:

- A description of the vulnerability
- Steps to reproduce (if applicable)
- Any suggested fix or mitigation

We aim to acknowledge reports within 5 business days.

## Scope

This library handles database connections and optional OpenAI embedding calls.
Deployers are responsible for:

- Securing `AGENTLOOM_DB_*` / `DATABASE_URL` credentials
- Securing `OPENAI_API_KEY` when using vector search
- Running MySQL with appropriate network and access controls

The OSS pre-release scanner (`scripts/oss_release/scan_internal_info.py`) runs in
CI to reduce the risk of accidental internal-data leaks in public commits.
