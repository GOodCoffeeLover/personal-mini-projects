import requests
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_URL = os.getenv("TELEGRAM_BOT_URL")

url = "https://api.telegram.org/bot{token}/{method}".format(
        token = TELEGRAM_BOT_TOKEN,
        #method = "setWebhook",
        method="getWebhookinfo",
        #method = "deleteWebhook",
        )

data = {"url": TELEGRAM_BOT_URL}

def main():
    #r = requests.post(url, data=data)
    r = requests.get(url)
    print(r.json())

if __name__ == "__main__":
    main()
