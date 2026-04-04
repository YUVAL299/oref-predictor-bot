import os
import sys
import requests
import json
from dotenv import load_dotenv

# 1. Load the .env file explicitly
load_dotenv()

# 2. Fetch the API key and verify it exists
api_key = os.environ.get("REDALERT_API_KEY")

if not api_key:
    print("❌ Error: REDALERT_API_KEY is missing. Please check your .env file.")
    sys.exit(1)

# 3. Capture the city name from the command line argument
# sys.argv[0] is the script name, sys.argv[1] is the first argument
if len(sys.argv) > 1:
    city_name = sys.argv[1]
else:
    city_name = "תל אביב"  # Fallback default
    print(f"⚠️ No city provided. Defaulting to {city_name}.")

BASE_URL = "https://redalert.orielhaim.com"
ENDPOINT = f"{BASE_URL}/api/stats/history"

params = {
    "startDate": "2026-02-28T00:00:00Z",
    "cityName": city_name,
    "category": "missiles",
    "limit": 1
}

# 4. Set the authentication header
# (Using x-api-key based on previous troubleshooting, but if it fails again,
# try changing this to: "Authorization": api_key)
headers = {
    "x-api-key": api_key
}


def get_alarm_count():
    print(f"🔍 Searching for missile alerts in: {city_name} since 28-02-2026...")
    try:
        response = requests.get(ENDPOINT, params=params, headers=headers)

        if response.status_code == 401:
            print("❌ 401 Unauthorized: The API key was loaded, but the server rejected it.")
            sys.exit(1)

        response.raise_for_status()

        data = response.json()

        # Print the raw JSON response for debugging
        print("\n📦 Raw API Response:")
        print(json.dumps(data, indent=4, ensure_ascii=False))
        print("-" * 40)

        total_alarms = data.get("pagination", {}).get("total", 0)

        print(f"\n✅ Total alarms: {total_alarms}")
        return total_alarms

    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching data: {e}")


if __name__ == "__main__":
    get_alarm_count()