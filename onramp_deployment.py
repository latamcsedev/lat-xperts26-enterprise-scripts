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
            skip_BOR = False
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


            # Get PoP region using traffic out
            url = "https://portal.demo.fortisase.com/monitor-api/v1/traffic-history?type=Outbound"
            headers = {
                "Authorization": f"Bearer {bearer_token}"
            }
            response = requests.get(url, headers=headers)
            #print("Get Traffic OK") if response.status_code == 200 else print("Get Traffic NOK")
            
            if response.status_code != 200:
                #retry 1 time
                print("Get Traffic retrying")
                time.sleep(10)
                response = requests.get(url, headers=headers)
            #    #print("Get Traffic OK") if response.status_code == 200 else print("Get Traffic NOK")
            #print(response.json())
            regions = []
            if response.status_code == 200:
                for regionData in response.json()["data"]["datasets"]:
                    if regionData["region"] not in regions:
                        regions.append(regionData["region"])

            #on-ramp
            #provision
            #{"regions":[{"name":"region1","connections":200}]} Vancouver
            #{"regions":[{"name":"region2","connections":200}]} France
            #{"regions":[{"name":"region3","connections":200}]} SanJose
            #{"regions":[{"name":"region4","connections":200}]} Ashburn
            #{"regions":[{"name":"region5","connections":200}]} Plano
            #{"regions":[{"name":"region6","connections":200}]} Madrid
            # BOR region priority list
            region = ""
            if "dfw-f3" in regions:
                region = "region5"
            elif "iad-f1" in regions:
                region = "region4"
            elif "mad-f1" in regions:
                region = "region6"
            elif "yvr-f2" in regions:
                region = "region1"
            print(f"Region from Sec PoP: {regions}, BOR selected region: {region}")
            
            # Force deploy in Plano
            region = "region5"

            # Check if the BOR location already exists
            try:
                url = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec"
                headers = {
                    "Authorization": f"Bearer {bearer_token}"
                }
                response = requests.get(url, headers=headers)
                if "airport_name" in str(response.json()['data']):
                    print(f"A BOR location already exists, skipping")
                    skip_BOR = True
                    bgp_set = True
            except:
               # TBD - skip deployment for now
               traceback.print_exc()
               skip_BOR = True

            # check BGP type
            bgp_set = False
            bgp_missing = False
            try:
                url = "https://portal.demo.fortisase.com/resource-api/v1/private-access/network-configuration"
                headers = {
                    "Authorization": f"Bearer {bearer_token}"
                }
                response = requests.get(url, headers=headers)
                if not "bgp_design" in str(response.json()):
                    bgp_missing = True
                elif response.json()['data']['bgp_design'] == "loopback" and response.json()['data']['config_state'] == "success" and response.json()['data']['as_number'] == "65000":
                    print(f"BGP already set")
                    bgp_missing = False
                    bgp_set = True
                else:
                    bgp_set = False
                    bgp_missing = False
                    try:
                        if response.json()['data']['bgp_design'] != "loopback":
                            # need to delete the configuration
                            print(f"SPA BGP configuration going to be deleted")
                            url = "https://portal.demo.fortisase.com/resource-api/v1/private-access/network-configuration"
                            headers = {
                                "Authorization": f"Bearer {bearer_token}"
                            }
                            response = requests.delete(url,headers=headers)
                            time.sleep(5)
                            bgp_set = False
                            bgp_missing = True
                            print(f"Waiting for BGP to be deleted")
                            for i in range(0,300):
                                time.sleep(2)
                                response = requests.get(url, headers=headers)
                                if "config_state" in str(response.json()):
                                    print(f"State: {response.json()['data']['config_state']}")
                                else:
                                    break
                    except:
                        # failed to remove bgp configuration
                        print(f"BGP configuration error, skipping")
                        bgp_set = True
                        skip_BOR = True

            except:
                bgp_set = True
                skip_BOR = True
                print(f"Failed to set or verify BGP configuration, skipping")

            # Set BGP parameters
            try:
                if not bgp_set and not bgp_missing:
                    url = "https://portal.demo.fortisase.com/resource-api/v1/private-access/network-configuration"
                    headers = {
                        "Authorization": f"Bearer {bearer_token}"
                    }
                    payload = {
                        "bgp_router_ids_subnet": "172.17.0.0/24",
                        "as_number": "65000",
                        "recursive_next_hop": True,
                        "sdwan_rule_enable": False,
                        "sdwan_health_check_vm": "172.16.7.253"
                    }
                    response = requests.put(url, headers=headers, json=payload)
                    if response.status_code == 200:
                        print(f"BGP configuration set")
                    else:
                        print(f"Failed to set BGP configuration, skipping BOR deployment")
                        skip_BOR = True
                if bgp_missing:
                    url = "https://portal.demo.fortisase.com/resource-api/v1/private-access/network-configuration"
                    headers = {
                        "Authorization": f"Bearer {bearer_token}"
                    }
                    payload = {
                        "bgp_design": "loopback",
                        "bgp_router_ids_subnet": "172.17.0.0/24",
                        "as_number": "65000",
                        "recursive_next_hop": True,
                        "sdwan_rule_enable": False,
                        "sdwan_health_check_vm": "172.16.7.253"
                    }
                    response = requests.post(url, headers=headers, json=payload)
                    if response.status_code == 200:
                        print(f"BGP configuration set")
                    else:
                        print(f"Failed to set BGP configuration, skipping BOR deployment")
                        skip_BOR = True
            except:
                #TBD
                print(f"Failed to set BGP configuration, skipping BOR deployment")
                skip_BOR = True
            
            # Wait sometime for the configuration to complete
            # Options: Wait for BGP to finish and deploy BOR or Proceed and skip BOR
            wait_for_BGP = True
            #
            if wait_for_BGP:
                url = "https://portal.demo.fortisase.com/resource-api/v1/private-access/network-configuration"
                headers = {
                    "Authorization": f"Bearer {bearer_token}"
                }
                print("Waiting for BGP to be ready")
                for i in range(0,300):
                    time.sleep(2)
                    response = requests.get(url, headers=headers)
                    print(f"State: {response.json()['data']['config_state']}")
                    if response.json()['data']['bgp_design'] == "loopback" and response.json()['data']['config_state'] == "success" and response.json()['data']['as_number'] == "65000":
                        break
                # Check BGP
                response = requests.get(url, headers=headers)
                if response.json()['data']['bgp_design'] == "loopback" and response.json()['data']['config_state'] == "success" and response.json()['data']['as_number'] == "65000":
                    print(f"BGP configuration verified")
                else:
                    print(f"BGP configuration not updated, skipping BOR deployment")
                    skip_BOR = True
            else:
                skip_BOR = True

            # Deploy BOR location
            # dry run: skip_BOR = True
            # skip_BOR = True
            if not skip_BOR:
                print(f"Deploying BOR location on: {region}")
                url = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec/on-ramp/connection_limit"
                headers = {
                    "Authorization": f"Bearer {bearer_token}"
                }
                payload = {
                    "regions":[{"name": region,"connections":200}]
                }
                
                response = requests.post(url, headers=headers, json=payload)
                #print("on-ramp: " + str(response.json()))
                print("On-Ramp request OK") if response.status_code == 200 else print("On-Ramp request NOK")
            else:
                print(f"BOR already deployed on: {region}, skipping to verification")

            # Check BOR location status
            try:
                url = "https://portal.demo.fortisase.com/api/v1/security/sites/ipsec"
                headers = {
                    "Authorization": f"Bearer {bearer_token}"
                }
                response = requests.get(url, headers=headers)
                #print("on-ramp: " + str(response.json()))
                print("BOR Status: " + str(response.json()['data']['config_sites'][0]['resource_status']))
                print("BOR State: " + str(response.json()['data']['state']))
                #print("On-Ramp request OK") if response.status_code == 200 else print("On-Ramp request NOK")

                #results.append(str(bearer_token) + ";" + region + ";" + str(regions))
                #with open ('partial_result.txt', 'a+') as file:
                #    file.write(f"{"API user: " + str(username) + "; On-Ramp chosen region: " + region + "; SASE PoPs: " + str(regions)}\n")
                
            except:
                print("Failed to validate on-ramp status")

        except:
            traceback.print_exc()
            results.append("Error")


with open('result.txt', 'w') as file:
    for line in results:
        file.write(f"{line}\n")