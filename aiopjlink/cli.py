""" Basic CLI for testing.
"""

import sys
import asyncio
from aiopjlink.projector import PJLink


async def cli():

    # Parse
    ip = sys.argv[1] if len(sys.argv) >= 2 else "192.168.1.120"
    cmd = sys.argv[2] if len(sys.argv) >= 3 else "off"
    cmd = cmd.lower()

    # Send the mssage.
    async with PJLink(address=ip) as link:

        if cmd == "on":
            await link.power.turn_on()
            print(ip, "on")

        if cmd == "off":
            await link.power.turn_off()
            print(ip, "off")

        if cmd == "errors":
            print(ip, "errors = ", await link.errors.query())


if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    loop.run_until_complete(cli())
