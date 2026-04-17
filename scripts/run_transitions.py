import json
import os
from src.wallet_transitions import TransitionEngine
from src.persistence import PersistenceManager

def main():
    print("Running Wallet Transition Analysis...")

    if not os.path.exists("reports/scored_wallets.json"):
        print("Scored wallet data not found.")
        return

    with open("reports/scored_wallets.json", "r") as f:
        current_scores = json.load(f)

    engine = TransitionEngine()
    transitions = engine.analyze_transitions(current_scores)

    # Save transitions
    with open("reports/wallet_transitions.json", "w") as f:
        json.dump(transitions, f, indent=2)

    print(f"Transition Analysis complete. New: {len(transitions['new_wallets'])}, Rising: {len(transitions['rising_wallets'])}, Dropped: {len(transitions['dropped_wallets'])}")

if __name__ == "__main__":
    main()
