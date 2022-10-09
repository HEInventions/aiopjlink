""" projector.py

The `PJLink` class is a connection to a projector using the PJLink protocol.

To provide a "pythonic" API for the different PJLink commands, the
class `CommandGroup` is overriden and groups together related commands.

No state is kept inside the classes.
"""

import os
import re
import asyncio
import hashlib
from enum import Enum


""" Print out messages that are sent and recieved for debugging. """
PRINT_DEBUG_COMMS = bool(os.environ.get("AIOPJLINK_PRINT_DEBUG_COMMS", False))


class PJLinkException(Exception):
    """ Base exception for PJLink library issues. """
    pass


class PJLinkNoConnection(PJLinkException):
    """ Projector did not respond to the connection request. """
    pass


class PJLinkConnectionClosed(PJLinkException):
    """ Projector closed the connection. """
    pass


class PJLinkProtocolError(PJLinkException):
    """ Unexpected communication to or from the projector. """
    pass


class PJLinkUnexpectedResponseParameter(PJLinkException):
    """ Unable to parse a response parameter. """
    pass


class PJLinkPassword(PJLinkException):
    """ Invalid or absent password. """
    pass


class PJLinkProjectorError(PJLinkException):
    """ Projector raised an error when handling a command. """
    pass


class PJLinkERR1(PJLinkProtocolError):
    """ ERR 1, undefined command, as specified in (§2.2)
    """
    pass


class PJLinkERR2(PJLinkException):
    """ ERR 2, out of parameter, as specified in (§2.2)
    """
    pass


class PJLinkERR3(PJLinkException):
    """ ERR 3, unavailable at the current time or in the current projector state, as specified in (§2.2) """
    pass


class PJLinkERR4(PJLinkException):
    """ ERR 4, projector or display failure, as specified in (§2.2) """
    pass


class PJClass(Enum):
    """ Communication protocol message version.

    Class 1 is the most common type of PJLink, and is used for basic commands such as
    power on/off, input selection, and adjusting volume.

    Class 2 is an extended version of the protocol that supports additional commands such
    as opening and closing the projector's lens cover, and is typically used by more sophisticated devices.
    """

    ONE = '1'
    """ PJLink Class 1 command. """

    TWO = '2'
    """ PJLink Class 2 command. """


class PJLink:
    """ Manages a PJLink connection to a projector.

    Usage:

        >>> async with PJLink(address='192.168.100.100', password='secret') as link:
        >>>     await link.power.turn_off()
        >>>     time.sleep(4)
        >>>     await link.power.turn_on()

    """

    C1 = PJClass.ONE
    C2 = PJClass.TWO

    def __init__(self, address, port=4352, password=None, timeout=4, encoding='utf-8'):
        self._reader = None
        self._writer = None
        self._address = address
        self._port = port
        self._encoding = encoding
        self._timeout = timeout
        self._password = password

        # Add the different API namespaces.
        self.info = Information(self)
        self.power = Power(self)
        self.sources = Sources(self)
        self.mute = Mute(self)
        self.errors = Errors(self)
        self.lamps = Lamp(self)
        self.filter = Filter(self)
        self.freeze = Freeze(self)
        self.microphone = Volume(self, 'MVOL')
        self.speaker = Volume(self, 'SVOL')

    async def wait_for_notification(self):
        raise NotImplementedError('class 2 method not supported')

    async def __aenter__(self):
        """ Open a connection to the projector and authenticate. """
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._address, self._port),
                timeout=self._timeout
            )
        except asyncio.exceptions.TimeoutError:
            raise PJLinkNoConnection("timeout - projector did not accept the connection in time")
        except OSError as err:
            raise PJLinkNoConnection(f"os timeout - {str(err)}")

        # An authentication procedure is executed once after each establishment of TCP/IP connection.
        # The authentication procedure involves a password verification process.
        # See https://pjlink.jbmia.or.jp/english/data_cl2/PJLink_5-1.pdf SECTION 5

        # Projector sends first message to identify itself as PJLINK.
        # data = await self._raw_read(n_bytes=9)
        try:
            data = await self._read_next()
        except asyncio.exceptions.TimeoutError:
            raise PJLinkProtocolError('projector did not send a welcome message')
        if len(data) < 9:
            raise PJLinkProtocolError('unexpected opening header message from projector - too short')

        auth_header, auth_enabled, auth_close = data[:7], data[7], data[8]
        if auth_header.upper() != 'PJLINK ':
            raise PJLinkProtocolError('unexpected opening header message from projector - not PJLink')

        # Connection requires no auth: `PJLINK 0\r`.
        if auth_enabled == '0':
            return self

        # Connection requires auth: `PJLINK 1 <token>`.
        if auth_enabled != '1' and auth_close != ' ':
            raise PJLinkProtocolError('unexpected opening security message from projector - unrecognised auth method')

        # Check we have a password specified.
        if self._password is None:
            raise PJLinkPassword('password required')

        # Read the random number used to salt the password (excluding the terminating `\r`).
        token = data[9:-1]
        passcode = (token + self._password).encode('utf-8')
        passcode_md5 = hashlib.md5(passcode).hexdigest()

        # The PJLINK authentication procedure requires the password and the first command to be
        # transmitted together.  We send a power status request for simplicity.
        self._writer.write(bytearray(passcode_md5, encoding=self._encoding))
        self._writer.write(b'%1POWR ?\r')
        await self._writer.drain()

        # Read the first few bytes of the response - check for failed auth.
        # ERRA represents ERR or authorization.
        response = await self._read_next()
        if response.upper() == 'PJLINK ERRA\r':
            raise PJLinkPassword('authentication failed')
        self._parse_response(response, expect_command='POWR', expect_pjclass=PJClass.ONE)
        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        """ Close an open connection to the projector. """
        try:
            self._writer.close()
        finally:
            self._reader = None
            self._writer = None

    async def _read_next(self):
        """ Read data until the next terminator (CR) and return the
        message (including CR) as a decoded string."""
        try:
            raw = await asyncio.wait_for(self._reader.readuntil(b'\r'), self._timeout)
        except asyncio.IncompleteReadError as err:
            raise PJLinkConnectionClosed('projector closed the connection') from err
        return raw.decode(self._encoding)

    async def transmit(self, command, param, pjclass: PJClass):
        """ Send a command and get a response.

        Commands and responses are listed here:
        https://pjlink.jbmia.or.jp/english/data_cl2/PJLink_5-1.pdf

        Args:
            command (str): Four letter command (e.g. "POWR").
            param (str): Parameter that accompanies the command (e.g. "?")
            pjclass (str, optional): The PJLink command class. Defaults to '1'.

        Raises:
            PJLinkERR1, PJLinkERR2, PJLinkERR3, PJLinkERR4

        Returns:
            str: The response to the issued command.
        """
        # Generate the command string.
        cstring = self._format_command(command, param, pjclass)

        # Send the command string.
        cbytes = bytearray(cstring, self._encoding)
        if PRINT_DEBUG_COMMS:
            print("🚢", cbytes)
        self._writer.write(cbytes)
        await self._writer.drain()

        # Get the response.
        response = await self._read_next()

        # Parse the response.
        _, param = PJLink._parse_response(response, expect_command=command, expect_pjclass=pjclass)
        return param

    @staticmethod
    def _format_command(command, param, pjclass: PJClass):
        pjclass = PJClass(pjclass)
        if not command.isupper():
            raise PJLinkProtocolError('command is not uppercase')
        if len(command) != 4:
            raise PJLinkProtocolError('command is not 4 bytes')
        if len(param) > 128:
            raise PJLinkProtocolError('command param is larger than 128 bytes')
        sep = ' '
        return f'%{pjclass.value}{command}{sep}{param}\r'

    @staticmethod
    def _parse_response(data, expect_command=None, expect_pjclass=PJClass.ONE):
        # NOTE: Postels robustness principle - be conservative in what you do, be liberal in what you accept from others
        if PRINT_DEBUG_COMMS:
            print('➡️ ', data)
        expect_pjclass = PJClass(expect_pjclass)

        # Check header and class version.
        header, version = data[0], data[1]
        if header != '%':
            raise PJLinkProtocolError('unexpected response header')
        if version != expect_pjclass.value:
            raise PJLinkProtocolError('unexpected response protocol class')

        # Grab the command body, separator, and param.
        command = f'{data[2:6]}'.upper()
        sep = data[6]
        param = data[7:-1]

        # Check them for correctness.
        if sep != '=':
            raise PJLinkProtocolError('unexpected response separator')
        if expect_command is not None and command != expect_command:
            raise PJLinkProtocolError('unexpected response command')

        # Handle for protocol and projector errors.
        param_u = param.upper()
        if param_u == 'ERR1':
            raise PJLinkERR1('unsupported command')
        elif param_u == 'ERR2':
            raise PJLinkERR2('out of parameter')
        elif param_u == 'ERR3':
            raise PJLinkERR3('unavailable in the current state')
        elif param_u == 'ERR4':
            raise PJLinkERR4('projector or display failure')

        return command, param


class CommandGroup:
    """ Base class for related groups of PJLink functionality.
    """

    def __init__(self, link: PJLink):
        self._link = link

    async def _transmit_ok(self, command, param, pjclass):
        """ Transmit a command and check the response is OK. """
        response = await self._link.transmit(command, param, pjclass)
        if response.upper() != 'OK':
            raise PJLinkUnexpectedResponseParameter('expected OK response')


class Power(CommandGroup):
    """ Control and query the power state of the projector lamp. """

    class State(Enum):
        """ PJLink projector lamp states (combining §4.1 and §4.2). """
        OFF = '0'
        ON = '1'
        COOLING = '2'
        WARMING = '3'

        def __bool__(self):
            """ Truthy states for `on` and `warming`, falsy states for `off` and `cooling`. """
            return self in (Power.State.ON, Power.State.WARMING)

    ON = State.ON
    OFF = State.OFF

    async def set(self, state: State, pjclass=PJClass.ONE):
        """ Send a power control instruction to power the projector lamp on or off. """
        state = Power.State(state)
        if state in (Power.State.COOLING, Power.State.WARMING):
            raise ValueError('expected Power.State.ON or Power.State.OFF')
        await self._transmit_ok('POWR', state.value, pjclass=pjclass)

    async def get(self, pjclass=PJClass.ONE):
        """ Request the power status of the projector. """
        return Power.State(await self._link.transmit('POWR', '?', pjclass=pjclass))

    async def turn_on(self):
        """ Power the projector on. """
        await self.set(Power.State.ON)

    async def turn_off(self):
        """ Power the projector off. """
        await self.set(Power.State.OFF)


class Sources(CommandGroup):
    """ Control and query projector input sources. """

    class Mode(Enum):
        """ Display input source modes (§4.3)."""
        RGB = '1'
        VIDEO = '2'
        DIGITAL = '3'
        STORAGE = '4'
        NETWORK = '5'
        INTERNAL = '6'
        """ Class 2 only. """

    async def set(self, mode, index, pjclass=PJClass.ONE):
        """ Get the current source input selection (§4.3).
        """
        mode, index = self._check_mode_index(mode, index)
        return await self._transmit_ok(command='INPT', param=f'{mode.value}{index}', pjclass=pjclass)

    async def get(self, pjclass=PJClass.ONE):
        """ Get the current source input selection (§4.4).
        """
        values = await self._link.transmit(command='INPT', param='?', pjclass=pjclass)
        if len(values) != 2:
            raise PJLinkUnexpectedResponseParameter('expected 2 INPT response characters')
        return Sources.Mode(values[0]), values[1]

    def _check_mode_index(self, mode, index):
        """ Ensure a mode and index pair are valid. """
        mode = Sources.Mode(mode)
        index = f'{index}'
        if len(index) != 1:
            raise ValueError('index must be a single character (1-9 for Class 1, and 1-9A-Z for Class 2)')
        return mode, index

    async def available(self, pjclass=PJClass.ONE):
        """ List all the available input sources (§4.9).

        Returns:
            A list of available input sources in the format: (Sources.Mode, index str).  For example:
                [(<Mode.RGB: '1'>, '1'), (<Mode.DIGITAL: '3'>, '1'), ...]
        """
        sources = await self._link.transmit('INST', '?', pjclass)
        sources = sources.split(' ')
        return [(Sources.Mode(first), second) for first, second in sources]

    async def get_source_name(self, mode, index):
        """ Get the name of a given input source (§4.17).
        :param mode Sources.Mode: The input mode to select.
        :param index str: A single character.
        """
        mode, index = self._check_mode_index(mode, index)
        return await self._link.transmit(command='INNM', param=f'?{mode.value}{index}', pjclass='2')

    async def available_with_names(self):
        """ List all the available input sources with names (§4.17).
        Returns:
            A list of available input sources in the format (Source.Mode, index str, name str).
            For example:
                [(<Mode.RGB: '1'>, '1', 'Computer'), (<Mode.DIGITAL: '3'>, '1', 'DVI-D'), ...]
        """
        sources = await self.available(pjclass=PJClass.TWO)
        output = []
        for mode, index in sources:
            try:
                name = await self.get_source_name(mode, index)
            except PJLinkERR2:
                name = None
            output.append((mode, index, name))
        return output

    async def resolution(self):
        """ Get the current projector resolution (§4.18)
        Returns:
            (x:int, y:int) tuple: Horizontal and vertical resolutions of input signal respectively.
        """
        response = await self._link.transmit('IRES', '?', PJClass.TWO)
        if response == '-':
            raise PJLinkProjectorError('no signal input')
        if response == '*':
            raise PJLinkProjectorError('unknown signal')

        # Convert each axis to an integer.
        try:
            resolution = [int(dim) for dim in re.split('x', response, flags=re.IGNORECASE)]
            return tuple(resolution)
        except ValueError:
            raise PJLinkUnexpectedResponseParameter('unable to parse resolution')

    async def recommended_resolution(self):
        """ Get the current recommended resolution (§4.19)
        Returns:
            (x, y) tuple: Horizontal and vertical resolutions of input signal respectively.
        """
        response = await self._link.transmit('RRES', '?', PJClass.TWO)
        try:
            resolution = [int(dim) for dim in re.split('x', response, flags=re.IGNORECASE)]
            return tuple(resolution)
        except ValueError:
            raise PJLinkUnexpectedResponseParameter('unable to parse resolution')


class Mute(CommandGroup):
    """ Control audio and visual track mute status (§4.5, §4.6).

    If the mute function is individually executed or cancelled for the models
    that do not have audio or video mute functions, "ERR 2" (out of parameter range) is returned.
    """

    async def status(self):
        """ Current (video, audio) track mute status returned as two booleans (§4.6).

        Returns:
            tuple(video: bool, audio: bool): True if the track is muted.  False if not.
        """
        status = await self._link.transmit('AVMT', '?', pjclass=PJClass.ONE)
        if status == '11':
            return True, False
        elif status == '21':
            return False, True
        elif status == '31':
            return True, True
        elif status == '30':
            return False, False
        else:
            raise PJLinkUnexpectedResponseParameter('unexpected mute response')

    async def video(self, muted: bool):
        """ Set if the video track should be muted (True to mute, False to unmute). """
        cmd = '1' if muted is True else '0'
        await self._transmit_ok('AVMT', f'1{cmd}', pjclass=PJClass.ONE)

    async def audio(self, muted: bool):
        """ Set if the audio track should be muted (True to mute, False to unmute). """
        cmd = '1' if muted is True else '0'
        await self._transmit_ok('AVMT', f'2{cmd}', pjclass=PJClass.ONE)

    async def both(self, muted: bool):
        """ Set if the AV tracks should be muted (True to mute, False to unmute). """
        cmd = '1' if muted is True else '0'
        await self._transmit_ok('AVMT', f'3{cmd}', pjclass=PJClass.ONE)

    async def set(self, video: bool, audio: bool):
        """ Enable or disable mute for each track (call mirrors output of `status`).
        :param video (bool): True to mute. False to unmute. None to skip.
        :param audio (bool): True to mute. False to unmutes. None to skip.
        """
        # Skip non-specified condition.
        if video is None and audio is None:
            return

        # Fully specified conditions.
        if video is True and audio is True:
            await self.both(True)
        elif video is False and audio is False:
            await self.both(False)

        # Partially specified conditions.
        else:
            if video is not None:
                await self.video(video)
            if audio is not None:
                await self.audio(audio)


class Errors(CommandGroup):
    """ Provide information about errors occuring within the projector (§4.7).
    """
    class Category(Enum):
        """ The different types of error returned according to (§4.7). """
        FAN = 'fan'
        LAMP = 'lamp'
        TEMP = 'temperature'
        COVER = 'cover'
        FILTER = 'filter'
        OTHER = 'other'

    class Level(Enum):
        """ Error level for each `Category` (§4.7). """
        OK = '0'
        WARN = '1'
        ERROR = '2'

    async def query(self):
        """ Query the projecteor for the latest error status
        information for each of the error categories (§4.7).

        Returns:
            dict[Category]: Level: Table of error categories to states.
        """
        errors = await self._link.transmit('ERST', '?', pjclass=PJClass.ONE)
        try:
            fan, lamp, temp, cover, filt, other = errors
        except ValueError:
            raise PJLinkUnexpectedResponseParameter('unexpected number of error types reported')
        try:
            return {
                Errors.Category.FAN: Errors.Level(fan),
                Errors.Category.LAMP: Errors.Level(lamp),
                Errors.Category.TEMP: Errors.Level(temp),
                Errors.Category.COVER: Errors.Level(cover),
                Errors.Category.FILTER: Errors.Level(filt),
                Errors.Category.OTHER: Errors.Level(other),
            }
        except ValueError as err:
            raise PJLinkUnexpectedResponseParameter('unknown error level') from err


class Lamp(CommandGroup):
    """ Status information about the projector light sources (§4.8).

        According to the spec the "usage time of lamp is always 0 when it is
        not counted by the projector."
    """
    class State(Enum):
        OFF = '0'
        ON = '1'

    async def status(self):
        """ Query the current lamp hours and lamp statuses.
        There may be more than one lamp in some projectors, so this is returned
        as a list.
        Returns:
            [(hours:int, state:Lamp.State)]: List of lamp hours and states for each lamp.
        """
        # Express a special meaning for ERR1 (§4.8).
        try:
            response = await self._link.transmit('LAMP', '?', pjclass=PJClass.ONE)
        except PJLinkERR1:
            raise PJLinkERR1('no lamp')

        # Split the response by " " and then pair up all the numbers from
        # the list in groups of 2.  If there are any remainders, raise an error.
        # See: https://docs.python.org/3/library/itertools.html (grouper)
        # This takes a line like: "1000 1 50 0" and breaks it into pairs:
        #   (1000, 1), (50, 0)
        # Such that these can then be remapped into our high level interface.
        try:
            # Python 3.9 solution.
            pairs = []
            numbers = response.split(' ')
            for i in range(0, len(numbers), 2):
                pairs.append(numbers[i:i + 2])

            # Python 3.10 solution.
            # pairs = zip(*[iter(response.split(' '))] * 2, strict=True)
            # pairs = [pair for pair in pairs]#

            # Remap the statuses to integers and our lamp state enum.
            return [(int(hours), Lamp.State(state)) for hours, state in pairs]
        except ValueError as err:
            raise PJLinkUnexpectedResponseParameter('unparsable lamp status') from err

    async def hours(self):
        """ How long has the first lamp been on (hour, integer).

        According to the spec (§4.8) the "usage time of lamp is always 0 when it is
        not counted by the projector."
        """
        return (await self.status())[0][0]

    async def replacement_models(self):
        """ Get the lamp replacement models listed in the projector.
        There may be more than one model number, so they are returned in a list.
        """
        models = await self._link.transmit('RLMP', '?', pjclass=PJClass.TWO)
        return [m for m in models.split(' ') if m]


class Filter(CommandGroup):
    """ Status information about the projector filters (§4.20, §4.22).
    """

    async def hours(self):
        """ Query the filter usage time (§4.20).
        Filter usage time is always 0 when it is not counted by the projector.
        """
        # Request the value.
        try:
            return int(await self._link.transmit('FILT', '?', pjclass=PJClass.TWO))

        # Express a special meaning for ERR1 (§4.20).
        except PJLinkERR1:
            raise PJLinkERR1('no filter')

        # Parse issue.
        except ValueError:
            raise PJLinkUnexpectedResponseParameter('filter usage not parsable')

    async def replacement_models(self):
        """ Get the filter replacement models listed in the projector (§4.22).
        There may be more than one model number, so they are returned in a list.
        """
        models = await self._link.transmit('RFIL', '?', pjclass=PJClass.TWO)
        return [m for m in models.split(' ') if m]


class Freeze(CommandGroup):
    """ Controls freezing and unfreezing the current frame (§4.25, §4.26).
    """

    async def set(self, freeze: bool):
        """ Freeze or unfreeze the screen (§4.25). """
        cmd = '1' if bool(freeze) else '0'
        await self._transmit_ok('FREZ', cmd, pjclass=PJClass.TWO)

    async def get(self):
        """ Returns True if the screen is currently frozen, and False if not §4.26. """
        response = await self._link.transmit('FREZ', '?', pjclass=PJClass.TWO)
        if response == '0':
            return False
        elif response == '1':
            return True
        else:
            raise PJLinkUnexpectedResponseParameter('unexpected freeze state')


class Volume(CommandGroup):
    """ Controls a xVOL style command (e.g. for speakers and microphones) as
    defined in (§4.23, §4.24).

    According to the spec:
        "As for a specification to increase the microphone volume by one level when it
        is in the maximum state, and a specification to decrease the microphone
        volume by one level when it is in the minimum state, the response
        for a normal case is returned."

    Volume related to audio output (audio out, built-in speaker in equipment
    model, etc.) is referred to as the speaker volume.

    Volume related to voice input (audio in, microphone terminal to be input
    to the model, etc.) is referred to as the microphone volume.
    """

    def __init__(self, link, instruction):
        super().__init__(link)
        self.instruction = instruction

    async def turn_up(self):
        """ Increase the volume by one unit. """
        await self._transmit_ok(self.instruction, '1', pjclass=PJClass.TWO)

    async def turn_down(self):
        """ Decrease the volume by one unit. """
        await self._transmit_ok(self.instruction, '0', pjclass=PJClass.TWO)


class Information(CommandGroup):
    """ Gathers information about the projector.
    """

    async def table(self):
        """ Collect a table of all the different information available
        from this projector.  If the projector responds, an empty string is
        returned, but if it throws an error, `None` is returned.

        See the code for the dictionary entries.
        """

        # Helper to ensure it is always returned regardless of the exception.
        async def _safe(method):
            try:
                return await method()
            except Exception:
                return None

        # Table.
        return {
            "software_version": await _safe(self.software_version),
            "serial_number": await _safe(self.serial_number),
            "pjlink_class": await _safe(self.pjlink_class),
            "other": await _safe(self.other),
            "product_name": await _safe(self.product_name),
            "manufacturer_name": await _safe(self.manufacturer_name),
            "projector_name": await _safe(self.projector_name),
        }

    async def software_version(self):
        """ Request software version of the projector (§4.16).
        The version information of the software defined by the manufacturer is indicated.
        Version information can be expressed in any way.

        Returns:
            str: The version string.
        """
        return await self._link.transmit('SVER', '?', PJClass.TWO)

    async def serial_number(self):
        """ Request the projector serial number (§4.15).
        The serial number information defined by the manufacturer is indicated.

        Returns:
            str: The serial number string.
        """
        return await self._link.transmit('SNUM', '?', PJClass.TWO)

    async def pjlink_class(self, pjclass=PJClass.ONE):
        """ Get projectors PJLink class number as a `PJClass` enumeration (§4.14) """
        try:
            return PJClass(await self._link.transmit('CLSS', '?', pjclass=pjclass))
        except ValueError as err:
            raise PJLinkUnexpectedResponseParameter('unexpected PJLink class') from err

    async def other(self):
        """ Query the projector for other information about the projector/display
        described by the manufacture. Defined as in (§4.13).

        If there is no other information, this returns an empty string.
        """
        return await self._link.transmit('INFO', '?', PJClass.ONE)

    async def product_name(self):
        """ Get product name information string (e.g. EPSON PU1007B/PU1007W) as in (§4.12).

        If there is no information, this returns an empty string.
        """
        return await self._link.transmit('INF2', '?', PJClass.ONE)

    async def manufacturer_name(self):
        """ Get manufacturer name information string (e.g. EPSON) as in (§4.11).

        If there is no information, this returns an empty string.
        """
        return await self._link.transmit('INF1', '?', PJClass.ONE)

    async def projector_name(self):
        """ Get projector name information string (e.g. EBB13648) as in (§4.10).

        If there is no information, this returns an empty string.
        """
        return await self._link.transmit('NAME', '?', PJClass.ONE)
