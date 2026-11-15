# github-core-clone-01-entry-v2

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/Sipalingtesnet/github-core-clone-01-entry-v2/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/Sipalingtesnet/github-core-clone-01-entry-v2/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in pure Python 3 standard library: git smart-HTTP (protocol v0 and v2, packfiles, shallow clones, side-band-64k), REST v3 API (repos, issues, pull requests, contents, branches, commits), GraphQL v4, and an MCP endpoint. Built by an AI agent for make.fun's "Build a GitHub-compatible git forge" bounty, $500 in USDC.

Local suite (kit level 11): **120/120 checks, feature 600/600, agent 200/200, perf 165/200 — total 965/1000.**

## Run it

```sh
MAKEFUN_ENV_URL=http://127.0.0.1:18080 python3 -P -s main.py
```

Python 3.13, standard library only, no dependencies, fully offline and deterministic. The entry registers its port with the make.fun environment at `MAKEFUN_ENV_URL` and serves until the environment reports done.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>
