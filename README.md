eBay Product Monitor
This script monitors  eBay url listings and sends it to a discord webhooks when new products are listed.

Features:
Monitors eBay for specific listings based on a given URL.
Sends a nwebhooks when new products are detected.
Uses BeautifulSoup for web scraping.
Prerequisites:
Python 3
pip install requests
pip install beautifulsoup4
Setup:


Copy code
pip install -r requirements.txt
Replace 'YOUR_WEBHOOK_URL_HERE' in the script with your Discord webhook URL.

Replace 'YOUR_EBAY_URL_HERE' in the script with the desired eBay search URL.

Run the script:

The script is set to check eBay every 5 minutes. Adjust the sleep time if needed.
Use responsibly and avoid sending excessive requests to eBay.
Contributing:
If you'd like to contribute, please fork the repository and use a feature branch. Pull requests are warmly welcome.


You can input a link and it will monitor the link for new products
for example i use this link to find cheap macbooks to flip them: "https://www.ebay.de/sch/i.html?_from=R40&_nkw=Macbook&_sacat=0&_sop=2&LH_BIN=1&LH_ItemCondition=7000%7C3000&_blrs=recall_filtering&_udlo=20&rt=nc&LH_PrefLoc=1&_ipg=240"

Important note:
use Basic words like Macbook,Notebook,Computer and so on...
Sort for the lowest price first.
click on only show "buy now".
for the best expirence scroll to the button of the page an pick 240 or more articels on each page.
