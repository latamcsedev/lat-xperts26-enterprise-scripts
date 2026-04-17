import traceback
import os
import requests
from urllib3.exceptions import InsecureRequestWarning
import time

# Disable SSL verification globally via env vars
os.environ['REQUESTS_CA_BUNDLE'] = ""
os.environ['CURL_CA_BUNDLE'] = ""

# Suppress only the InsecureRequestWarning
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

results = []
i = 1
with open("creds.txt", "r") as creds_file:
    for cred in creds_file:
        try:
            print("------------")
            username = cred.strip().split(";")[0]
            password = cred.strip().split(";")[1]
            print("Starting: " + str(username))
            
            #get access token
            payload = {
                "username": username,
                "password": password,
                "client_id": "FortiSASE",
                "client_secret": "",
                "grant_type": "password"
            }
            headers = {
                "Content-Type": "application/json"
                #"Authorization": "Bearer YOUR_TOKEN"
            }
            url = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
            response = requests.post(url, json=payload, headers=headers)
            #print(username + " " + password)
            #print(response.text)
            #print("Auth token OK") if response.status_code == 200 else print("Auth token NOK")
            if response.status_code != 200:
                #retry 1 time
                print("Auth token retrying")
                time.sleep(10)
                response = requests.post(url, json=payload, headers=headers)
                #print("Auth token OK") if response.status_code == 200 else print("Auth token NOK")

            bearer_token = response.json()['access_token']

            #check access token
            #url = "https://portal.demo.fortisase.com/monitor-api/v1/infra/public-ip-feed"
            #headers = {
            #    "Authorization": f"Bearer {bearer_token}"
            #}
            #response = requests.get(url, headers=headers)
            #IPs = response.text.replace('\n', ' ')

            #get region using traffic out
            url = "https://portal.demo.fortisase.com/monitor-api/v1/traffic-history?type=Outbound"
            headers = {
                "Authorization": f"Bearer {bearer_token}"
            }
            #response = requests.get(url, headers=headers)
            #print("Get Traffic OK") if response.status_code == 200 else print("Get Traffic NOK")
            if response.status_code != 200:
                #retry 1 time
                print("Get Traffic retrying")
                time.sleep(10)
                response = requests.get(url, headers=headers)
                #print("Get Traffic OK") if response.status_code == 200 else print("Get Traffic NOK")
            #print(response.json())
            regions = []
            #if response.status_code == 200:
            #    for regionData in response.json()["data"]["datasets"]:
            #        regions.append(regionData["region"])

            

            #on-ramp
            #provision
            #{"regions":[{"name":"region1","connections":200}]} Vancouver
            #{"regions":[{"name":"region2","connections":200}]} France
            #{"regions":[{"name":"region3","connections":200}]} SanJose
            #{"regions":[{"name":"region4","connections":200}]} Ashburn
            #{"regions":[{"name":"region5","connections":200}]} Plano
            #{"regions":[{"name":"region6","connections":200}]} Madrid
            region = ""
            if "dfw-f3" in regions:
                region = "Plano"
            elif "iad-f1" in regions:
                region = "Ashburn"
            elif "mad-f1" in regions:
                region = "Madrid"
            elif "yvr-f2" in regions:
                region = "Vancouver"
#
            url = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec/on-ramp/connection_limit"
            headers = {
                "Authorization": f"Bearer {bearer_token}"
            }
            payload = {
                "regions":[{"name":"region5","connections":200}]
            }
            #response = requests.post(url, headers=headers, json=payload)
            #print("on-ramp: " + str(response.json()))
            #print("On-Ramp request OK") if response.status_code == 200 else print("On-Ramp request NOK")

            #validate
            url = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec"
            headers = {
                "Authorization": f"Bearer {bearer_token}"
            }
            response = requests.get(url, headers=headers)
            #print("on-ramp: " + str(response.json()))
            print("on-ramp: " + str(response.json()['data']['config_sites'][0]['resource_status']))
            print("on-ramp: " + str(response.json()['data']['state']))
            #print("On-Ramp request OK") if response.status_code == 200 else print("On-Ramp request NOK")

            #results.append(str(bearer_token) + ";" + region + ";" + str(regions))
            #with open ('partial_result.txt', 'a+') as file:
            #    file.write(f"{"API user: " + str(username) + "; On-Ramp chosen region: " + region + "; SASE PoPs: " + str(regions)}\n")

        except:
            traceback.print_exc()
            results.append("Error")


with open('result.txt', 'w') as file:
    for line in results:
        file.write(f"{line}\n")