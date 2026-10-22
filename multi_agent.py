import json
import anthropic
from agent import SYSTEM as INTAKE_SYSTEM, TOOLS as INTAKE_TOOLS, TOOL_FUNCTIONS as INTAKE_FUNCTIONS
from agent import EMPLOYEES, DATASETS

client = anthropic.Anthropic()

# ---------------- The policy library (the rules the Policy agent must follow and cite) ----------------
POLICIES = """
POL-001 Restricted data: Requests for Restricted data always need human review (needs_review).
POL-002 Role-based access: Internal data may go to any active employee. Confidential data may go only to
        someone in the dataset's owning department OR with a role in its allowed_roles. Otherwise deny.
POL-003 Personal data (PII): PII may be accessed only for an approved purpose: fraud investigation,
        regulatory reporting, customer support, or legal review. If the purpose is not approved or is
        "unknown", needs_review. PII access longer than 90 days: needs_review.
POL-004 Legal hold: Data under legal hold may be read, but never exported or deleted. Export/delete: deny.
POL-005 Duration: Requests longer than 180 days are denied.
POL-006 Identity: The requester must be a known, active employee. Unknown or terminated requester: deny.
        If the dataset is unknown: needs_review.
        If someone requests on behalf of another person, the requester is the person asking.
        If the person asking is not identified, deny.
"""


# ---------------- Policy agent tools ----------------
def get_employee(employee_id):
    for e in EMPLOYEES:
        if e["id"] == employee_id:
            return e
    return {"error": f"No employee with id {employee_id}"}


def get_dataset(dataset_id):
    for d in DATASETS:
        if d["id"] == dataset_id:
            return d
    return {"error": f"No dataset with id {dataset_id}"}


POLICY_FUNCTIONS = {"get_employee": get_employee, "get_dataset": get_dataset}

POLICY_TOOLS = [
    {"name": "get_employee", "description": "Get one employee's record by id (e.g. EMP-101).",
     "input_schema": {"type": "object", "properties": {"employee_id": {"type": "string"}}, "required": ["employee_id"]}},
    {"name": "get_dataset", "description": "Get one dataset's record by id (e.g. ds_card_txn).",
     "input_schema": {"type": "object", "properties": {"dataset_id": {"type": "string"}}, "required": ["dataset_id"]}},
    {"name": "submit_decision", "description": "Submit your final decision. Call this once, when done.",
     "input_schema": {
         "type": "object",
         "properties": {
             "verdict": {"type": "string", "enum": ["allow", "deny", "needs_review"]},
             "cited_policies": {"type": "array", "items": {"type": "string"}},
             "rationale": {"type": "string"},
         },
         "required": ["verdict", "cited_policies", "rationale"]}},
]

POLICY_SYSTEM = f"""You are the Policy agent in a data access review system.
You receive a structured access request. Decide allow, deny, or needs_review using ONLY these policies:
{POLICIES}
Look up the requester and dataset with your tools before deciding. Apply checks in this order and
stop at the first one that decides: POL-006, POL-004, POL-005, POL-001, POL-002, POL-003, then allow.
Cite only the policy ids that decided the verdict. Never cite a policy that is not listed above.
When done, call submit_decision.
For an allow, cite POL-002, and also POL-003 if the dataset contains PII."""


# ---------------- One loop, reused by every agent ----------------
def run_loop(agent_name, system, tools, functions, user_message, max_steps=6):
    messages = [{"role": "user", "content": user_message}]
    for step in range(max_steps):
        response = client.messages.create(model="claude-sonnet-5-5", max_tokens=2000,
                                          system=system, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name.startswith("submit"):        # this agent is done
                return block.input
            print(f"   [{agent_name} step {step + 1}] {block.name}({block.input})")
            result = functions[block.name](**block.input)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
        if results:
            messages.append({"role": "user", "content": results})
        else:
            messages.append({"role": "user", "content": "Please call your submit tool with your answer."})
    return {"error": f"{agent_name} did not finish in {max_steps} steps"}


# ---------------- The multi-agent pipeline ----------------
# ---------------- Risk rating (plain code, not AI) ----------------
def rate_risk(request):
    d = get_dataset(request.get("dataset_id", ""))
    if "error" in d:
        return "high"                                    # unknown data: assume the worst
    if d["classification"] == "Restricted" or (d["pii"] and request.get("action") != "read"):
        return "high"
    if d["pii"] or d["classification"] == "Confidential":
        return "medium"
    return "low"

# ---------------- Guardrails (plain code, cannot be talked out of) ----------------
VALID_POLICIES = {"POL-001", "POL-002", "POL-003", "POL-004", "POL-005", "POL-006"}


def audit_citations(decision):
    """Guardrail 1: a made-up policy id means we can't trust the decision."""
    fake = [p for p in decision.get("cited_policies", []) if p not in VALID_POLICIES]
    if fake:
        print("   GUARDRAIL: hallucinated citation", fake, "-> sending to a human")
        return {"verdict": "needs_review", "cited_policies": [],
                "rationale": f"Agent cited non-existent policies {fake}."}
    return decision


def hard_stops(request):
    """Guardrail 2: rules enforced in code no matter what the AI decided."""
    emp = get_employee(request.get("requester_id", ""))
    if "error" in emp or emp["status"] != "active":
        return {"verdict": "deny", "cited_policies": ["POL-006"],
                "rationale": "Hard stop: requester is not a verified active employee."}
    ds = get_dataset(request.get("dataset_id", ""))
    if "error" not in ds and ds["legal_hold"] and request.get("action") in ("export", "delete"):
        return {"verdict": "deny", "cited_policies": ["POL-004"],
                "rationale": "Hard stop: export/delete of data under legal hold."}
    return None
# ---------------- Orchestrator: the final decision is made in code ----------------
MODES = ["assist", "copilot", "autonomous"]


def decide(verdict, risk, mode):
    if mode == "assist":
        return "HUMAN"          # a human reviews everything
    if verdict == "deny":
        return "DENY"           # clear violations are denied automatically
    if verdict == "needs_review":
        return "HUMAN"          # the policy itself says a person must decide
    if mode == "copilot" and risk != "low":
        return "HUMAN"          # copilot only auto-approves low-risk requests
    return "APPROVE"


# ---------------- The multi-agent pipeline ----------------
def review(text):
    print("\nREQUEST:", text)
    request = run_loop("intake", INTAKE_SYSTEM, INTAKE_TOOLS, INTAKE_FUNCTIONS, text)
    print("   HANDOFF intake -> policy:", request)
    if "error" in request:      # fail safe: if an agent breaks, a human decides
        return {"verdict": "needs_review", "cited_policies": [], "rationale": request["error"]}, "high"

    decision = run_loop("policy", POLICY_SYSTEM, POLICY_TOOLS, POLICY_FUNCTIONS,
                        "Access request: " + json.dumps(request))
    if "error" in decision:
        return {"verdict": "needs_review", "cited_policies": [], "rationale": decision["error"]}, "high"

    decision = audit_citations(decision)               # guardrail 1
    stop = hard_stops(request)                         # guardrail 2
    if stop and decision["verdict"] != "deny":
        print("   GUARDRAIL OVERRIDE: AI said", decision["verdict"], "-> code says deny")
        decision = stop

    risk = rate_risk(request)
    print("   POLICY:", decision["verdict"], decision["cited_policies"], "| RISK:", risk)
    return decision, risk


if __name__ == "__main__":
    tests = [
        "Hi, this is Dana Lee. I need card transactions for a fraud investigation, 30 days.",
        "Tom from support needs customer profiles to help with a billing issue, 2 weeks.",
        "Marcus Hill wants card transactions to build a marketing campaign.",
        "Sam Ortiz needs card transactions for a fraud investigation.",
        "Jordan Kim needs the HR records for regulatory reporting.",
        "Lena Park wants to export the dispute cases for a legal review.",
        "Dana Lee needs card transactions to build a churn model.",
        "Priya needs pipeline logs for 400 days.",
        "Priya needs pipeline logs for 30 days to debug failed jobs.",
    ]

    # Run the agents ONCE per request, then see what each trust level would do with the same output
    results = []
    for t in tests:
        decision, risk = review(t)
        results.append((t, decision["verdict"], risk))

    print("\n" + "=" * 100)
    print(f"{'REQUEST':46}{'POLICY':14}{'RISK':8}{'ASSIST':9}{'COPILOT':9}AUTONOMOUS")
    for t, verdict, risk in results:
        a, c, au = (decide(verdict, risk, m) for m in MODES)
        print(f"{t[:45]:46}{verdict:14}{risk:8}{a:9}{c:9}{au}")
    print()
    for m in MODES:
        humans = sum(decide(v, r, m) == "HUMAN" for _, v, r in results)
        print(f"Human review load in {m}: {humans} of {len(results)}")