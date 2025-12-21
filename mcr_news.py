#!/usr/bin/env python3
# Author: Max Pelly
# Created: 02-DEC-2025
# Licence: GNU AGPL 3


import requests
from time import sleep
import re
from datetime import datetime
import argparse
import configparser


def get_news(url):
    while True:
        responce = requests.get(url)
        if responce.status_code//100 == 2:
            feed = responce.text
            return re.sub("\\s+", " ", re.split(r"\r\n[0-9]{2}", feed)[0].replace("\r\n", ""))
        else:
            sleep(60)

def send_teams_message(url, message):
    payload = {
            "type":"message",
            "summary": message,
            "attachments":[
               {
                  "contentType":"application/vnd.microsoft.card.adaptive",
                  "summary": message,
                  "contentUrl":None,
                  "content":{
                      "$schema":"http://adaptivecards.io/schemas/adaptive-card.json",
                      "type":"AdaptiveCard",
                      "version":"1.2",
                      "fallbackText": message,
                      "summary": message,
                      "body":[
                          {
                          "type": "TextBlock",
                          "size": "Medium",
                          "weight": "Bolder",
                          "text": "MCR Update"
                          },
                          {
                          "type": "TextBlock",
                          "text": message,
                          "wrap": True
                          }
                      ]
                   }
                }
             ]
          }
    requests.post(url, json=payload)

parser = argparse.ArgumentParser("ISIS MCR News Monitor")
parser.add_argument("config", help="Configuration file to use for data and webhook urls.", type=str)
parser.add_argument("-n", "--notify_current", help="Send a notification for the current news. Otherwise waits untill new news is posted.", type=bool, default=False)
args = parser.parse_args()

config = configparser.ConfigParser(interpolation=None)
config.read(args.config)

mcr_news_url = config["DATA"]["mcr_news_url"]
if not mcr_news_url:
    print(f"MCR news url is required. Please edit config file. Current config file is {args.config}")
    raise(Exception())
teams_url = config["WEBHOOKS"]['news_teams_url']


if args.notify_current:
    old_news = ""    
else:
    old_news = get_news(mcr_news_url)
    print(old_news)

while True:
    try:
        new_news = get_news(mcr_news_url)
    except requests.ConnectionError as e:
        now = datetime.now()
        print(f"{now}: Connection Error")
        time.sleep(5*60)
    if new_news != old_news:
        old_news = new_news
        print(new_news)
        if teams_url:
            send_teams_message(teams_url, new_news)
    else:
        now = datetime.now()
        print(f"{now}: No new news", end="\r")
    sleep(60)
