"""
Daily monitoring run. Execute once each morning (e.g. via cron at 9:00 AM ET).
Runs all four layers in sequence and fires alerts if needed.
"""
from monitor import layer1_valuation, layer2_analytics, layer3_news, layer4_alerts


def main():
    print("[1/4] Valuing positions...")
    valued = layer1_valuation.run()

    print("[2/4] Computing portfolio analytics...")
    analytics = layer2_analytics.run(valued)

    print("[3/4] Fetching macro context and news (Claude API)...")
    market = layer3_news.run(valued)

    print("[4/4] Running alerts and rendering dashboard...")
    alerts = layer4_alerts.run(valued, analytics, market)

    if alerts:
        print(f"\n{len(alerts)} alert(s) fired.")
    else:
        print("\nNo alerts today.")


if __name__ == "__main__":
    main()
