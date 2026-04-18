import json, os
from src.wallet_transitions import TransitionEngine
def main():
    if not os.path.exists("reports/scored_wallets.json"): return
    with open("reports/scored_wallets.json", "r") as f: curr = json.load(f)
    e = TransitionEngine(); res = e.analyze_transitions(curr)
    with open("reports/wallet_transitions.json", "w") as f: json.dump(res, f, indent=2)
if __name__ == "__main__": main()
