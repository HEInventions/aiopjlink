<div align="center">

# aiopjlink

A modern Python asyncio PJLink library (Class I and Class II).

</div>

# What is aiopjlink?

Most projectors that have RJ45 ports on the back can be controlled via [PJLink](https://pjlink.jbmia.or.jp/english/).

PJLink is a unified standard for operating and controlling data projectors via `TCP/IP`, regardless of manufacturer.

PJLink is a communication protocol used by audiovisual equipment.

* Class 1 is the most common type of PJLink, and is used for basic commands such as power on/off, input selection, and adjusting volume.
* Class 2 is an extended version of the protocol that supports additional commands such as opening and closing the projector's lens cover, and is typically used by more sophisticated devices.

PJLink consists of [Class 1](https://pjlink.jbmia.or.jp/english/data/5-1_PJLink_eng_20131210.pdf) commands and queries, as well as [Class 2](https://pjlink.jbmia.or.jp/english/data_cl2/PJLink_5-1.pdf) notifications and extensions.


# Usage

Each "connection" to a projector is managed through a `PJLink` context manager.  Once this is connected, you access the different functions through a high level API (e.g. `conn.power.turn_off()`, `conn.lamps.hours()`, `conn.errors.query()`, etc).

For example, create a `PJLink` connection to the projector and issue commands:

```python
async with PJLink(address="192.168.1.120", password="secretpassword") as link:

    # Turn on the projector.
    await link.power.turn_on()

    # Wait a few seconds, then print out all the error information.
    await asyncio.sleep(5)
    print("errors = ", await link.errors.query())

    # Then wait a few seconds, then turn the projector off.
    await asyncio.sleep(5)
    await link.power.turn_off()
```

# Development

We use the [PDM package manager](https://pdm.fming.dev/latest/).

```bash
pdm install --dev  # install all deps required to run and test the code

pdm run lint  # check code quality
pdm run test  # check all test cases run OK

pdm publish  # Publish the project to PyPI
```

Other notes:
* There are more "pdm scripts" in the `.toml` file.
* Set the env variable `AIOPJLINK_PRINT_DEBUG_COMMS` to print debug comms to the console.

# Roadmap

Pull requests with test cases are welcome. There are still some things to finish, including:

* [ ] Search Protocol (§3.2)
* [ ] Status Notification Prototol (§3.3)
