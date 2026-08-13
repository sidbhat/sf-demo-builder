"""Demo content generation tools (agent cards and scripts)."""

import json
from auth import _build_sf_config
from scenarios import SCENARIO_KB


def _wrap(text: str, width: int) -> list[str]:
    """Simple word-wrap for card display."""
    words = text.split()
    lines, line = [], []
    for w in words:
        if sum(len(x)+1 for x in line) + len(w) > width:
            lines.append(" ".join(line))
            line = [w]
        else:
            line.append(w)
    if line:
        lines.append(" ".join(line))
    return lines


def generate_agent_card(plan_json: str) -> str:
    """
    Generate a Joule Agent Hub-style card from a design_demo_org() plan.

    Takes the JSON output of design_demo_org() and returns a card object
    ready for display or sharing — with title, challenge statement,
    3 sample prompts calibrated to what's actually live, capability summary,
    and a direct Joule login URL.

    Use this after design_demo_org() but before provision_demo_org() to
    preview the demo narrative, or after provisioning to share the card
    with the sales team.

    Args:
        plan_json: The full JSON string returned by design_demo_org()
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    card = plan.get("agent_card", {})
    live  = plan.get("live_data", [])
    story = plan.get("story_data", [])
    employees = plan.get("employees", [])
    instance_code = plan.get("sf_instance", plan.get("company_code", "SFSALES011375"))
    script_login_url = plan.get("login_url", "https://hcm-us10-sales.hr.cloud.sap/login?company=SFSALES011375")

    # Build persona table — who to log in as for the demo
    personas = []
    for e in employees[:3]:
        personas.append({
            "name":     f"{e['firstName']} {e['lastName']}",
            "title":    e["jobTitle"],
            "username": e["username"],
            "grade":    e["payGrade"],
            "login":    f"{e['username']}@{instance_code}",
        })

    # Story data framing — what the AE says when Joule can't show it
    story_framing = []
    for item in story:
        story_framing.append(
            f"  '{item['entity']}' — {item['description']}"
        )

    output = {
        "card": {
            "title":     card.get("title", ""),
            "challenge": card.get("challenge", ""),
            "prompts":   card.get("prompts", []),
            "joule_url": script_login_url,
        },
        "demo_context": {
            "company":   plan.get("company_name"),
            "industry":  plan.get("industry"),
            "country":   plan.get("country"),
            "scenario":  plan.get("scenario_label"),
            "employees": len(employees),
            "code":      plan.get("company_code"),
        },
        "live_data_summary": [
            f"{i['status']} {i['entity']}: {i['description']}"
            for i in live
        ],
        "story_framing": (
            "When Joule can't surface these directly, use this framing:\n"
            + ("\n".join(story_framing) if story_framing
               else "  (no story data — everything in this scenario is live)")
        ),
        "suggested_personas": personas,
        "ready_to_demo": len(story) == 0,
        "display": (
            f"┌{'─'*62}┐\n"
            f"│ {card.get('title','')[:60]:<60} │\n"
            f"├{'─'*62}┤\n"
            + "\n".join(
                f"│ {line:<60} │"
                for line in _wrap(card.get("challenge", ""), 60)
            ) + "\n"
            f"├{'─'*62}┤\n"
            f"│ {'Sample prompts:':<60} │\n"
            + "\n".join(
                f"│   {'↳ ' if i else '• '}{_wrap(p, 57)[0]:<57} │"
                + ("\n" + "\n".join(f"│     {line:<57} │" for line in _wrap(p, 57)[1:]) if len(_wrap(p, 57)) > 1 else "")
                for i, p in enumerate(card.get("prompts", []))
            ) + "\n"
            f"├{'─'*62}┤\n"
            + "\n".join(
                f"│ {line:<60} │"
                for line in _wrap("LIVE: " + ", ".join(i["entity"] for i in live), 60)
            ) + "\n"
            + ("\n".join(
                f"│ {line:<60} │"
                for line in _wrap("STORY: " + ", ".join(i["entity"] for i in story), 60)
            ) + "\n" if story else "")
            + f"│ {'Login: ' + script_login_url[:53]:<60} │\n"
            f"└{'─'*62}┘"
        ),
    }
    return json.dumps(output, indent=2)


def generate_demo_script(plan_json: str) -> str:
    """
    Generate a two-surface demo script from a design_demo_org() plan.

    Produces a runnable demo guide with:
    - SURFACE 1: Joule Chat — 3 beats with exact prompts and what Joule shows
    - SURFACE 2: Joule Desktop / Claude Code — 3 beats with agent instructions,
      which SF OData calls the agent makes, what it produces, and the AE bridge line

    Each beat is honest about what's live vs what's narrative framing.

    Use after design_demo_org() (or provision_demo_org()) to hand the AE
    a complete script they can run without further prep.

    Args:
        plan_json: The full JSON string returned by design_demo_org()
    """
    try:
        plan = json.loads(plan_json)
    except Exception as e:
        return json.dumps({"error": f"Invalid plan JSON: {e}"})

    problem_key = plan.get("business_problem", "")
    scenario    = SCENARIO_KB.get(problem_key, {})
    demo_story  = scenario.get("demo_story", {})
    company     = plan.get("company_name", "the company")
    employees   = plan.get("employees", [])
    password    = plan.get("password", "")
    instance_code = plan.get("sf_instance", plan.get("company_code", "SFSALES011375"))
    script_login_url = plan.get("login_url", "https://hcm-us10-sales.hr.cloud.sap/login?company=SFSALES011375")

    if not demo_story:
        return json.dumps({
            "error": f"No demo_story defined for scenario '{problem_key}'."
        })

    # Build persona reference (first 3 employees)
    personas = []
    for e in employees[:3]:
        personas.append(
            f"{e['firstName']} {e['lastName']} ({e['jobTitle']}, {e['payGrade']}) "
            f"— login: {e['username']}@{instance_code} / {password}"
        )

    # Format Joule Chat beats
    chat_beats = demo_story.get("joule_chat", [])
    chat_lines = []
    for i, beat in enumerate(chat_beats, 1):
        chat_lines.append(
            f"  Beat {i}: {beat['beat']}\n"
            f"  ─────────────────────────────────────────\n"
            f"  Login as: {personas[min(i-1, len(personas)-1)]}\n"
            f"  Prompt:   \"{beat['prompt']}\"\n"
            f"  Shows:    {beat['what_joule_shows']}\n"
            f"  AE says:  \"{beat['ae_bridge']}\""
        )

    # Format Joule Desktop beats
    desktop_beats = demo_story.get("joule_desktop", [])
    desktop_lines = []
    for i, beat in enumerate(desktop_beats, 1):
        mcp_calls = "; ".join(beat.get("mcp_calls", []))
        desktop_lines.append(
            f"  Beat {i}: {beat['beat']}\n"
            f"  ─────────────────────────────────────────\n"
            f"  Instruction: \"{beat['agent_instruction']}\"\n"
            f"  MCP reads:   {mcp_calls}\n"
            f"  Produces:    {beat['what_it_produces']}\n"
            f"  AE says:     \"{beat['ae_bridge']}\""
        )

    # Story bridge — what AE says for narrative items
    story_items = plan.get("story_data", [])
    story_bridge = (
        scenario.get("story_narrative", "")
        if story_items
        else "All data in this scenario is live — no narrative bridge needed."
    )

    script_text = (
        f"{'='*70}\n"
        f"  DEMO SCRIPT: {company} — {scenario.get('label','')}\n"
        f"  Instance: {instance_code}  |  Password: {password}\n"
        f"{'='*70}\n\n"
        f"PERSONAS (log in as these users):\n"
        + "\n".join(f"  • {p}" for p in personas) + "\n\n"
        f"{'─'*70}\n"
        f"SURFACE 1 — JOULE CHAT (in-app assistant)\n"
        f"  Show the AE typing these prompts directly in the SF Joule sidebar.\n"
        f"  Everything Joule answers here is grounded in live data.\n"
        f"{'─'*70}\n\n"
        + "\n\n".join(chat_lines) + "\n\n"
        f"{'─'*70}\n"
        f"SURFACE 2 — JOULE DESKTOP / CLAUDE CODE (agentic tier)\n"
        f"  Switch to Joule Desktop or Claude Code with the sf-demo-builder MCP.\n"
        f"  The agent reads live SF data via MCP and synthesises it into outputs\n"
        f"  no chatbot response can match.\n"
        f"{'─'*70}\n\n"
        + "\n\n".join(desktop_lines) + "\n\n"
        f"{'─'*70}\n"
        f"STORY BRIDGE (what the AE says when Joule can't show it live)\n"
        f"{'─'*70}\n"
        f"  {story_bridge}\n\n"
        f"{'='*70}\n"
        f"  End of script. Total live entities: {len(plan.get('live_data',[]))}\n"
        f"  Story entities (narrative only): {len(story_items)}\n"
        f"{'='*70}\n"
    )

    return json.dumps({
        "script":   script_text,
        "personas": personas,
        "joule_chat_beats":    [b["beat"] for b in chat_beats],
        "joule_desktop_beats": [b["beat"] for b in desktop_beats],
        "story_bridge": story_bridge,
        "login_url": script_login_url,
    }, indent=2)


def register(mcp):
    """Register content tools with mcp instance."""
    mcp.tool()(generate_agent_card)
    mcp.tool()(generate_demo_script)
