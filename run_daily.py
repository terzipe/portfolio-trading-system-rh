"""
Daily monitoring run. Execute once each morning (e.g. via cron at 9:00 AM ET).
Runs all layers in sequence and fires alerts if needed.
"""
from monitor import layer0_universe, layer1_valuation, layer2_analytics, layer3_news, layer4_alerts


def main():
    print("[0/4] Building ticker universe (live RH positions + LS top longs)...")
    universe = layer0_universe.run()

    # Layer 1 still values held positions and saves the daily snapshot.
    # We pass the full universe to layer3 so news covers both held + watchlist.
    print("[1/4] Valuing held positions...")
    held = [p for p in universe if p.get("source") == "held"]
    valued = layer1_valuation.run(positions_override=held) if held else layer1_valuation.run()

    print("[2/4] Computing portfolio analytics...")
    analytics = layer2_analytics.run(valued)

    print("[3/4] Fetching macro context and news (Claude API)...")
    market = layer3_news.run(universe)   # full universe — held + LS watchlist

    print("[4/4] Running alerts and rendering dashboard...")
    alerts = layer4_alerts.run(valued, analytics, market)

    if alerts:
        print(f"\n{len(alerts)} alert(s) fired.")
    else:
        print("\nNo alerts today.")


if __name__ == "__main__":
    main()
