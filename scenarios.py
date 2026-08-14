"""
Demo scenario definitions and configuration data.
Static lookup tables for industries, roles, locales, goals, and salary.
"""

# ── Scenario knowledge base ───────────────────────────────────────────────────
# See original server.py lines 362-1096 for full SCENARIO_KB content
# ── Scenario knowledge base ───────────────────────────────────────────────────

SCENARIO_KB = {
    "mass_hiring": {
        "label": "Mass Hiring / High-Volume Recruiting",
        "org_bias": "recruiting_heavy",
        "talent_story": "Several positions open simultaneously; hiring managers overwhelmed; structured pipeline needed.",
        "agent_card": {
            "title": "New Hire Readiness Assistant",
            "challenge": (
                "A new hire is joining next month and no one has visibility into who their manager is, "
                "which team they're landing in, or what their first 30 days look like. "
                "Hiring is moving fast but the handoff to HR operations is broken."
            ),
            "prompts": [
                "Who is starting next month and who is their manager?",
                "What team is the new hire joining and what does the org structure look like?",
                "Show me everyone who joined in the last 90 days and their current manager.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Who's arriving and who owns them",
                    "prompt": "Who is starting next month and who is their manager?",
                    "live_data": ["onboardee", "employees"],
                    "what_joule_shows": "Sam Rivera, Senior Associate, reporting to the VP Ops. Start date Nov 3. Position P-{code}-001 assigned.",
                    "ae_bridge": "This is real data — provisioned minutes ago. Joule read it directly from EC. No mock data, no rehearsed answer.",
                },
                {
                    "beat": "Team context the manager needs",
                    "prompt": "What does the team Sam is joining look like? Who are the key people?",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Org chart of the VP Ops' direct reports, their titles, grades, and tenure.",
                    "ae_bridge": "The manager can prep for the first 1:1 using exactly this — real headcount, real reporting lines.",
                },
                {
                    "beat": "Setting expectations on day one",
                    "prompt": "What are the team's active goals right now?",
                    "live_data": ["goal_assignments"],
                    "what_joule_shows": "Current annual goals for each team member — what they're working toward when Sam arrives.",
                    "ae_bridge": "Sam walks in knowing the team's priorities, not guessing. This is the difference between a good and a great onboarding.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Pull the full onboarding context in one shot",
                    "agent_instruction": "Read Sam Rivera's employment record from SF, their manager's direct reports, and the team's current annual goals. Summarise what the manager needs to know before the new hire's first day.",
                    "mcp_calls": ["SF OData: EmpJob for onboardee, User list under manager, Goal_11 for team"],
                    "what_it_produces": "A structured briefing: who Sam reports to, the 4 teammates, their titles and grades, and 2-3 sentences on each person's active goal focus — synthesised from live data.",
                    "ae_bridge": "This is what the agent tier unlocks. Joule chat answered one question at a time. The desktop agent read across three entities, connected them, and wrote the manager's prep note.",
                },
                {
                    "beat": "Spot the talent risk in the receiving team",
                    "agent_instruction": "For the team Sam Rivera is joining, surface anyone flagged as high flight risk or high impact of loss. Are there gaps that a new hire could be positioned to fill?",
                    "mcp_calls": ["SF OData: talent profiles for manager's direct reports"],
                    "what_it_produces": "Table of team members, their impactOfLoss / riskOfLoss flags, and a plain-language read: 'Two members are high impact / medium risk — Sam's onboarding should prioritise knowledge transfer with them early.'",
                    "ae_bridge": "The agent connected two signals — who's arriving and who's at risk — to give the manager advice, not just data.",
                },
                {
                    "beat": "Draft the 30-day plan from live priorities",
                    "agent_instruction": "Using the team's active goals and the onboardee's role, draft a 30-60-90 day onboarding plan for Sam Rivera. Anchor each phase to a real team goal or a live team member.",
                    "mcp_calls": ["SF OData: Goal_11 for team, DevGoal_2001 for team, EmpJob for onboardee"],
                    "what_it_produces": "A structured 30-60-90 plan with named colleagues, real goal references, and suggested first contributions — all grounded in the SF data just read.",
                    "ae_bridge": "Not a template. Not a generic plan. An actual draft, anchored to the live org data we just provisioned.",
                },
            ],
        },
        "joule_prompts": [
            "Who is starting next month and who is their manager?",
            "What team is the new hire joining and what does the org structure look like?",
            "Show me everyone who joined in the last 90 days and their current manager.",
            "Which new hires don't have a position assigned yet?",
            "Draft a welcome message for our incoming Senior Associate.",
        ],
        "live_data": ["org_structure", "employees", "onboardee", "talent_profiles", "spot_awards"],
        "story_data": ["job_requisitions", "candidate_pipeline", "offer_letters", "interview_schedules"],
        "story_narrative": (
            "The onboardee Sam Rivera (Senior Associate, Nov 3 start) is live in SF with a manager and position assigned. "
            "Joule can surface their start date, reporting line, and team context from real data. "
            "Open reqs, candidate pipeline, and offer letters are narrative — "
            "Recruiting module setup (req templates, candidate records) is not provisioned."
        ),
    },
    "compensation_planning": {
        "label": "Compensation Planning & Pay Equity",
        "org_bias": "standard",
        "talent_story": "Annual comp cycle opens in two weeks; managers need to propose merit increases; budget constraints and outliers need to be visible.",
        "agent_card": {
            "title": "Compensation Review Assistant",
            "challenge": (
                "The annual comp cycle opens in two weeks and managers don't know who on their team "
                "is below midpoint, who hasn't had a raise in over a year, or how their team's total "
                "comp compares to budget. Decisions are being made blind."
            ),
            "prompts": [
                "Who on my team hasn't had a salary increase in the last 12 months?",
                "Show me anyone below the midpoint for their pay grade.",
                "Compare my top performer's salary progression to their peers.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Find who hasn't moved in over a year",
                    "prompt": "Who on my team hasn't had a salary increase in the last 12 months?",
                    "live_data": ["salary_history", "employees"],
                    "what_joule_shows": "List of employees with last pay change date, current base, and months since last increase.",
                    "ae_bridge": "Three years of real comp history in SF. Joule read it directly — no spreadsheet export, no Finance request.",
                },
                {
                    "beat": "Surface the below-midpoint risk",
                    "prompt": "Show me anyone below the midpoint for their pay grade.",
                    "live_data": ["salary_history", "talent_profiles"],
                    "what_joule_shows": "Employees at bottom of grade band, with their impactOfLoss flag next to it.",
                    "ae_bridge": "This is the dangerous combination — low pay AND high impact. That's your retention risk hiding in the comp data.",
                },
                {
                    "beat": "Progression comparison for the merit conversation",
                    "prompt": "Show me the salary progression for our VP Engineering over the last 3 years compared to peers at the same grade.",
                    "live_data": ["salary_history"],
                    "what_joule_shows": "Year-over-year base salary for VP Eng vs average at GR-14, showing relative position.",
                    "ae_bridge": "The manager walks into the merit conversation with this. No surprises, no guesswork.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Full comp risk scan across the team",
                    "agent_instruction": "Read salary history for all employees. Cross-reference with talent profile impact/risk flags. Identify anyone who is (a) high impact of loss AND (b) hasn't had a raise in 12+ months OR is below the GR midpoint. Rank by risk.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring for all employees", "SF OData: User talent profile fields"],
                    "what_it_produces": "Priority stack-ranked list: name, grade, last increase date, current base vs midpoint estimate, impactOfLoss flag — with a plain-language risk sentence per person.",
                    "ae_bridge": "One agent instruction replaces a three-way VLOOKUP between EC, Talent, and a pay band spreadsheet. The manager gets a ranked action list, not a data dump.",
                },
                {
                    "beat": "Projection: what does fixing this cost?",
                    "agent_instruction": "For everyone flagged as below midpoint or overdue for a raise, calculate what it would cost to bring them to midpoint. Show the total budget impact and individual deltas.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring — current salaries and grade bands"],
                    "what_it_produces": "Cost-to-fix table: per person delta to midpoint, total annual cost, as a percentage of team payroll — formatted for a budget conversation.",
                    "ae_bridge": "The agent turned the risk list into a business case. The manager can take this to the CHRO or finance partner directly.",
                },
                {
                    "beat": "Comp narrative for the board slide",
                    "agent_instruction": "Summarise the team's compensation health for an executive audience: grade distribution, recent movement, outliers, and top retention risks. Write it as 4 bullet points suitable for an HR update slide.",
                    "mcp_calls": ["SF OData: salary history + talent profiles for all employees"],
                    "what_it_produces": "4 executive-ready bullet points, specific and data-grounded — names replaced with roles for board presentation.",
                    "ae_bridge": "From raw SF data to board-ready narrative in one agent step. This is what the desktop tier enables — not just answering, but producing.",
                },
            ],
        },
        "joule_prompts": [
            "Who on my team hasn't had a salary increase in the last 12 months?",
            "Show me anyone below the midpoint for their pay grade.",
            "Compare my top performer's salary progression to their peers.",
            "Summarise total compensation spend across my team.",
            "Who received a bonus but no merit increase this cycle?",
        ],
        "live_data": ["org_structure", "employees", "salary_history", "bonus", "talent_profiles"],
        "story_data": ["merit_proposals", "budget_approval_workflow", "pay_equity_analysis"],
        "story_narrative": (
            "3 years of salary history and a Dec 2025 bonus entry are live per employee. "
            "Joule can surface pay grade comparisons, salary progression, and flag outliers from real data. "
            "Merit proposal workflows and budget pool allocation are narrative — "
            "SF Compensation module (comp templates, budget pools) is not provisioned."
        ),
    },
    "talent_retention": {
        "label": "Talent Retention & Flight Risk",
        "org_bias": "standard",
        "talent_story": "Key roles at risk; succession gaps identified; retention actions needed before year-end.",
        "agent_card": {
            "title": "Talent Retention Assistant",
            "challenge": (
                "Three of your highest-impact employees are flagged as medium-to-high flight risk "
                "heading into year-end. Two of those roles have no identified successor. "
                "The window to act before the market opens in January is closing fast."
            ),
            "prompts": [
                "Who has high impact of loss and high risk of leaving right now?",
                "Which critical roles have no identified successor and a flight-risk incumbent?",
                "Show me future leaders on my team who haven't been recognised this year.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "The danger list",
                    "prompt": "Who has high impact of loss and high risk of leaving right now?",
                    "live_data": ["talent_profiles", "employees"],
                    "what_joule_shows": "Employees with impactOfLoss=HIGH and riskOfLoss=HIGH or MEDIUM, with their role and grade.",
                    "ae_bridge": "That's a real talent profile, set when we provisioned this org. Joule didn't infer it — it read it.",
                },
                {
                    "beat": "Where the succession bench is thin",
                    "prompt": "Which of those flight-risk roles have no identified successor?",
                    "live_data": ["succession_nominations", "talent_profiles"],
                    "what_joule_shows": "Roles with high-risk incumbent and either zero nominations or only 3+ year readiness nominees.",
                    "ae_bridge": "Two live data points connected: who might leave and who could step up. That's the gap the board cares about.",
                },
                {
                    "beat": "The recognition signal",
                    "prompt": "Show me future leaders on my team who haven't been recognised with a spot award this year.",
                    "live_data": ["talent_profiles", "spot_awards"],
                    "what_joule_shows": "futureLeader=true employees vs spot award recipients — surfacing who's been overlooked.",
                    "ae_bridge": "Recognition is a retention lever. Joule just told the manager who to act on before year-end.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the retention risk register",
                    "agent_instruction": "Read talent profiles for all employees. Identify anyone who is high impact of loss, medium or high risk of loss, AND either has no succession nomination or has a dev goal stuck On Track for 6+ months. For each person, write a two-sentence retention risk summary.",
                    "mcp_calls": ["SF OData: talent profile fields (impactOfLoss, riskOfLoss, futureLeader)", "SF OData: NominationService — succession depth per position", "SF OData: DevGoal_2001 — state and due date"],
                    "what_it_produces": "A ranked risk register: person, role, risk level, succession gap (yes/no), dev goal stall (yes/no), and a 2-sentence narrative per row — ready to paste into an HR business review deck.",
                    "ae_bridge": "The agent read across talent, succession, and goals in one pass. That's three modules connected, a task that would take an HRBP 30 minutes manually.",
                },
                {
                    "beat": "Match flight risks to their likely next move",
                    "agent_instruction": "For the top 2 flight risk employees, describe what their next role likely looks like externally based on their current title, grade, and goals. What would you offer to keep them? Frame it as a retention conversation guide for their manager.",
                    "mcp_calls": ["SF OData: EmpJob (title, grade)", "SF OData: DevGoal_2001 (aspiration)", "SF OData: spot awards (recognition history)"],
                    "what_it_produces": "Two manager-ready conversation guides: external market context, what the employee is likely being offered, and 3 specific retention levers the manager can pull — based on live data.",
                    "ae_bridge": "The agent synthesised data into advice. It's not telling the manager what SF says. It's telling the manager what to do.",
                },
                {
                    "beat": "CHRO briefing note",
                    "agent_instruction": "Prepare a 1-page retention risk brief for the CHRO. Lead with the number of high-risk roles, identify the two highest-priority actions, and end with a recommended 30-day plan.",
                    "mcp_calls": ["SF OData: talent profiles + succession + goals — aggregated view"],
                    "what_it_produces": "A structured CHRO brief: exec summary, risk count, the two names and their gaps, and a 30-day action plan — ready to send.",
                    "ae_bridge": "One instruction, one output. The CHRO gets a brief, not a dashboard. That's the agentic difference.",
                },
            ],
        },
        "joule_prompts": [
            "Who has high impact of loss and high risk of leaving right now?",
            "Which critical roles have no identified successor and a flight-risk incumbent?",
            "Show me future leaders on my team who haven't been recognised this year.",
            "Which employees have development goals tied to a next-level role?",
            "Recommend retention actions for my top three flight risks.",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "succession_nominations", "spot_awards", "goal_assignments"],
        "story_data": ["retention_action_plans", "development_conversations", "counter_offer_tracking"],
        "story_narrative": (
            "Talent profiles (impact/risk/futureLeader), succession nominations, and employee goals are live. "
            "Joule can identify flight risks, succession gaps, and development progress from real data. "
            "Retention action plans, 1:1 notes, and continuous feedback are narrative — "
            "they require the Continuous Feedback module to be configured."
        ),
    },
    "skills_learning": {
        "label": "Skills Gap & Learning Development",
        "org_bias": "standard",
        "talent_story": "Skills inventory incomplete; learning paths not aligned to role requirements; L&D budget under scrutiny.",
        "agent_card": {
            "title": "Skills & Development Advisor",
            "challenge": (
                "The L&D budget review is next month and the team can't answer which employees have "
                "critical skill gaps, which training has actually been completed, or whether the "
                "learning investments are aligned to the roles that matter most."
            ),
            "prompts": [
                "Which employees on my team are marked as future leaders but have no development goals?",
                "Show me who has high impact of loss and what their current development focus is.",
                "Which roles in my org have the widest gap between current grade and next-level requirements?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Find the development blind spots",
                    "prompt": "Which employees are marked as future leaders but have no development goals?",
                    "live_data": ["talent_profiles", "goal_assignments"],
                    "what_joule_shows": "futureLeader=true employees cross-referenced against DevGoal_2001 — who's flagged but not invested in.",
                    "ae_bridge": "The system knows who the company thinks is a future leader. The agent just checked whether anyone is actually doing anything about it.",
                },
                {
                    "beat": "Connect impact to development",
                    "prompt": "Show me who has high impact of loss and what their current development focus is.",
                    "live_data": ["talent_profiles", "goal_assignments"],
                    "what_joule_shows": "High-impact employees with their active dev goal name and metric — linking business criticality to growth trajectory.",
                    "ae_bridge": "This is what an HRBP would spend an afternoon pulling. Joule answered it in the conversation.",
                },
                {
                    "beat": "Readiness gap by role",
                    "prompt": "Which roles in my org have people at GR-13 who should be growing toward GR-14?",
                    "live_data": ["employees", "goal_assignments"],
                    "what_joule_shows": "GR-13 employees with their development goal focus — the organic succession pipeline below the formal nominations.",
                    "ae_bridge": "The nominations are the formal view. The goals data is the real signal of who's actually developing.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the development investment map",
                    "agent_instruction": "For each employee, read their dev goal (DevGoal_2001), talent profile (futureLeader, impactOfLoss), and pay grade. Create a 2x2 view: high impact + dev goal present, high impact + no dev goal, low impact + dev goal, low impact + no dev goal. Name each quadrant.",
                    "mcp_calls": ["SF OData: DevGoal_2001 by userId", "SF OData: User talent profile fields", "SF OData: EmpJob grades"],
                    "what_it_produces": "A 2x2 development investment map with named employees in each quadrant and a headline finding: e.g. '2 high-impact employees have no active development goal — the highest-priority gap.'",
                    "ae_bridge": "This is a classic talent analytics deliverable. It normally comes from a Workday or SF People Analytics export. The agent built it live from OData.",
                },
                {
                    "beat": "L&D priority recommendation",
                    "agent_instruction": "Based on the development gaps identified, recommend the top 3 development investments the company should make in the next 6 months. Anchor each recommendation to a specific employee, their gap, and the business impact of closing it.",
                    "mcp_calls": ["SF OData: DevGoal_2001 + talent profiles — cross-org view"],
                    "what_it_produces": "3 named development recommendations: [Employee] → [Gap] → [Recommended investment] → [Business case]. Concrete and actionable.",
                    "ae_bridge": "The agent moved from data to recommendation. That's what the talent leader needs for the L&D budget conversation.",
                },
                {
                    "beat": "Succession readiness vs formal nominations",
                    "agent_instruction": "Compare the formal succession nominations to the organic pipeline visible in dev goals. Who has a dev goal pointing toward a senior role but isn't on the formal succession list? Flag them as 'informal pipeline.'",
                    "mcp_calls": ["SF OData: NominationService — nominated successors", "SF OData: DevGoal_2001 — purpose and name fields"],
                    "what_it_produces": "Side-by-side: formal nominations table vs informal pipeline. Highlights anyone doing the right development work but not yet visible to senior leadership.",
                    "ae_bridge": "Two data layers that never talk to each other in standard reporting. The agent connected them and found people worth nominating.",
                },
            ],
        },
        "joule_prompts": [
            "Which employees on my team are marked as future leaders but have no development goals?",
            "Show me who has high impact of loss and what their current development focus is.",
            "Which roles in my org have the widest gap between current grade and next-level requirements?",
            "Recommend a development path for someone moving from GR-13 to GR-14.",
            "Who completed a development goal this year and is ready for stretch?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "goal_assignments"],
        "story_data": ["skills_assignments", "learning_completions", "learning_catalog"],
        "story_narrative": (
            "Org structure, talent profiles, and development goals (DevGoal_2001) are live. "
            "Joule can reason about development focus and future-leader readiness from real data. "
            "WSM skill profiles, LMS completions, and the learning catalog are narrative — "
            "they require Workforce Skills Management and Learning modules configured with content."
        ),
    },
    "performance_goals": {
        "label": "Performance Management & Goal Setting",
        "org_bias": "standard",
        "talent_story": "Mid-year review cycle; goals set; manager calibration session next week.",
        "agent_card": {
            "title": "Goals & Performance Assistant",
            "challenge": (
                "Calibration is scheduled for next week and the manager doesn't know which employees "
                "have made meaningful progress on their annual goals, who is coasting on development "
                "targets, and who deserves to be called out as a standout this cycle."
            ),
            "prompts": [
                "Show me the annual goals for each person on my team.",
                "Which employees have a development goal tied to a leadership or next-level skill?",
                "Who has been recognised with a spot award this year and what for?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Goal overview before the calibration walk-in",
                    "prompt": "Show me the annual goals for each person on my team.",
                    "live_data": ["goal_assignments", "employees"],
                    "what_joule_shows": "Each team member with their 2 Goal_11 entries — name, metric, and state (On Track).",
                    "ae_bridge": "Real goals, provisioned minutes ago. Joule read them from Goal_11. Not a demo account — this company was built for this conversation.",
                },
                {
                    "beat": "Who's investing in the next level",
                    "prompt": "Which employees have a development goal tied to a leadership or next-level skill?",
                    "live_data": ["goal_assignments"],
                    "what_joule_shows": "DevGoal_2001 entries with purpose='Current role' and name pointing to leadership or technical advancement.",
                    "ae_bridge": "Development goals are a leading indicator of who's ready to grow. The manager sees this before the calibration conversation, not after.",
                },
                {
                    "beat": "Recognition as a performance signal",
                    "prompt": "Who has been recognised with a spot award this year and what for?",
                    "live_data": ["spot_awards"],
                    "what_joule_shows": "SpotAward records: nominator, nominee, amount, reason, date — all Approved status.",
                    "ae_bridge": "Recognition data and performance data side by side. The manager can walk into calibration knowing who's been called out and why.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Pre-calibration team briefing",
                    "agent_instruction": "For each employee on the team, pull their 2 annual goals, 1 dev goal, spot award history, talent profile flags, and grade. Produce a calibration briefing card per person: 4 bullet points, written for a manager going into a talent review.",
                    "mcp_calls": ["SF OData: Goal_11 + DevGoal_2001 + SpotAward + talent profile fields + EmpJob grade — per employee"],
                    "what_it_produces": "5 calibration cards, one per employee: goals summary, development focus, recognition highlights, talent flags (futureLeader, impactOfLoss). Formatted as 4 bullets each.",
                    "ae_bridge": "The manager's prep pack for the calibration session. Normally this takes an HRBP half a day to compile. The agent built it in one pass across five SF entities.",
                },
                {
                    "beat": "Spot the standouts and the risks",
                    "agent_instruction": "Across the team, identify: (1) who has the strongest goal-recognition alignment — ambitious goals AND spot awards; (2) who has goals On Track but no recognition or dev investment — potential flight risk. Name both groups.",
                    "mcp_calls": ["SF OData: Goal_11 state + SpotAward + DevGoal_2001 + riskOfLoss"],
                    "what_it_produces": "Two named groups with plain-language reasoning: the standouts the manager should call out in calibration, and the 'quiet quitters' whose engagement signal is going negative.",
                    "ae_bridge": "The agent synthesised four signals into a manager action list. Not a report — a recommendation.",
                },
                {
                    "beat": "Draft the calibration talking points",
                    "agent_instruction": "For the top performer on this team, draft a calibration talking point: what they've achieved, how they've grown, what they should be recognised for. Then do the same for the person most at risk of being overlooked. Keep each under 100 words.",
                    "mcp_calls": ["SF OData: goals + awards + talent profile — top 2 employees"],
                    "what_it_produces": "Two calibration talking points, ready to read aloud: grounded in specific goals and recognition data, written to influence the calibration room.",
                    "ae_bridge": "This is what makes the Joule Desktop tier different from a chatbot. It produced the output the manager needs to walk out of calibration with their people fairly represented.",
                },
            ],
        },
        "joule_prompts": [
            "Show me the annual goals for each person on my team.",
            "Which employees have a development goal tied to a leadership or next-level skill?",
            "Who has been recognised with a spot award this year and what for?",
            "Draft a mid-year performance summary for my VP Engineering.",
            "Which team members have the strongest goal-to-recognition alignment?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "spot_awards", "goal_assignments"],
        "story_data": ["performance_forms", "calibration_sessions", "ratings"],
        "story_narrative": (
            "Annual goals (Goal_11) and development goals (DevGoal_2001) are provisioned live — "
            "each employee has 2 annual goals and 1 development goal. "
            "Joule can surface goal content, ownership, and spot award history from real data. "
            "Performance review forms, ratings, and calibration sessions are narrative — "
            "they require PM module form templates and an active review cycle."
        ),
    },
    "workforce_planning": {
        "label": "Workforce Planning & Org Design",
        "org_bias": "standard",
        "talent_story": "Headcount request submitted; org restructure under review; budget owner needs visibility.",
        "agent_card": {
            "title": "Org Design & Headcount Assistant",
            "challenge": (
                "A restructure proposal is on the table but the business lead doesn't have a clear "
                "picture of current span of control, which roles are vacant, or where the talent risk "
                "is concentrated before they move headcount around."
            ),
            "prompts": [
                "Show me the current org structure and reporting lines.",
                "Which managers have the widest span of control?",
                "Where is talent risk highest — who has no successor and is a flight risk?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Baseline the org before touching it",
                    "prompt": "Show me the current org structure and reporting lines.",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Org chart with reporting hierarchy, role titles, and headcount by department.",
                    "ae_bridge": "This is the org as provisioned. Every position, every reporting line, real data. The restructure conversation starts here.",
                },
                {
                    "beat": "Find the span-of-control problem",
                    "prompt": "Which managers have the widest span of control relative to their grade?",
                    "live_data": ["org_structure", "employees"],
                    "what_joule_shows": "Manager vs direct report count, flagging anyone above the recommended ratio for their grade.",
                    "ae_bridge": "A span-of-control problem is often invisible until a restructure makes it urgent. Joule surfaced it proactively.",
                },
                {
                    "beat": "Where is the talent risk concentrated?",
                    "prompt": "Which departments have the highest concentration of flight risk and no succession cover?",
                    "live_data": ["talent_profiles", "succession_nominations"],
                    "what_joule_shows": "Department-level risk summary: count of high riskOfLoss employees, succession nominations per position.",
                    "ae_bridge": "Before moving headcount, you need to know where you can't afford to lose people. This is that answer.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Org health scan for the restructure brief",
                    "agent_instruction": "Read the full org structure, grades, spans of control, talent profiles, and succession nominations. Produce a 1-page org health brief: headcount by department, span-of-control outliers, talent risk hot spots, and succession gaps. Write it for a CHRO who is reviewing a restructure proposal.",
                    "mcp_calls": ["SF OData: EmpJob org hierarchy", "SF OData: talent profiles all", "SF OData: NominationService succession depth"],
                    "what_it_produces": "A structured org health brief: department headcount table, 2 span-of-control flags, top 3 talent risk concentrations, and a bottom-line assessment of whether this org is restructure-ready.",
                    "ae_bridge": "This is the brief the CHRO needs before approving the proposal. The agent assembled it from three live SF entities in one instruction.",
                },
                {
                    "beat": "Restructure scenario: impact on talent risk",
                    "agent_instruction": "The proposal is to consolidate Operations and Engineering under a single VP. Read the talent profiles and succession nominations for both departments. Would this increase or decrease talent concentration risk? Name the specific risks.",
                    "mcp_calls": ["SF OData: EmpJob department filter — OPS and ENG", "SF OData: talent profiles + succession for those employees"],
                    "what_it_produces": "A named risk assessment: which individuals become critical concentrations under the merged structure, whether any succession nominations span both departments, and a go/no-go read on the consolidation.",
                    "ae_bridge": "The agent analysed a hypothetical decision against live data. That's the advisory capability the restructure team doesn't have without running a full workforce analytics project.",
                },
                {
                    "beat": "Headcount summary for the finance partner",
                    "agent_instruction": "Summarise current headcount, grade distribution, and average compensation by department. Format it as a table the finance partner can use to model the restructure cost impact.",
                    "mcp_calls": ["SF OData: EmpJob (grade, dept)", "SF OData: EmpPayCompRecurring (current salary per employee)"],
                    "what_it_produces": "A department-level table: headcount, grade mix (GR-11 to GR-15 counts), average base salary, total payroll — formatted for a finance modelling spreadsheet.",
                    "ae_bridge": "The agent turned SF compensation data into a finance-ready summary. One instruction, one output, no People Analytics licence required.",
                },
            ],
        },
        "joule_prompts": [
            "Show me the current org structure and reporting lines.",
            "Which managers have the widest span of control?",
            "Where is talent risk highest — who has no successor and is a flight risk?",
            "What positions are currently filled vs vacant?",
            "Summarise headcount by department and pay grade.",
        ],
        "live_data": ["org_structure", "employees", "positions", "talent_profiles"],
        "story_data": ["headcount_plan", "attrition_forecast", "org_restructure_proposal", "budget_submissions"],
        "story_narrative": (
            "Org structure, positions, and employee data are live — Joule can surface org charts, "
            "span of control, and talent risk concentration from real data. "
            "Headcount planning, attrition forecasts, and budget submissions are narrative — "
            "they require Workforce Planning module and integration with Finance."
        ),
    },
    "succession_prep": {
        "label": "Succession Nomination Prep",
        "org_bias": "standard",
        "talent_story": "Board review in 6 weeks; succession depth for C-1 and C-2 roles not documented; managers avoiding the conversation.",
        "agent_card": {
            "title": "Succession Nomination Prep Assistant",
            "challenge": (
                "Managers are delaying critical successor nomination conversations heading into "
                "the annual board talent review. Readiness ratings are missing, development gaps "
                "haven't been assessed, and there's no clear view of bench depth for the top roles."
            ),
            "prompts": [
                "Which critical positions have fewer than two active successors nominated?",
                "Show me the readiness rating and development goals for each nominated successor.",
                "Who is flagged as a future leader but not yet nominated for any succession plan?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Where is the bench thin?",
                    "prompt": "Which critical positions have fewer than two active successors nominated?",
                    "live_data": ["succession_nominations"],
                    "what_joule_shows": "Positions with 0 or 1 nominations from the live NominationService data.",
                    "ae_bridge": "These nominations were provisioned via the SF succession API. Real data, isolated company. This answer is grounded in what's actually in the system.",
                },
                {
                    "beat": "Successor readiness profile",
                    "prompt": "Show me the readiness rating and development goals for each nominated successor.",
                    "live_data": ["succession_nominations", "goal_assignments"],
                    "what_joule_shows": "Each nominee with their readiness value (1.0=Ready Now, 2.0=1-2yr, 3.0=3+yr) and their active development goal.",
                    "ae_bridge": "Readiness without development context is just a number. The goals data tells you whether the nominee is actually working toward it.",
                },
                {
                    "beat": "The pipeline below the nominations",
                    "prompt": "Who is flagged as a future leader but not yet nominated for any succession plan?",
                    "live_data": ["talent_profiles", "succession_nominations"],
                    "what_joule_shows": "futureLeader=true employees who do not appear in any NominationService record — the untapped pipeline.",
                    "ae_bridge": "Every succession plan has invisible candidates. Joule just made them visible.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Build the succession board pack",
                    "agent_instruction": "Read all succession nominations with their readiness ratings. For each nominated position, read the incumbent's talent profile and the nominee's development goals. Produce a succession briefing in the format used for a board talent review: position, incumbent risk, nominee(s), readiness, and a 2-sentence gap assessment.",
                    "mcp_calls": ["SF OData v4: NominationService — all nominations + readiness", "SF OData: talent profiles for incumbents", "SF OData: DevGoal_2001 for nominees"],
                    "what_it_produces": "A succession board pack: 5 position rows, each with incumbent flight risk, nominee names + readiness levels, active dev goals, and a named gap (e.g. 'VP Eng has one Ready Now nominee but their dev goal doesn't address the strategic planning gap the role requires.').",
                    "ae_bridge": "This is a three-module read that HR normally takes a week to compile for the board. The agent produced it in one instruction.",
                },
                {
                    "beat": "Identify the hidden successors",
                    "agent_instruction": "For any position with fewer than 2 nominees at Readiness 1.0 or 2.0, identify employees at GR-13 or GR-14 who have futureLeader=true, a development goal pointing toward leadership, and are NOT currently nominated. Flag them as 'informal pipeline — recommend for nomination.'",
                    "mcp_calls": ["SF OData v4: NominationService", "SF OData: talent profiles (futureLeader)", "SF OData: DevGoal_2001 (dev goal name)", "SF OData: EmpJob (grade)"],
                    "what_it_produces": "A recommendation list: [Name] → [Current role] → [Dev goal] → [Recommended for: Position X] with a 1-sentence rationale per person.",
                    "ae_bridge": "The agent found succession candidates the formal process missed. That's analyst-grade work done in seconds.",
                },
                {
                    "beat": "Pre-meeting briefing for the CHRO",
                    "agent_instruction": "The CHRO has a 30-minute talent review meeting in 2 days. Prepare a concise pre-read: how many positions have adequate succession cover, how many are at risk, and what the top 2 actions are before the board meeting. Keep it under 200 words.",
                    "mcp_calls": ["SF OData v4: NominationService — aggregate view", "SF OData: talent profiles — flight risk for incumbents"],
                    "what_it_produces": "A 200-word pre-read: succession health summary (X of Y positions covered), the 2 critical gaps, and 2 specific action recommendations with named owners.",
                    "ae_bridge": "The CHRO gets a brief, not a dashboard. The agent wrote it. That's the Joule Desktop value proposition in one output.",
                },
            ],
        },
        "joule_prompts": [
            "Which critical positions have fewer than two active successors nominated?",
            "Show me the readiness rating and development goals for each nominated successor.",
            "Who is flagged as a future leader but not yet nominated for any succession plan?",
            "Prepare a succession briefing for the VP Engineering role.",
            "Which successors are ready now vs 1–2 years out?",
        ],
        "live_data": ["org_structure", "employees", "talent_profiles", "succession_nominations", "goal_assignments"],
        "story_data": ["readiness_assessments", "development_conversations", "board_pack"],
        "story_narrative": (
            "Succession nominations (OData v4 NominationService) and talent profiles are live. "
            "Joule can surface bench depth, readiness levels, and development goal alignment from real data. "
            "Formal readiness assessments, 9-box placements, and board pack generation are narrative — "
            "they require the Succession & Development module to be fully configured."
        ),
    },
    "pay_equity_deep_dive": {
        "label": "Pay Equity & Compensation Fairness",
        "org_bias": "standard",
        "talent_story": "CHRO asked for a pay equity audit before the comp cycle opens; grade and impact disparities need to be surfaced.",
        "agent_card": {
            "title": "Pay Equity Audit Assistant",
            "challenge": (
                "The CHRO needs a pay equity snapshot before the compensation cycle opens. "
                "There's no quick way to see who is paid below midpoint, whether high-impact employees "
                "are fairly compensated relative to peers, or where the largest salary variance sits."
            ),
            "prompts": [
                "Show me everyone below the midpoint for their pay grade across the org.",
                "Who has the highest impact of loss but is in the bottom quartile for their grade?",
                "Compare the salary progression over 3 years for GR-14 vs GR-13 employees.",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Midpoint gap across the org",
                    "prompt": "Show me everyone below the midpoint for their pay grade.",
                    "live_data": ["salary_history", "employees"],
                    "what_joule_shows": "Employees with current base vs estimated grade midpoint, flagging those below — drawn from 3 years of live EmpPayCompRecurring data.",
                    "ae_bridge": "This used to require a People Analytics report or a manual VLOOKUP against a pay band spreadsheet. Joule read it from EC directly.",
                },
                {
                    "beat": "High impact, underpaid — the dangerous combination",
                    "prompt": "Who has the highest impact of loss but is in the bottom quartile for their grade?",
                    "live_data": ["salary_history", "talent_profiles"],
                    "what_joule_shows": "Cross-reference of impactOfLoss=HIGH with bottom-quartile comp within their grade band.",
                    "ae_bridge": "This is the pay equity risk the CHRO actually cares about. Not aggregate statistics — specific people who can be named and acted on.",
                },
                {
                    "beat": "3-year progression: who's moving, who's stuck",
                    "prompt": "Compare salary progression over 3 years for GR-14 versus GR-13 employees.",
                    "live_data": ["salary_history"],
                    "what_joule_shows": "Year-over-year average base by grade, showing differential growth rates between GR-14 and GR-13.",
                    "ae_bridge": "3 years of real salary history per person. The progression comparison is live, not simulated.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "Full pay equity audit report",
                    "agent_instruction": "Read salary history and talent profile flags for all employees. Produce a pay equity audit: (1) who is below midpoint by grade, (2) the correlation between impactOfLoss and pay quartile, (3) salary growth rate by grade over 3 years. Present as a structured report suitable for the CHRO.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring — 3 entries per employee", "SF OData: talent profile fields — impactOfLoss"],
                    "what_it_produces": "A 3-section pay equity report: below-midpoint list by grade, impact-to-pay correlation summary with named outliers, and grade-level 3-year CAGR comparison.",
                    "ae_bridge": "This is an HR analytics deliverable that normally takes a People Analytics team 2 days to produce. The agent built it from SF OData in one instruction.",
                },
                {
                    "beat": "Prioritised remediation list",
                    "agent_instruction": "From the pay equity findings, rank the top 3 employees who need an immediate comp correction. For each: current base, estimated midpoint, correction delta, and business case for acting before the comp cycle opens.",
                    "mcp_calls": ["SF OData: EmpPayCompRecurring + talent profiles — sorted by risk-impact combination"],
                    "what_it_produces": "A remediation action list: 3 employees ranked by urgency, with their delta to midpoint in absolute and percentage terms, and a 1-sentence business case each ('GR-14 impact-critical role, 12% below midpoint, no raise in 18 months').",
                    "ae_bridge": "The agent converted an audit finding into an action list. The CHRO can forward this to the comp team as-is.",
                },
                {
                    "beat": "Equity narrative for the board",
                    "agent_instruction": "Write a 3-bullet pay equity summary for a board HR committee update. Lead with the headline finding, note the 2 highest-risk outliers (by role, not name), and close with a recommended action timeline.",
                    "mcp_calls": ["SF OData: compensation + talent profiles — aggregated"],
                    "what_it_produces": "3 board-ready bullet points: headline stat, two named risks (by role), action timeline. Appropriate for a 15-minute governance committee update.",
                    "ae_bridge": "From raw SF compensation data to boardroom-ready language. The agent bridged that gap in one step.",
                },
            ],
        },
        "joule_prompts": [
            "Show me everyone below the midpoint for their pay grade across the org.",
            "Who has the highest impact of loss but is in the bottom quartile for their grade?",
            "Compare the salary progression over 3 years for GR-14 vs GR-13 employees.",
            "Which departments have the widest spread between highest and lowest paid at the same grade?",
            "Flag anyone whose bonus was above target but base salary hasn't moved in 3 years.",
        ],
        "live_data": ["org_structure", "employees", "salary_history", "bonus", "talent_profiles"],
        "story_data": ["pay_equity_analysis", "compa_ratio_report", "gender_pay_gap_report"],
        "story_narrative": (
            "3 years of salary history and a Dec 2025 bonus entry are live per employee, with talent profile "
            "impact/risk ratings and pay grades for every person. Joule can surface grade-level comparisons "
            "and flag outliers from real data. Formal pay equity analysis, compa-ratio reports, and gender "
            "pay gap reporting are narrative — they require EC Compensation module and reporting configuration."
        ),
    },
    "onboarding_readiness": {
        "label": "Onboarding & Day-One Readiness",
        "org_bias": "standard",
        "talent_story": "New hire starts in 3 weeks; manager unprepared; no visibility into team context or first-week plan.",
        "agent_card": {
            "title": "Onboarding Readiness Assistant",
            "challenge": (
                "A new hire is 3 weeks out from their start date and the hiring manager hasn't been "
                "told who else is on the team, what the org looks like around them, or what a realistic "
                "30-60-90 day plan should include given the team's current goals and priorities."
            ),
            "prompts": [
                "Who is Sam Rivera's manager and what does their team look like?",
                "What are the current annual goals for the team Sam is joining?",
                "Who on the team was recently recognised — what were they called out for?",
            ],
        },
        "demo_story": {
            "joule_chat": [
                {
                    "beat": "Manager and team context",
                    "prompt": "Who is Sam Rivera's manager and what does their team look like?",
                    "live_data": ["onboardee", "org_structure", "employees"],
                    "what_joule_shows": "Sam's EmpJob record: manager name, role, and direct report list under that manager.",
                    "ae_bridge": "Sam is live in SF. Manager is assigned. This is what Day 1 prep looks like when the system is actually set up.",
                },
                {
                    "beat": "What the team is focused on",
                    "prompt": "What are the current annual goals for the team Sam is joining?",
                    "live_data": ["goal_assignments", "employees"],
                    "what_joule_shows": "Goal_11 entries for each team member — what they're working toward in the current cycle.",
                    "ae_bridge": "Sam can walk in knowing the team's priorities before their first meeting. Not guessing from a job description.",
                },
                {
                    "beat": "Recognition culture signal",
                    "prompt": "Who on the team was recently recognised and what for?",
                    "live_data": ["spot_awards"],
                    "what_joule_shows": "SpotAward records for team members: award reason, nominator, amount — gives Sam a read on team norms and who the standouts are.",
                    "ae_bridge": "Recognition history is a proxy for team culture and values. Joule just gave Sam a head start on reading the room.",
                },
            ],
            "joule_desktop": [
                {
                    "beat": "The manager's Day 1 prep briefing",
                    "agent_instruction": "Read Sam Rivera's employment record, their manager's team structure, the team's active annual and development goals, and recent spot awards. Produce a manager briefing for Sam's first week: who's on the team, what they're working on, and 3 suggestions for Sam's first conversations.",
                    "mcp_calls": ["SF OData: EmpJob for Sam + manager's direct reports", "SF OData: Goal_11 + DevGoal_2001 for team", "SF OData: SpotAward for team"],
                    "what_it_produces": "A Day 1 manager briefing: team roster with roles and grades, 2-sentence goal summary per person, recognition highlights, and 3 named first-conversation suggestions tailored to the team's real priorities.",
                    "ae_bridge": "The manager prep note the system should have generated automatically. The agent built it from live SF data in one instruction.",
                },
                {
                    "beat": "Sam's personalised 30-60-90 plan",
                    "agent_instruction": "Based on the team's active goals and Sam's role as Senior Associate, draft a 30-60-90 day onboarding plan. Each phase should reference a real team goal or a named colleague. Make it specific, not generic.",
                    "mcp_calls": ["SF OData: Goal_11 for team", "SF OData: EmpJob for Sam + team members", "SF OData: SpotAward — recognition context"],
                    "what_it_produces": "A structured 30-60-90 plan: Days 1-30 (team orientation, named first meetings, goal alignment), Days 31-60 (contribution phase, tied to specific team goals), Days 61-90 (independent contribution, named stretch objective). Real names and real goals throughout.",
                    "ae_bridge": "Not a template. A plan grounded in the actual team Sam is joining. This is what separates Joule Desktop from a doc generator.",
                },
                {
                    "beat": "Talent risk context for HR",
                    "agent_instruction": "For the team Sam Rivera is joining, surface any talent risk the HR team should know about at onboarding time: flight risk, succession gaps, or future leaders without development investment. Write it as a 3-bullet HR intake note.",
                    "mcp_calls": ["SF OData: talent profiles for team", "SF OData: NominationService — succession for team roles", "SF OData: DevGoal_2001 for futureLeader employees"],
                    "what_it_produces": "A 3-bullet HR intake note: (1) any flight risk on the receiving team, (2) succession gaps Sam might be positioned to eventually fill, (3) development investment gaps. Framed for the HRBP who owns the onboarding.",
                    "ae_bridge": "The agent gave HR the context they need to position this hire strategically — not just process the paperwork.",
                },
            ],
        },
        "joule_prompts": [
            "Who is Sam Rivera's manager and what does their team look like?",
            "What are the current annual goals for the team Sam is joining?",
            "Who on the team was recently recognised — what were they called out for?",
            "Draft a 30-60-90 day onboarding plan for Sam based on the team's current priorities.",
            "What development goals are active on the team Sam is joining?",
        ],
        "live_data": ["org_structure", "employees", "onboardee", "goal_assignments", "spot_awards"],
        "story_data": ["onboarding_tasks", "buddy_assignment", "equipment_provisioning"],
        "story_narrative": (
            "The onboardee Sam Rivera is live in SF with a manager, position, and Nov 3 start date. "
            "The team's goals (Goal_11) and recent spot awards are also live — "
            "Joule can give the manager a real picture of what Sam is walking into. "
            "Formal onboarding task lists, buddy assignments, and equipment provisioning are narrative — "
            "they require Onboarding module workflow configuration."
        ),
    },
}

LOCALE_CONFIG = {
    "USA": {"currency": "USD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "US/Eastern",        "country_code": "USA"},
    "GBR": {"currency": "GBP", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/London",     "country_code": "GBR"},
    "DEU": {"currency": "EUR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/Berlin",     "country_code": "DEU"},
    "FRA": {"currency": "EUR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/Paris",      "country_code": "FRA"},
    "IND": {"currency": "INR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Asia/Kolkata",      "country_code": "IND"},
    "AUS": {"currency": "AUD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Australia/Sydney",  "country_code": "AUS"},
    "SGP": {"currency": "SGD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Asia/Singapore",    "country_code": "SGP"},
    "BRA": {"currency": "BRL", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "America/Sao_Paulo", "country_code": "BRA"},
}

INDUSTRY_ROLES = {
    "retail": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "SVP Ops",         "SVP Store Operations",       "OPS",  "GR-14"),
        "merch": ("003", "VP Merch",        "VP Merchandising",           "PROD", "GR-14"),
        "hr":    ("004", "CHRO",            "Chief HR Officer",           "OPS",  "GR-14"),
        "fin":   ("005", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
    },
    "tech": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "eng":   ("002", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "prod":  ("003", "VP Product",      "VP Product",                 "PROD", "GR-14"),
        "sales": ("004", "VP Sales",        "VP Sales",                   "SALES","GR-14"),
        "cos":   ("005", "CoS",             "Chief of Staff",             "OPS",  "GR-12"),
    },
    "manufacturing": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "plant": ("002", "Plant Mgr",       "Plant Manager",              "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "sc":    ("004", "SC Mgr",          "Supply Chain Manager",       "SALES","GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "healthcare": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cmo":   ("002", "CMO",             "Chief Medical Officer",      "MED",  "GR-15"),
        "ops":   ("003", "VP Ops",          "VP Clinical Operations",     "OPS",  "GR-14"),
        "fin":   ("004", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "financial_services": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cro":   ("002", "CRO",             "Chief Risk Officer",         "FIN",  "GR-15"),
        "ops":   ("003", "COO",             "Chief Operating Officer",    "OPS",  "GR-14"),
        "sales": ("004", "Head Sales",      "Head of Client Coverage",    "SALES","GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "energy": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "VP Ops",          "VP Field Operations",        "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "hse":   ("004", "HSE Dir",         "Director Health Safety Env", "OPS",  "GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
}

FIRST_NAMES = {
    "ceo": ("Jordan", "Kim"),       "eng": ("Priya", "Mehta"),
    "prod": ("Sona", "Park"),       "sales": ("Marcus", "Webb"),
    "cos": ("Elise", "Torres"),     "ops": ("Dana", "Reeves"),
    "plant": ("Marco", "Silva"),    "sc": ("Ayesha", "Khan"),
    "hr": ("Hira", "Nair"),         "merch": ("Cleo", "Nash"),
    "fin": ("Jordan", "Moss"),      "cmo": ("Dr. Ethan", "Walsh"),
    "cro": ("Natalie", "Cross"),    "hse": ("Owen", "Fletcher"),
    "head sales": ("Leon", "Park"),
}

SALARY_HISTORY = {
    "GR-15": [160000, 171000, 182000],
    "GR-14": [142000, 152000, 162000],
    "GR-13": [118000, 127000, 136000],
    "GR-12": [116000, 125000, 135000],
    "GR-11": [100000, 108000, 118000],
}

GRADE_IMPACT = {
    "GR-15": ("HIGH",   "HIGH",   True),
    "GR-14": ("HIGH",   "MEDIUM", True),
    "GR-13": ("MEDIUM", "MEDIUM", False),
    "GR-12": ("MEDIUM", "LOW",    False),
    "GR-11": ("LOW",    "LOW",    False),
}

DEPT_DIVISION = {
    "EXEC": "CORP_SVCS", "OPS": "CORP_SVCS", "SALES": "CORP_SVCS",
    "FIN": "CORP_SVCS",  "MED": "MANU",      "ENG": "MANU",
    "PROD": "MANU",
}
DEPT_BU = {
    "EXEC": "CORP", "OPS": "CORP",  "SALES": "CORP", "FIN": "CORP",
    "MED": "PRODS", "ENG": "PRODS", "PROD": "PRODS",
}
DEPT_NAMES = {
    "EXEC": "Executive", "ENG": "Engineering", "PROD": "Product",
    "SALES": "Sales",    "OPS": "Operations",  "FIN": "Finance",
    "MED": "Medical",    "ADMIN": "Administration",
}

BONUS_BY_GRADE = {
    "GR-15": 25000, "GR-14": 18000, "GR-13": 14000,
    "GR-12": 12000, "GR-11": 10000,
}

# Annual goals (Goal_11) and development goals (DevGoal_2001) by industry role
# Each tuple: (annual_goal_1_name, annual_goal_1_metric, annual_goal_2_name, annual_goal_2_metric,
#              dev_goal_name, dev_goal_metric)
GOAL_CONTENT = {
    "ceo": (
        "Company Revenue Growth",        "Achieve 20% YoY revenue growth and expand to 2 new markets",
        "Leadership & Culture",          "Maintain eNPS >= 55; complete 4 all-hands and 1 offsite",
        "Executive Presence & Stakeholder Influence", "Lead 3 board-level presentations; complete exec leadership programme",
    ),
    "eng": (
        "Platform Reliability",          "Achieve 99.9% uptime; reduce P1 incidents by 40%",
        "Engineering Velocity",          "Ship 85% of planned roadmap features on schedule",
        "Technical Architecture Mastery","Complete cloud architecture certification; lead 1 system design review",
    ),
    "prod": (
        "Product Launch Success",        "Launch 2 major features with NPS >= 45 and adoption >= 60%",
        "Customer Discovery",            "Conduct 24 customer interviews; translate insights into 3 product bets",
        "Product Strategy & Roadmapping","Complete advanced product strategy course; present 3-year vision to leadership",
    ),
    "sales": (
        "Sales Target Achievement",      "Achieve 110% of quota; close 5 new enterprise logos",
        "Pipeline Development",          "Maintain pipeline coverage 3x; generate 40 qualified opportunities",
        "Consultative Selling Skills",   "Complete Miller Heiman certification; apply methodology on 10 deals",
    ),
    "cos": (
        "Executive Coordination",        "Drive 100% on-time delivery of CEO-sponsored initiatives",
        "Process Improvement",           "Identify and eliminate 3 bottlenecks; reduce decision latency by 30%",
        "Strategic Communication",       "Complete executive communication programme; shadow 5 C-level strategy sessions",
    ),
    "ops": (
        "Operational Efficiency",        "Reduce operational costs by 12% while maintaining quality SLAs",
        "Process Automation",            "Automate 4 manual workflows; save 200+ hours/month",
        "Change Management",             "Complete prosci change management certification; lead 1 transformation project",
    ),
    "plant": (
        "Production Output",             "Meet 98% of monthly production targets with <1% defect rate",
        "Safety & Compliance",           "Zero LTIs; maintain ISO certification; 100% audit pass rate",
        "Lean Manufacturing",            "Complete Lean Six Sigma Green Belt; apply to 2 production lines",
    ),
    "sc": (
        "Supply Chain Resilience",       "Reduce lead times by 15%; build dual-sourcing for top 10 components",
        "Inventory Optimisation",        "Reduce inventory carrying cost by 10% while maintaining 98% fill rate",
        "Supply Chain Analytics",        "Complete supply chain analytics certification; build 2 predictive dashboards",
    ),
    "hr": (
        "Employee Experience",           "Achieve 80% engagement score; reduce voluntary turnover to <10%",
        "Talent Acquisition",            "Fill open positions in <45 days avg; achieve 90% hiring manager satisfaction",
        "HR Digital Transformation",     "Complete SAP SuccessFactors advanced certification; lead 2 HR tech rollouts",
    ),
    "merch": (
        "Category Performance",          "Grow category revenue by 15%; achieve 50% gross margin on key lines",
        "Vendor Partnership",            "Negotiate 3 new strategic vendor agreements; reduce procurement cost 8%",
        "Merchandising Analytics",       "Complete retail analytics certification; implement data-driven assortment model",
    ),
    "fin": (
        "Financial Close Excellence",    "Achieve hard close in 3 days; zero material audit findings",
        "Cost Reduction Initiatives",    "Identify and deliver $2M in cost savings across 3 business units",
        "Financial Modelling",           "Complete FP&A advanced programme; build integrated 3-statement model",
    ),
    "cmo": (
        "Clinical Quality Outcomes",     "Maintain patient satisfaction >= 90%; zero preventable adverse events",
        "Clinical Efficiency",           "Reduce average length of stay by 8%; improve throughput by 12%",
        "Clinical Leadership",           "Complete healthcare executive leadership programme; mentor 3 junior clinicians",
    ),
    "cro": (
        "Risk Framework",                "Implement enterprise risk framework; reduce operational risk exposure by 20%",
        "Regulatory Compliance",         "Zero regulatory breaches; pass all audits with zero material findings",
        "Risk Analytics",                "Complete advanced risk modelling certification; build 2 predictive risk models",
    ),
    "hse": (
        "Safety Performance",            "Zero LTIs; achieve TRIR <= 0.4; maintain all safety certifications",
        "Environmental Compliance",      "Meet all regulatory targets; reduce Scope 1 emissions by 8%",
        "HSE Systems",                   "Complete NEBOSH diploma; implement ISO 14001 improvements in 2 sites",
    ),
}

# Fallback goal content for roles not in GOAL_CONTENT
_DEFAULT_GOAL = (
    "Business Performance",          "Achieve key performance targets and deliver measurable business impact",
    "Team Development",              "Complete assigned goals on schedule with high quality output",
    "Professional Development",      "Complete 2 relevant training courses; apply learnings to current role",
)

LOCALE_CONFIG = {
    "USA": {"currency": "USD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "US/Eastern",        "country_code": "USA"},
    "GBR": {"currency": "GBP", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/London",     "country_code": "GBR"},
    "DEU": {"currency": "EUR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/Berlin",     "country_code": "DEU"},
    "FRA": {"currency": "EUR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Europe/Paris",      "country_code": "FRA"},
    "IND": {"currency": "INR", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Asia/Kolkata",      "country_code": "IND"},
    "AUS": {"currency": "AUD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Australia/Sydney",  "country_code": "AUS"},
    "SGP": {"currency": "SGD", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "Asia/Singapore",    "country_code": "SGP"},
    "BRA": {"currency": "BRL", "pay_scale": "USA/US1", "pay_group": "US", "locale": "en_US", "tz": "America/Sao_Paulo", "country_code": "BRA"},
}

INDUSTRY_ROLES = {
    "retail": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "SVP Ops",         "SVP Store Operations",       "OPS",  "GR-14"),
        "merch": ("003", "VP Merch",        "VP Merchandising",           "PROD", "GR-14"),
        "hr":    ("004", "CHRO",            "Chief HR Officer",           "OPS",  "GR-14"),
        "fin":   ("005", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
    },
    "tech": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "eng":   ("002", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "prod":  ("003", "VP Product",      "VP Product",                 "PROD", "GR-14"),
        "sales": ("004", "VP Sales",        "VP Sales",                   "SALES","GR-14"),
        "cos":   ("005", "CoS",             "Chief of Staff",             "OPS",  "GR-12"),
    },
    "manufacturing": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "plant": ("002", "Plant Mgr",       "Plant Manager",              "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "sc":    ("004", "SC Mgr",          "Supply Chain Manager",       "SALES","GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "healthcare": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cmo":   ("002", "CMO",             "Chief Medical Officer",      "MED",  "GR-15"),
        "ops":   ("003", "VP Ops",          "VP Clinical Operations",     "OPS",  "GR-14"),
        "fin":   ("004", "CFO",             "Chief Financial Officer",    "FIN",  "GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "financial_services": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "cro":   ("002", "CRO",             "Chief Risk Officer",         "FIN",  "GR-15"),
        "ops":   ("003", "COO",             "Chief Operating Officer",    "OPS",  "GR-14"),
        "sales": ("004", "Head Sales",      "Head of Client Coverage",    "SALES","GR-14"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
    "energy": {
        "ceo":   ("001", "CEO",             "Chief Executive Officer",    "EXEC", "GR-15"),
        "ops":   ("002", "VP Ops",          "VP Field Operations",        "OPS",  "GR-14"),
        "eng":   ("003", "VP Eng",          "VP Engineering",             "ENG",  "GR-14"),
        "hse":   ("004", "HSE Dir",         "Director Health Safety Env", "OPS",  "GR-13"),
        "hr":    ("005", "HR BP",           "HR Business Partner",        "OPS",  "GR-11"),
    },
}

FIRST_NAMES = {
    "ceo": ("Jordan", "Kim"),       "eng": ("Priya", "Mehta"),
    "prod": ("Sona", "Park"),       "sales": ("Marcus", "Webb"),
    "cos": ("Elise", "Torres"),     "ops": ("Dana", "Reeves"),
    "plant": ("Marco", "Silva"),    "sc": ("Ayesha", "Khan"),
    "hr": ("Hira", "Nair"),         "merch": ("Cleo", "Nash"),
    "fin": ("Jordan", "Moss"),      "cmo": ("Dr. Ethan", "Walsh"),
    "cro": ("Natalie", "Cross"),    "hse": ("Owen", "Fletcher"),
    "head sales": ("Leon", "Park"),
}

SALARY_HISTORY = {
    "GR-15": [160000, 171000, 182000],
    "GR-14": [142000, 152000, 162000],
    "GR-13": [118000, 127000, 136000],
    "GR-12": [116000, 125000, 135000],
    "GR-11": [100000, 108000, 118000],
}

GRADE_IMPACT = {
    "GR-15": ("HIGH",   "HIGH",   True),
    "GR-14": ("HIGH",   "MEDIUM", True),
    "GR-13": ("MEDIUM", "MEDIUM", False),
    "GR-12": ("MEDIUM", "LOW",    False),
    "GR-11": ("LOW",    "LOW",    False),
}

DEPT_DIVISION = {
    "EXEC": "CORP_SVCS", "OPS": "CORP_SVCS", "SALES": "CORP_SVCS",
    "FIN": "CORP_SVCS",  "MED": "MANU",      "ENG": "MANU",
    "PROD": "MANU",
}
DEPT_BU = {
    "EXEC": "CORP", "OPS": "CORP",  "SALES": "CORP", "FIN": "CORP",
    "MED": "PRODS", "ENG": "PRODS", "PROD": "PRODS",
}
DEPT_NAMES = {
    "EXEC": "Executive", "ENG": "Engineering", "PROD": "Product",
    "SALES": "Sales",    "OPS": "Operations",  "FIN": "Finance",
    "MED": "Medical",    "ADMIN": "Administration",
}

BONUS_BY_GRADE = {
    "GR-15": 25000, "GR-14": 18000, "GR-13": 14000,
    "GR-12": 12000, "GR-11": 10000,
}

# Annual goals (Goal_11) and development goals (DevGoal_2001) by industry role
# Each tuple: (annual_goal_1_name, annual_goal_1_metric, annual_goal_2_name, annual_goal_2_metric,
#              dev_goal_name, dev_goal_metric)
GOAL_CONTENT = {
    "ceo": (
        "Company Revenue Growth",        "Achieve 20% YoY revenue growth and expand to 2 new markets",
        "Leadership & Culture",          "Maintain eNPS >= 55; complete 4 all-hands and 1 offsite",
        "Executive Presence & Stakeholder Influence", "Lead 3 board-level presentations; complete exec leadership programme",
    ),
    "eng": (
        "Platform Reliability",          "Achieve 99.9% uptime; reduce P1 incidents by 40%",
        "Engineering Velocity",          "Ship 85% of planned roadmap features on schedule",
        "Technical Architecture Mastery","Complete cloud architecture certification; lead 1 system design review",
    ),
    "prod": (
        "Product Launch Success",        "Launch 2 major features with NPS >= 45 and adoption >= 60%",
        "Customer Discovery",            "Conduct 24 customer interviews; translate insights into 3 product bets",
        "Product Strategy & Roadmapping","Complete advanced product strategy course; present 3-year vision to leadership",
    ),
    "sales": (
        "Sales Target Achievement",      "Achieve 110% of quota; close 5 new enterprise logos",
        "Pipeline Development",          "Maintain pipeline coverage 3x; generate 40 qualified opportunities",
        "Consultative Selling Skills",   "Complete Miller Heiman certification; apply methodology on 10 deals",
    ),
    "cos": (
        "Executive Coordination",        "Drive 100% on-time delivery of CEO-sponsored initiatives",
        "Process Improvement",           "Identify and eliminate 3 bottlenecks; reduce decision latency by 30%",
        "Strategic Communication",       "Complete executive communication programme; shadow 5 C-level strategy sessions",
    ),
    "ops": (
        "Operational Efficiency",        "Reduce operational costs by 12% while maintaining quality SLAs",
        "Process Automation",            "Automate 4 manual workflows; save 200+ hours/month",
        "Change Management",             "Complete prosci change management certification; lead 1 transformation project",
    ),
    "plant": (
        "Production Output",             "Meet 98% of monthly production targets with <1% defect rate",
        "Safety & Compliance",           "Zero LTIs; maintain ISO certification; 100% audit pass rate",
        "Lean Manufacturing",            "Complete Lean Six Sigma Green Belt; apply to 2 production lines",
    ),
    "sc": (
        "Supply Chain Resilience",       "Reduce lead times by 15%; build dual-sourcing for top 10 components",
        "Inventory Optimisation",        "Reduce inventory carrying cost by 10% while maintaining 98% fill rate",
        "Supply Chain Analytics",        "Complete supply chain analytics certification; build 2 predictive dashboards",
    ),
    "hr": (
        "Employee Experience",           "Achieve 80% engagement score; reduce voluntary turnover to <10%",
        "Talent Acquisition",            "Fill open positions in <45 days avg; achieve 90% hiring manager satisfaction",
        "HR Digital Transformation",     "Complete SAP SuccessFactors advanced certification; lead 2 HR tech rollouts",
    ),
    "merch": (
        "Category Performance",          "Grow category revenue by 15%; achieve 50% gross margin on key lines",
        "Vendor Partnership",            "Negotiate 3 new strategic vendor agreements; reduce procurement cost 8%",
        "Merchandising Analytics",       "Complete retail analytics certification; implement data-driven assortment model",
    ),
    "fin": (
        "Financial Close Excellence",    "Achieve hard close in 3 days; zero material audit findings",
        "Cost Reduction Initiatives",    "Identify and deliver $2M in cost savings across 3 business units",
        "Financial Modelling",           "Complete FP&A advanced programme; build integrated 3-statement model",
    ),
    "cmo": (
        "Clinical Quality Outcomes",     "Maintain patient satisfaction >= 90%; zero preventable adverse events",
        "Clinical Efficiency",           "Reduce average length of stay by 8%; improve throughput by 12%",
        "Clinical Leadership",           "Complete healthcare executive leadership programme; mentor 3 junior clinicians",
    ),
    "cro": (
        "Risk Framework",                "Implement enterprise risk framework; reduce operational risk exposure by 20%",
        "Regulatory Compliance",         "Zero regulatory breaches; pass all audits with zero material findings",
        "Risk Analytics",                "Complete advanced risk modelling certification; build 2 predictive risk models",
    ),
    "hse": (
        "Safety Performance",            "Zero LTIs; achieve TRIR <= 0.4; maintain all safety certifications",
        "Environmental Compliance",      "Meet all regulatory targets; reduce Scope 1 emissions by 8%",
        "HSE Systems",                   "Complete NEBOSH diploma; implement ISO 14001 improvements in 2 sites",
    ),
}

# Fallback goal content for roles not in GOAL_CONTENT
_DEFAULT_GOAL = (
    "Business Performance",          "Achieve key performance targets and deliver measurable business impact",
    "Team Development",              "Complete assigned goals on schedule with high quality output",
    "Professional Development",      "Complete 2 relevant training courses; apply learnings to current role",
)
