import json
import os
from src.copytrader_paper import PaperCopyTrader


def main():
    required = [
        "reports/stable_wallets.json",
        "reports/enriched_wallets.json",
    ]
    missing = [x for x in required if not os.path.exists(x)]
    if missing:
        print(f"Missing required inputs: {', '.join(missing)}")
        return

    with open("reports/stable_wallets.json", "r") as f:
        stable_wallets = json.load(f)
    with open("reports/enriched_wallets.json", "r") as f:
        enriched_wallets = json.load(f)

    trader = PaperCopyTrader()
    state = trader.sync(stable_wallets, enriched_wallets)

    summary = {
        "selected_wallets": len(state.get("selected_wallets", [])),
        "open_positions": len(state.get("open_positions", [])),
        "closed_positions": len(state.get("closed_positions", [])),
        "available_capital": state.get("available_capital"),
        "last_run_at": state.get("last_run_at"),
    }

    with open("reports/paper_copytrader_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"Paper copytrader synced. Open positions: {summary['open_positions']}")


if __name__ == "__main__":
    main()
