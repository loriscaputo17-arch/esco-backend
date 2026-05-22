"""
Job batch: genera N journey AI variando vibe/duration/kind.
Lanciato manualmente (o da scheduler in futuro).

Uso:
    python -m app.jobs.generate_feed --count 30 --city-id <uuid> --user-id <uuid>
"""
import argparse
import time
import random
from app.models.journey import JourneyComposeRequest
from app.services.composer import compose_journey


# Combinazioni curate di parametri per dare varietà al feed
PRESETS = [
    ("relaxing", "afternoon", "places_only"),
    ("relaxing", "short", "places_only"),
    ("lively", "evening", "mix"),
    ("lively", "full_day", "mix"),
    ("cultural", "afternoon", "places_only"),
    ("cultural", "full_day", "mix"),
    ("romantic", "evening", "mix"),
    ("romantic", "short", "places_only"),
    ("discovery", "afternoon", "places_only"),
    ("discovery", "full_day", "mix"),
    (None, None, None),  # surprise me
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10, help="How many journeys to generate")
    parser.add_argument("--city-id", required=True, help="UUID of the city")
    parser.add_argument("--user-id", required=True, help="UUID of an authorized user (creator)")
    parser.add_argument("--delay", type=float, default=2.0, help="Sec between calls (rate limit)")
    args = parser.parse_args()

    print(f"\n🎨 Generating {args.count} AI journeys for city {args.city_id}\n")

    success = 0
    failures = 0

    for i in range(args.count):
        # Pick random preset
        vibe, duration, kind_pref = random.choice(PRESETS)
        surprise = vibe is None

        request_kwargs = {
            "user_id": args.user_id,
            "city_id": args.city_id,
            "surprise_me": surprise,
        }
        if not surprise:
            request_kwargs["vibe"] = vibe
            request_kwargs["duration"] = duration
            request_kwargs["kind_preference"] = kind_pref

        try:
            req = JourneyComposeRequest(**request_kwargs)
            label = f"surprise" if surprise else f"{vibe}/{duration}/{kind_pref}"
            print(f"[{i+1}/{args.count}] Composing ({label})...", end=" ", flush=True)

            result = compose_journey(req)
            print(f"✓ {result['title'][:50]}")
            success += 1

        except Exception as e:
            print(f"✗ FAILED: {str(e)[:120]}")
            failures += 1

        # Rate limit
        if i < args.count - 1:
            time.sleep(args.delay)

    print(f"\n📊 Done. {success} success, {failures} failures.")


if __name__ == "__main__":
    main()