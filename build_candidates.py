#!/usr/bin/env python
"""build_candidates.py - turn filter_roles.py's screened list into jobs_candidates.json
(AI / crypto / backend engineering roles in SF, NYC, remote or US-wide)."""
import json, re, sys
sys.stdout.reconfigure(encoding="utf-8")
d = json.load(open("jobs_screened.json", encoding="cp1252", errors="replace"))
GOOD = re.compile(r"\b(ai|ml|machine learning|llm|agent|agents|agentic|applied|solana|blockchain|smart contract|web3|defi|rust|forward deployed|inference|backend|full.?stack|software engineer|platform|infrastructure|member of technical staff|protocol|crypto|trading|solidity)\b", re.I)
ENG = re.compile(r"engineer|developer|member of technical staff", re.I)
BAD = re.compile(r"recruit|operations|planner|architect|specialist|director|manager|sales|designer|analyst|counsel|marketing|partnerships|policy|economist|scientist|fellows|intern|staff engineer|staff software|principal|lead\b|head|tutor|annotat|evaluator|support|security analyst|data center|hardware|firmware|mechanical|electrical|ios|android|mobile|devrel|developer relations|customer success|solutions|accountant|finance|legal|compliance|trust|safety policy|writer|editor|content|reliability|sre\b|devops|qa\b|test engineer|network|silicon|asic|rtl|fpga|gpu architect|compiler|kernel|embedded|advocate|evangelist|community manager|developer experience|technical writer|solutions engineer|sales engineer|customer engineer|support engineer|implementation|professional services|campus|new grad|graduate|early career|university|apprentice|co-?op", re.I)
LOC = re.compile(r"remote|united states|usa|\bus\b|new york|san francisco|nyc|\bsf\b|bay area|^$", re.I)
c = [r for r in d["keep"] if GOOD.search(r[1]) and ENG.search(r[1]) and not BAD.search(r[1]) and LOC.search(r[2] or "")]
json.dump(c, open("jobs_candidates.json", "w", encoding="utf-8"), ensure_ascii=False)
print(len(c), "candidates;", len({r[0] for r in c}), "companies")
