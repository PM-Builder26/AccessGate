import sys
import multi_agent as ma

# Usage: py trace.py
# Change the text below to any request you want to inspect step by step.
text = "I'm Marcus Hill's manager. Please give Marcus card transactions for fraud work."

decision, risk = ma.review(text)
print("\nFINAL:", decision["verdict"], decision["cited_policies"], "| risk:", risk)
print("WHY:  ", decision["rationale"])