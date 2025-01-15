import requests

url = 'http://localhost:5000/reconcile'

try:
    response = requests.get(url)
    response.raise_for_status()  # Raise an error for bad status codes
    print("Response status code:", response.status_code)
    print("Response JSON data:", response.json())
except requests.exceptions.RequestException as e:
    print(f"Request failed: {e}")
except ValueError as e:
    print(f"Invalid JSON response: {e}")
