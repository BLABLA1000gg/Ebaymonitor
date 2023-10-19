import requests
from bs4 import BeautifulSoup
import time

WEBHOOK_URL = 'YOUR_WEBHOOK_URL_HERE'
EBAY_URL = 'YOUR_EBAY_URL_HERE'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.182 Safari/537.36'
}

last_seen_links = set()  # Stores the last seen product links

def send_to_discord(title, link, price, description, image_url):
    data = {
        "embeds": [{
            "title": title,
            "description": description,
            "url": link,
            "color": 16711680,  # Color of the embed (Red in this case)
            "fields": [{
                "name": "Price",
                "value": price,
                "inline": True
            }],
            "thumbnail": {
                "url": image_url
            }
        }]
    }
    response = requests.post(WEBHOOK_URL, json=data)
    if response.status_code == 204:
        print(f"Offer {title} successfully sent to Discord.")
    else:
        print(f"Error sending the offer {title} to Discord.")


def monitor_ebay():
    global last_seen_links
    response = requests.get(EBAY_URL, headers=HEADERS)
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        listings = soup.select('.s-item')

        current_links = set()  # Stores the current product links for this run
        for listing in listings:
            link = listing.select_one('.s-item__link')['href']
            title = listing.select_one('.s-item__title').text
            price = listing.select_one('.s-item__price').text
            image_element = listing.select_one('.s-item__image-img')
            if image_element:
                image_url = image_element.get('src', image_element.get('data-src'))
            else:
                image_url = None
            current_links.add(link)

            if link not in last_seen_links:
                send_to_discord(title, link, price, '', image_url)

        last_seen_links = current_links  # Update the last seen links for the next run

    else:
        print(f"Error fetching the eBay page. Status-Code: {response.status_code}")

    time.sleep(60 * 5)  # Waits for 5 minutes 

print("Scanning for new products...")
if __name__ == "__main__":
    while True:
        monitor_ebay()
