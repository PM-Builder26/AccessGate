import json
import anthropic

client = anthropic.Anthropic()

# ---------------- Our "company systems": a tiny fake database ----------------
EMPLOYEES = [
    {"id": "EMP-101", "name": "Dana Lee",    "department": "Fraud",            "role": "Fraud Analyst",      "status": "active"},
    {"id": "EMP-102", "name": "Marcus Hill", "department": "Marketing",        "role": "Marketing Manager",  "status": "active"},
    {"id": "EMP-103", "name": "Priya Shah",  "department": "Data Engineering", "role": "Data Engineer",      "status": "active"},
    {"id": "EMP-104", "name": "Tom Becker",  "department": "Customer Support", "role": "Support Lead",       "status": "active"},
    {"id": "EMP-106", "name": "Jordan Kim",  "department": "Compliance",       "role": "Compliance Officer", "status": "active"},
    {"id": "EMP-107", "name": "Sam Ortiz",   "department": "Fraud",            "role": "Fraud Analyst",      "status": "terminated"},
    {"id": "EMP-108", "name": "Lena Park",   "department": "Legal",            "role": "Counsel",            "status": "active"},
    {"id": "EMP-109", "name": "Tom Nguyen",  "department": "Marketing",        "role": "Designer",           "status": "active"},
]

DATASETS = [
    {"id": "ds_card_txn", "name": "Card Transactions", "classification": "Confidential",
     "owner_department": "Fraud", "allowed_roles": ["Fraud Analyst", "Compliance Officer"],
     "pii": True, "legal_hold": False},
    {"id": "ds_customer_profiles", "name": "Customer Profiles", "classification": "Confidential",
     "owner_department": "Customer Support", "allowed_roles": ["Support Lead", "Compliance Officer"],
     "pii": True, "legal_hold": True},
    {"id": "ds_dispute_cases", "name": "Dispute Cases", "classification": "Confidential",
     "owner_department": "Legal", "allowed_roles": ["Counsel", "Compliance Officer", "Fraud Analyst"],
     "pii": True, "legal_hold": True},
    {"id": "ds_gl_ledger", "name": "General Ledger", "classification": "Confidential",
     "owner_department": "Finance", "allowed_roles": ["Financial Analyst"],
     "pii": False, "legal_hold": False},
    {"id": "ds_pipeline_logs", "name": "Pipeline Logs", "classification": "Internal",
     "owner_department": "Data Engineering", "allowed_roles": [],
     "pii": False, "legal_hold": False},
    {"id": "ds_employee_hr", "name": "Employee HR Records", "classification": "Restricted",
     "owner_department": "HR", "allowed_roles": [],
     "pii": True, "legal_hold": False},
]


# ---------------- Tools: plain Python functions Claude can ask us to run ----------------
def find_employee(query):
    q = query.lower()
    return [e for e in EMPLOYEES if q in e["name"].lower()]


def find_dataset(query):
    q = query.lower()
    return [d for d in DATASETS if q in d["name"].lower() or q in d["id"]]


TOOL_FUNCTIONS = {"find_employee": find_employee, "find_dataset": find_dataset}

TOOLS = [
    {"name": "find_employee",
     "description": "Search the employee directory by full or partial name. Returns matching employees.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_dataset",
     "description": "Search the data catalog by name or name fragment. Returns matching datasets.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "submit_request",
     "description": "Submit the final structured request. Call this once, when you are done.",
     "input_schema": {
         "type": "object",
         "properties": {
             "requester_id": {"type": "string"},
             "dataset_id": {"type": "string"},
             "action": {"type": "string", "enum": ["read", "export", "delete"]},
             "purpose": {"type": "string"},
             "duration_days": {"type": "integer"},
             "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
         },
         "required": ["requester_id", "dataset_id", "action", "purpose", "duration_days", "urgency"]}},
]

SYSTEM = """You are the Intake agent in a data access review system.
Turn a free-text access request into a structured request.
- Use find_employee to resolve the requester to an employee id. If several people match,
  use clues like department or role to pick one. If there is no single clear match, use "unknown".
- Use find_dataset to resolve the dataset to a dataset id. If unclear, use "unknown".
- action is read unless the request says export/download or delete/purge.
- Convert weeks or months to days. If no duration is stated, use 30.
- If the duration is indefinite (forever, permanently, ongoing), use 9999.
- urgency: high only if the request says urgent/ASAP/blocking; low if it says no rush; otherwise normal.
- Never guess and never invent ids. Record the request as asked, even if it looks like a bad request.
- When you are done, call submit_request.
- purpose: only the business reason (why they need it). If no reason is given, use "unknown". Never restate the request as the purpose.
- Never guess and never invent ids. Record the request as asked, even if it looks like a bad request.
- The named person is the requester, even if the request is written in the third person (e.g. "Jordan Kim needs..."). Only if the text explicitly says the sender is asking for someone else (e.g. "I'm Marcus's manager", "on behalf of") and the sender doesn't give their own name, use "unknown" for requester_id.
- If the request asks for more than one dataset, use "unknown" for dataset_id. Each dataset needs its own request.
- When you are done, call submit_request."""


# ---------------- The agent loop ----------------
def run_agent(text, max_steps=6):
    messages = [{"role": "user", "content": text}]

    for step in range(max_steps):
        response = client.messages.create(
            model="claude-sonnet-5-5", max_tokens=2000,
            system=SYSTEM, tools=TOOLS, messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_request":          # Claude says it's done
                print(f"   [step {step + 1}] submit_request")
                return block.input
            print(f"   [step {step + 1}] tool: {block.name}({block.input})")
            result = TOOL_FUNCTIONS[block.name](**block.input)   # WE run the tool
            print(f"            result: {result}")
            tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(result)})

        if tool_results:   # hand the results back to Claude and loop again
            messages.append({"role": "user", "content": tool_results})
        else:              # Claude replied in text instead of using a tool
            messages.append({"role": "user", "content": "Please call submit_request with your answer."})

    return {"error": f"Agent did not finish in {max_steps} steps"}   # guardrail: no infinite loops


requests = [
    "Hi, this is Dana Lee. I need card transactions for a fraud investigation, 30 days.",
    "Tom from support needs customer profiles to help with a billing issue, 2 weeks.",
    "Tom needs the pipeline logs.",
    "Lena wants to export the dispute cases.",
    "Sam Ortiz needs card transactions for 2 weeks.",
    "Kevin needs the ledger ASAP.",
   
]

if __name__ == "__main__":
    for r in requests:
        print("\nREQUEST:", r)
        print("RESULT: ", run_agent(r))