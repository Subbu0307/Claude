"""Chat with the marketplace agent from your terminal, no WhatsApp account needed.

    python -m marketplace.cli --phone 15550001111 --name Asha

Run it in two terminals with different --phone values to play buyer and seller against the same
database. Messages the agent would send to other users over WhatsApp are printed instead.
"""

import argparse

from .agent import MarketplaceAgent
from .config import Settings
from .store import Store
from .whatsapp import ConsoleNotifier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", required=True, help="Your simulated WhatsApp number, digits only")
    parser.add_argument("--name", help="Your WhatsApp profile name")
    args = parser.parse_args()

    settings = Settings()
    agent = MarketplaceAgent(
        Store(settings.db_path),
        notify=ConsoleNotifier().send_text,
        model=settings.model,
        effort=settings.effort,
        currency=settings.currency,
    )
    print(f"Chatting as +{args.phone}. Type 'reset' to start over, Ctrl-C to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text:
            print(f"\nmarketplace> {agent.handle_message(args.phone, text, args.name)}\n")


if __name__ == "__main__":
    main()
