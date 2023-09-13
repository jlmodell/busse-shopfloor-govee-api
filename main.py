import httpx
from rich import print
import yaml
from fastapi import FastAPI, HTTPException
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import imaplib
import email
from email.header import decode_header
import re
from datetime import datetime
import sys
import json


DEBUG = sys.argv[-1] == "debug"


def init_config():
    fpath = r"config.yaml"
    load = yaml.safe_load(open(fpath))

    return load


config = init_config()


base_url = config["base_url"] or ""
api_key = config["api_key"] or ""
assert base_url != "", "base_url is empty"
assert api_key != "", "api_key is empty"


imap_host = config["imap"]["host"] or ""
imap_port = config["imap"]["port"] or 993
imap_email = config["imap"]["email"] or ""
imap_password = config["imap"]["password"] or ""
assert imap_host != "", "imap_host is empty"
assert imap_email != "", "imap_email is empty"
assert imap_password != "", "imap_password is empty"


test_keys = ["14"]
ids = {str(k): v for k,v in config["ids"].items()}
assert len(ids) > 0, "ids is empty"
assert all([k in ids for k in test_keys]), f"ids is missing one of {test_keys}"


discord_webhook_url = config["discord"]["webhook_url"] or ""
assert discord_webhook_url != "", "discord_webhook_url is empty"


# constants
ON = "on"
OFF = "off"


# ids are set in shopfloor
ANDONS = {
    "14": {
        "id": ids["14"],
        "name": "RECEIVING",
        "state": OFF,
        "last_changed": None,
    },
}


async def govee_info():
    global DEBUG

    async with httpx.AsyncClient() as client:
        r = await client.get(
            base_url,
            headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},  
        )

        temp = r.json()
        devices = temp["data"]["devices"]

        status = {}

        for andon_id, andon_dict in ANDONS.items():
            device_name = andon_dict["name"]
            for device in devices:
                if device["deviceName"] == device_name:
                    status[andon_id] = device
                    status[andon_id]["last_changed"] = andon_dict["last_changed"]
                    status[andon_id]["state"] = andon_dict["state"]

        if DEBUG:
            print(status)
    
    return status


async def discord_notification(andon_id: str, cmd: str, result: dict) -> None:
    global DEBUG, ANDONS, discord_webhook_url

    async with httpx.AsyncClient() as client:
        r = await client.post(
            discord_webhook_url,
            json={
                "content": f"ANDON ID \n-> {andon_id} ({ANDONS.get(andon_id).get('name', 'unknown')}) \n-> Received command '{cmd}' \n-> @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} \n-> Result {json.dumps(result, indent=4)}",
            },
        )


async def interact_with_govee_api(id: str, device_id: str, cmd: str):
    global ON, OFF, DEBUG, ANDONS, api_key, base_url

    async with httpx.AsyncClient() as client:
        r = await client.put(
            base_url + "control",
            headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
            json={
                "device": device_id,
                "model": "H6076",
                "cmd": {
                    "name": "turn",
                    "value": ON if cmd == ON else OFF,
                },
            },
        )

        await discord_notification(id, cmd, r.json())

        if cmd == ON:

            s = await client.put(
                base_url + "control",
                headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
                json={
                    "device": device_id,
                    "model": "H6076",
                    "cmd": {
                        "name": "color",
                        "value": {"r": 255, "g": 191, "b": 0},
                    },
                },
            )

        ANDONS[id]["state"] = ON if cmd == ON else OFF
        ANDONS[id]["last_changed"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    

async def checker_hook(andon_id: str):
    global DEBUG, ON, OFF, ANDONS

    if andon_id not in ANDONS:
        raise HTTPException(status_code=404, detail=f"Andon {andon_id} not found")

    with imaplib.IMAP4_SSL(host=imap_host) as m:
        m.login(imap_email, imap_password)
        _, messages = m.select()

        messages = int(messages[0])
        n = messages if messages < 15 else 15

        if messages == 0:
            return ANDONS[andon_id]

        for i in range(messages, messages - n, -1):
            _, msg = m.fetch(str(i), "(RFC822)")

            for response in msg:
                if isinstance(response, tuple):
                    msg = email.message_from_bytes(response[1])
                    subject = decode_header(msg["Subject"])[0][0]                    
                    
                    if isinstance(subject, bytes):
                        try:
                            subject = subject.decode("utf-8")
                        except UnicodeDecodeError:
                            subject = subject.decode("cp1252")
                        
                        except:
                            continue                    
                    
                    if DEBUG:
                        print(subject, re.search(rf"^{andon_id}", subject), re.search(r"ON$", subject))

                    if re.search(rf"^{andon_id}", subject, re.IGNORECASE):
                        if re.search(r"ON$", subject, re.IGNORECASE):
                            if ANDONS[andon_id]["state"] == ON:
                                return ANDONS[andon_id]
                            
                            await interact_with_govee_api(andon_id, ANDONS[andon_id]["id"], ON)

                            return ANDONS[andon_id]
                        else:
                            if ANDONS[andon_id]["state"] == OFF:
                                return ANDONS[andon_id]
                            
                            await interact_with_govee_api(andon_id, ANDONS[andon_id]["id"], OFF)

                            return ANDONS[andon_id]


app = FastAPI()


@app.on_event("startup")
async def hooks():
    await checker_hook("14")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        func=checker_hook,
        trigger="interval",
        args=["14"],
        seconds=30,
    )

    scheduler.start()


@app.get("/")
async def root():
    return await govee_info()


@app.get("/andon/{andon_id}")
async def andon(andon_id: str):
    global ON, OFF, DEBUG, ANDONS

    if andon_id not in ANDONS:
        raise HTTPException(status_code=404, detail=f"Andon {andon_id} not found")

    return ANDONS[andon_id]


@app.post("/andon/{andon_id}")
async def andon_interaction(andon_id: str, cmd: str):
    global ON, OFF, DEBUG, ANDONS

    if andon_id not in ANDONS:
        raise HTTPException(status_code=404, detail=f"Andon {andon_id} not found")

    if cmd not in [ON, OFF]:
        raise HTTPException(status_code=404, detail=f"Command {cmd} not found")

    await interact_with_govee_api(andon_id, ANDONS[andon_id]["id"], cmd)
    # await discord_notification(andon_id, cmd, {"status": "success"})

    return ANDONS[andon_id]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8989, reload=True)