import httpx
import asyncio
from rich import print



async def govee_info():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            base_url,
            headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},  
        )

        print(r.json())
    
    return r.json()

if __name__ == "__main__":
    asyncio.run(govee_info())