import websocket
import ssl
import json
import datetime
import requests
import base64
import argparse
import configparser


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
                          "text": "Beam Update"
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


parser = argparse.ArgumentParser("ISIS Beam and Experiment Monitor")
parser.add_argument("config", help="Configuration file to use for data and webhook urls.", type=str)
#parser.add_argument("-i", "--instrument", help="Name of the instrument to monitor.", type=str, default=None)
parser.add_argument("-nc", "--notify_counts", help="Recived current to Notify on.", type=float, default=130)
args = parser.parse_args()

config = configparser.ConfigParser(interpolation=None)
config.read(args.config)

data_url = config["DATA"]["isis_websocket_url"]
teams_beam_url = config["WEBHOOKS"]["beam_teams_url"]
teams_experiment_url = config["WEBHOOKS"]["experiment_teams_url"]

ts1BeamCurrentPv = "AC:TS1:BEAM:CURR"
currentCountsPV = "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
currentNamePV = "IN:PEARL:DAE:WDTITLE"

params = {
    "type": "subscribe",
    "pvs": [ts1BeamCurrentPv, currentNamePV, currentCountsPV],
    }

class Beam_Status(object):
    def __init__(self, teams_beam_url, teams_experiment_url, counts_target):
        self.beam = 0
        self.beam_state = "off"
        self.last_beam_state = "off"
        self.beam_state_change_time = -100
        self.teams_beam_url = teams_beam_url
        self.teams_experiment_url = teams_experiment_url
        self.name = ""
        self.counts=-100
        self.counts_target=counts_target
        self.end_notified=False

    def get_state(self, beam):
        if beam == 0:
            return "off"
        if beam < 50:
            return "low"
        if beam < 100:
            return "medium"
        return "high"
        
    def on_open(self, ws):
        print('Opened Connection')
        ws.send(json.dumps(params))
    
    def on_close(self, ws, *args):
        print('Closed Connection', args)
        #send_teams_message(self.teams_url, "Websocket Closed")
    
    def on_message(self, ws, message):
        message=json.loads(message)
        time = datetime.datetime.now()
        if message["type"] == "update" and message["pv"] == ts1BeamCurrentPv:
            beam = message["value"]
            state = self.get_state(beam)
            print(f"{time}: Beam current is {beam} uA. Power is {state}.          ", end="\r")
            
            if (self.beam >= 0):
                if state != self.beam_state:
                    send_teams_message(self.teams_beam_url, f"{time}: Beam is now {state}. Current beam current is {beam} uA")

            
            self.beam = beam
            self.beam_state = state

        elif message["type"] == "update" and message["pv"] == currentNamePV:
            name = base64.b64decode(message["b64byt"]).decode().strip("\x00")
            if self.name != None and self.name != name:
                send_teams_message(self.teams_experiment_url, f"{time}: Detected new run start. {name}")
                self.counts = 0
            self.name = name

        elif message["type"] == "update" and message["pv"] == currentCountsPV:
                    counts = float(message["text"].split("/")[1])
                    self.counts=counts
                    if self.end_notified and (self.counts > self.counts_target/10) and (self.counts < (self.counts_target - 25)):
                        self.end_notified = False
                    if (self.counts > self.counts_target) and not self.end_notified:
                        send_teams_message(self.teams_experiment_url, f"{time}: {self.name} about to finish")
                        self.end_notified = True

        print(f"{time}: Beam current is {self.beam} uA. Power is {self.beam_state}. Currently running {self.name}, {self.counts}/{self.counts_target}.          ", end="\r")

            
    
    def on_error(self, ws, err):
      print("Got a an error: ", err)
      #send_teams_message(self.teams_url, f"There has been an error: {err}")


beam_status = Beam_Status(teams_beam_url, teams_experiment_url, args.notify_counts)
ws = websocket.WebSocketApp(data_url, on_open = beam_status.on_open, on_close = beam_status.on_close, on_message = beam_status.on_message,on_error=beam_status.on_error)
ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
