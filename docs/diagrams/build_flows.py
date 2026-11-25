"""Simple, plain-language flow + user-flow diagrams → docs/diagrams/relay-flows.drawio

Complements the detailed technical diagrams (relay.drawio). Run: python build_flows.py
"""

from pathlib import Path

from _drawio import Document

d = Document()

d.page("1. A task end to end").flow(
    "What an agent task looks like end to end",
    [("trig", "Something happens", "issue, PR, Slack message", 0, 1, "actor"),
     ("pick", "Pick the agent", "coding, review, support…", 1, 1),
     ("plan", "Plan", "steps + budget", 2, 1),
     ("work", "Work in a sandbox", "read, edit, run tests", 3, 1),
     ("risky", "Risky step?", "", 4, 1, "ask"),
     ("ask", "Ask a human", "Slack approve / reject", 4, 2.6),
     ("do", "Do it", "exactly once", 5, 1),
     ("report", "Report result", "PR, comment, ticket", 6, 1, "good")],
    [("trig", "pick"), ("pick", "plan"), ("plan", "work"), ("work", "risky"), ("risky", "do", "no"),
     ("risky", "ask", "yes"), ("ask", "do", "approved"), ("ask", "plan", "rejected"), ("do", "work", "next step"),
     ("do", "report", "done")],
    note="If the agent crashes it resumes from the last step; if it runs out of budget it returns what it has.")

d.page("2. User flow: code review").flow(
    "Code review agent",
    [("pr", "Open a pull request", "", 0, 1, "actor"),
     ("tools", "Run linters + scanners", "fast, certain checks first", 1, 1),
     ("ai", "AI review", "bugs, security, performance", 2, 1),
     ("verify", "Prove each finding", "", 3, 1, "ask"),
     ("drop", "Drop it", "no evidence = no comment", 3, 2.6, "end"),
     ("post", "Post comments", "on the exact lines", 4, 1),
     ("dev", "Developer reacts", "fixes, 👍 or 👎", 5, 1),
     ("learn", "Track precision", "noisy rules get tuned", 6, 1, "good")],
    [("pr", "tools"), ("tools", "ai"), ("ai", "verify"), ("verify", "drop", "weak"), ("verify", "post", "proven"),
     ("post", "dev"), ("dev", "learn")])

d.page("3. User flow: fix an issue").flow(
    "Coding agent: from issue to pull request",
    [("label", "Label the issue", "relay:fix", 0, 1, "actor"),
     ("plan", "Agent plans", "", 1, 1),
     ("ok", "Approve plan?", "optional", 2, 1, "ask"),
     ("loop", "Edit + test", "until tests pass", 3, 1),
     ("self", "Self-review", "uses the review agent", 4, 1),
     ("pr", "Draft PR", "with test evidence + cost", 5, 1),
     ("human", "Human merges", "agent can never merge", 6, 1, "good")],
    [("label", "plan"), ("plan", "ok"), ("ok", "loop", "yes"), ("ok", "plan", "change it"), ("loop", "self"),
     ("self", "loop", "issues found"), ("self", "pr", "clean"), ("pr", "human")])

d.page("4. User flow: support").flow(
    "Support agent",
    [("cust", "Customer asks", "Slack or website", 0, 1, "actor"),
     ("kind", "What kind?", "", 1, 1, "ask"),
     ("search", "Search the docs", "via Atlas", 2, 0.5),
     ("conf", "Confident?", "", 3, 0.5, "ask"),
     ("reply", "Reply with sources", "", 4, 0, "good"),
     ("draft", "Draft for a human", "approve or edit in Slack", 4, 1.3),
     ("bug", "Create ticket", "Jira, with details", 2, 2.2),
     ("esc", "Hand to a human", "account / billing", 1, 2.6, "end")],
    [("cust", "kind"), ("kind", "search", "question"), ("kind", "bug", "bug"), ("kind", "esc", "account"),
     ("search", "conf"), ("conf", "reply", "yes"), ("conf", "draft", "no"), ("draft", "reply", "approved")])

d.page("5. User flow: meeting notes").flow(
    "Meeting intelligence",
    [("rec", "Upload recording", "", 0, 1, "actor"),
     ("tx", "Transcribe", "who said what", 1, 1),
     ("ext", "Pull out", "decisions, action items, owners", 2, 1),
     ("check", "Review in Slack", "edit before anything is sent", 3, 1),
     ("jira", "Jira tasks", "", 4, 0, "good"),
     ("notion", "Notion notes", "", 4, 1, "good"),
     ("slack", "Slack recap", "", 4, 2, "good")],
    [("rec", "tx"), ("tx", "ext"), ("ext", "check"), ("check", "jira", "approved"), ("check", "notion"),
     ("check", "slack")])

d.page("6. Workflow automation").flow(
    "Automations: rules first, AI only where needed",
    [("ev", "Event", "PR merged", 0, 1, "actor"),
     ("issues", "Find linked tickets", "plain rule", 1, 1),
     ("note", "Write release note", "AI step", 2, 1),
     ("appr", "Needs approval?", "", 3, 1, "ask"),
     ("human", "Approve in Slack", "", 3, 2.6),
     ("jira", "Close tickets", "plain rule", 4, 1),
     ("post", "Post to #releases", "", 5, 1, "good")],
    [("ev", "issues"), ("issues", "note"), ("note", "appr"), ("appr", "jira", "no"), ("appr", "human", "yes"),
     ("human", "jira"), ("jira", "post")])

d.page("7. Safety check on every action").flow(
    "Every tool call goes through the same safety check",
    [("want", "Agent wants to act", "e.g. post a comment", 0, 1, "actor"),
     ("budget", "Budget left?", "", 1, 1, "ask"),
     ("partial", "Stop with partial result", "", 1, 2.6, "end"),
     ("kind", "What kind of action?", "", 2, 1, "ask"),
     ("read", "Read-only", "allowed", 3, 0, "good"),
     ("ext", "Writes outside", "after reading untrusted text?", 3, 1.3),
     ("block", "Dangerous", "merge, delete → blocked", 3, 2.6, "end"),
     ("human", "Human approval", "", 4, 1.3),
     ("run", "Run once", "logged for audit", 5, 1, "good")],
    [("want", "budget"), ("budget", "partial", "no"), ("budget", "kind", "yes"), ("kind", "read"),
     ("kind", "ext"), ("kind", "block"), ("ext", "human", "yes"), ("ext", "run", "no"), ("read", "run"),
     ("human", "run", "approved")])

print(f"wrote {d.save(Path(__file__).with_name('relay-flows.drawio'))}")
