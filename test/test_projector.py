import asyncio
import hashlib
import unittest
import platform
import contextlib

import aiopjlink.projector as aiopjlink


# Allow the loop to close cleanly on Windows.
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class PJLinkTestFrameworkError(Exception):
    """ Base exception for problems with the test framework. """
    pass


class NoClientMessage(PJLinkTestFrameworkError):
    """ The test case fails because the test case didn't send a client message. """
    pass


class UnexpectedClientMessage(PJLinkTestFrameworkError):
    """ The test case fails because the test case expected something the client didn't send. """
    pass


class NoExpectations(PJLinkTestFrameworkError):
    """ The test case fails because the mock projector recieves a command before it is told how to respond. """
    pass


class PJLinkServerProtocol(asyncio.Protocol):
    """
    Provides a method for mocking a projector, including a full socket
    stack to provide a proper simulation of a projector for the client - including
    erroneous behaviours.

    This class has some nuance - check it here.
    https://docs.python.org/3/library/asyncio-protocol.html
    """

    def __init__(self, loop, debug=False):

        # Event loop of the current test case.
        self.loop = loop

        # Should send and recieve messages be printed.
        self.debug = debug

        # An opening message is sent as soon as a connection is established.
        self._opening_message = None

        # What the projector should expect to recieve / do next (as defined by the test case).
        self._expected_events = []

        # Buffer fore incoming messages until \r.
        self._recv_buffer = b''

    def _write(self, data):
        """ Send data to the client. """
        if self.debug:
            print("PJLinkServerProtocol SEND:", data)
        self.transport.write(data)

    def connection_made(self, transport):
        """ Called when a connection is first made to this projector. """
        self.transport = transport
        if self.debug:
            peer = self.transport.get_extra_info('peername')
            print("PJLinkServerProtocol CONNECTION_MADE:", peer)
        if self._opening_message:
            self._write(self._opening_message)

    def connection_lost(self, exc):
        """ Called when the connection is lost or closed. """
        if self.debug:
            peer = self.transport.get_extra_info('peername')
            print("PJLinkServerProtocol CONNECTION_LOST:", peer)

    def data_received(self, data):
        """ Called when the projector recieves data from the client. """
        if self.debug:
            print("PJLinkServerProtocol RECV:", data)

        # Buffer up writes until we get a terminator.
        self._recv_buffer += data
        if self._recv_buffer.endswith(b'\r'):

            # Handle bad test case programming.
            if not len(self._expected_events):
                raise NoExpectations('mock projector got a message before it was told to expect data')

            # Pop the next expected event off.
            expected = self._expected_events.pop(0)

            # If the client sent an _unexpected_ message, flag it as incorrect
            # then save the contents of the buffer, and close the connection.
            if self._recv_buffer != expected.incoming:
                if self.debug:
                    print("PJLinkServerProtocol UNEXPECTED:", self._recv_buffer, 'expected', expected.incoming)
                expected.recv_buffer_contents = self._recv_buffer[:]
                self.loop.call_soon_threadsafe(expected.set)
                self._recv_buffer = b''
                self.transport.close()

            # If the client sent an _expected_ message, flag it as correct,
            # save the buffer contents, and then reply with the expected
            # response.
            else:
                expected.recv_buffer_contents = self._recv_buffer[:]
                self.loop.call_soon_threadsafe(expected.set)
                self.transport.write(expected.respond_with)
                self._recv_buffer = b''

    def open_and_send(self, message):
        """ Set the message to send to the client when a connection is first established. """
        self._opening_message = message

    @contextlib.asynccontextmanager
    async def when(self, incoming, respond_with, within=1):
        """
        When the projector recieves an incoming message, it should respond with
        a reply within n seconds.

        If the projector recieves nothing from the client, raise `NoClientMessage`.
        If the projector recieves a different message, raise `UnexpectedClientMessage`.

        Other exceptions are passed through for handling by the test case.
        """
        # Define the expected incoming message and response.
        expected = ExpectedEvent(incoming, respond_with, within=within)
        self._expected_events.append(expected)

        # Give the event back to the application.
        try:
            yield expected
            await asyncio.wait_for(expected.wait(), timeout=expected.timeout)

        # Our mock protocol closes the server if it finds an error.
        # This results in the client seeing a disconnection (it doesn't know its mocked)
        # so we check the result of our event.

        # Handle reasons our tests might not be written correctly.
        except asyncio.exceptions.TimeoutError:
            # print("⌚ probably no message from client")
            if not expected.recv_buffer_contents:
                raise NoClientMessage(f'projector recieved no data from client (within={expected.timeout}s)')

        except aiopjlink.PJLinkConnectionClosed:
            # print("🚌 probably transport closed on purpose after bad message")
            # Check the object state to see if it got a bad message.
            if expected.recv_buffer_contents != expected.incoming:
                raise UnexpectedClientMessage('projector expected {!r} from the client but got {!r}'.format(expected.incoming, expected.recv_buffer_contents)) # noqa

        # Ensure the event is removed.
        # Handles the edge case where the client API raises an exception before transmission.
        finally:
            # # Check the object state to see if it ever got a message from the client.
            # print("📋 expected.incoming", expected.incoming)
            # print("📋 expected.recv_buffer_contents", expected.recv_buffer_contents)

            if expected in self._expected_events:
                self._expected_events.remove(expected)


class ExpectedEvent(asyncio.Event):
    """
    Specalised event that controls what the mocked projector
    does next and validates that it behaves in the expected way.
    """
    def __init__(self, incoming, respond_with, within):
        super().__init__()
        # What the projector expects to recieve from the client.
        self.incoming = incoming

        # What should the projector respond with when it receives the expected
        # message from the client. NOTE: If it doesn't get the expected message
        # it hangs up the connection.  In this way, simulated projector errors
        # need to be implemented by the test cases.
        self.respond_with = respond_with

        # How long does the client have to do its thing.
        self.timeout = within

        # Contents of the projector recieve buffer after processing.
        self.recv_buffer_contents = None


@contextlib.asynccontextmanager
async def mock_tcp_pjlink(host='127.0.0.1', port=4352, password=None):
    loop = asyncio.get_running_loop()
    protocol = PJLinkServerProtocol(loop=loop)
    server = await loop.create_server(lambda: protocol, host, port)
    try:
        server_task = loop.create_task(server.serve_forever())
        yield protocol
    finally:
        server_task.cancel()


@contextlib.asynccontextmanager
async def mock_client_server_noauth():
    async with mock_tcp_pjlink() as server:
        server.open_and_send(b'PJLINK 0\r')
        async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:
            yield server, client


class ReflectiveMockTests(unittest.IsolatedAsyncioTestCase):
    """ Validate the test case framework. """

    async def test_mock_nonresponse(self):
        """
        The test framework notices that the client has not issued the expected command.
        It times out with a `NoClientMessage` and closes the server.
        """
        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')

            # Expect a POWR status request.
            with self.assertRaises(NoClientMessage):
                async with server.when('%1POWR ?', respond_with=b'%1POWR=0\r'):
                    # Never give one.
                    pass

    async def test_mock_unexpectedresponse(self):
        """
        The test framework notices that the client has issued an "incorrect" command
        to the one that was expected (in this case, during auth).
        It catches this error, closes the server, and issues an `UnexpectedClientMessage`.

        This helps us check the tests are written correctly without super complex error messages.
        """
        # Start server with auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 1 21d0e96e\r')

            # Expect an unexpected message (valid test behaviour).
            with self.assertRaises(UnexpectedClientMessage):

                # Compute what the projector expects to see if the password was actually "ABC123"
                # with the above salt token (21d0e96e).
                salted_password = hashlib.md5('21d0e96eABC123'.encode()).hexdigest().encode()
                expected_cmd = bytes(salted_password)+b'%1POWR ?\r'

                # Tell the test framework to raise an UnexpectedClientMessage
                # if we get a different password sent from the client.  In this case, that is
                # what we want (not an auth error) because we are testing our ability to mock
                # a projector and NOT the behaviour of the projector.
                async with server.when(expected_cmd, respond_with=b'%1POWR=0\r'):

                    # Give a junk password to trigger the UnexpectedClientMessage.
                    async with aiopjlink.PJLink(address='127.0.0.1', password='INCORRECT'):
                        pass

    async def test_mock_unexpectedresponse_after_connection(self):
        """
        The test framework allows: (1) successful messages to be handled OK, (2) successive
        successful messages, and (3) catches errors in successive unexpected messages.

        This ensures the test framework is suitable for multiple calls (e.g. internal buffers
        are reset, and that sort of thing).
        """
        # Open a connection successfully with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')
            async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:

                # Check that the test framework recieves the expected message.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):
                    value = await client.transmit('POWR', '?', pjclass=aiopjlink.PJClass.ONE)
                    self.assertEqual(value, '0')

                # Check that the test framework recieves the expected message - 2nd time.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):
                    value = await client.transmit('POWR', '?', pjclass=aiopjlink.PJClass.ONE)
                    self.assertEqual(value, '0')

                # Check that the test framework handles the mistake in the test code.
                with self.assertRaises(UnexpectedClientMessage):
                    async with server.when(b'SOME_MESSAGE\r', respond_with=b'%1POWR=0\r'):
                        await client.transmit('POWR', '?', pjclass=aiopjlink.PJClass.ONE)

    async def test_mock_response_stacking(self):
        """ The test framework allows expected responses to be queued.
        """
        # Open a connection successfully with no auth.
        async with mock_client_server_noauth() as (server, client):

            # Expect a POWR followed by a CLSS.
            async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):
                async with server.when(b'%1CLSS ?\r', respond_with=b'%1CLSS=2\r'):
                    value = await client.transmit('POWR', '?', pjclass=aiopjlink.PJClass.ONE)
                    self.assertEqual(value, '0')
                    value = await client.transmit('CLSS', '?', pjclass=aiopjlink.PJClass.ONE)
                    self.assertEqual(value, '2')

            # Expect a POWR followed by a CLSS - but don't get it, so we should
            # expect a warning about the test case: UnexpectedClientMessage
            with self.assertRaises(UnexpectedClientMessage):
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):
                    async with server.when(b'%1CLSS ?\r', respond_with=b'%1CLSS=2\r'):
                        await client.transmit('CLSS', '?', pjclass=aiopjlink.PJClass.ONE)
                        await client.transmit('POWR', '?', pjclass=aiopjlink.PJClass.ONE)


class AuthTests(unittest.IsolatedAsyncioTestCase):
    """ PJLink authentication behaves as expected.
    """

    async def test_auth_none(self):
        """ Tests a connection with no authentication. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')

            # Prime the server to handle a power request.
            async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):

                # Send the power request and check the response is expected.
                async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:
                    value = await client.transmit('POWR', '?', pjclass='1')
                    self.assertEqual(value, '0')

    async def test_auth_malformed(self):
        """ Tests the projector sending back a malformed welcome message generates a `PJLinkProtocolError`. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK XXX\r')

            # Expect to see a protocol error when we connect.
            with self.assertRaises(aiopjlink.PJLinkProtocolError):
                async with aiopjlink.PJLink(address='127.0.0.1', password=None):
                    pass

    async def test_auth_no_welcome(self):
        """ Tests the projector not sending a welcome message generates a `PJLinkProtocolError` """

        # Start server with no auth.
        async with mock_tcp_pjlink():

            # Do not send a message (i.e. the one commented out below).
            # server.open_and_send(b'PJLINK XXX\r')

            # Expect to see a protocol error when we connect.
            with self.assertRaises(aiopjlink.PJLinkProtocolError):
                async with aiopjlink.PJLink(address='127.0.0.1', password=None, timeout=0.5):
                    pass

    async def test_auth_no_server(self):
        """ Tests that the client honours the timeout if no server responds to the connection. """

        # Expect to see a PJLinkNoConnection when we connect.
        # CONDITION 1: The host can be reached by the OS but no response (aiotimeout).
        with self.assertRaises(aiopjlink.PJLinkNoConnection) as err:
            async with aiopjlink.PJLink(address='127.0.0.1', password=None, timeout=0.5):
                pass
        self.assertEqual(str(err.exception), 'timeout - projector did not accept the connection in time')

        # CONDITION 2: The host cannot be reached by the OS.
        with self.assertRaises(aiopjlink.PJLinkNoConnection) as err:
            async with aiopjlink.PJLink(address='0.0.0.0', password=None, timeout=0.5):
                pass
        self.assertIn('os timeout', str(err.exception))

    async def test_auth_valid_pw(self):
        """ Tests a connection with valid authentication. """

        # Server sends the auth challenge: token='21d0e96e'; password='abc123'
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 1 21d0e96e\r')

            # Calcuate the expected projector behaviour.
            # NOTE: Our client implementation always sends a `POWR ?` since that is
            # a commonly implemented command.
            salted_password = hashlib.md5('21d0e96eabc123'.encode()).hexdigest().encode()
            cmd = bytes(salted_password)+b'%1POWR ?\r'
            async with server.when(cmd, respond_with=b'%1POWR=0\r'):

                # Send the power request and check the response is expected.
                async with aiopjlink.PJLink(address='127.0.0.1', password='abc123'):
                    pass

    async def test_auth_invalid_pw(self):
        """ Tests a connection with invalid authentication. """

        # Server sends the auth challenge: token='21d0e96e'; password='abc123'
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 1 21d0e96e\r')

            # Calcuate the expected projector behaviour.
            salted_password = hashlib.md5('21d0e96eINVALIDPW'.encode()).hexdigest().encode()
            cmd = bytes(salted_password)+b'%1POWR ?\r'
            async with server.when(cmd, respond_with=b'PJLINK ERRA\r'):

                # Send the power request and check the response is expected.
                with self.assertRaises(aiopjlink.PJLinkPassword):
                    async with aiopjlink.PJLink(address='127.0.0.1', password='INVALIDPW'):
                        pass


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    """ PJLink protocol managment behaves as expected.
    """

    async def test_response_parsing_high_level(self):
        """ Check that responses are handled correctly by the client. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')
            async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:

                # Unxpected command sent as a result of a request.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1ROWP=A\r'):
                    with self.assertRaises(aiopjlink.PJLinkProtocolError):
                        await client.power.get()

    async def test_command_construction(self):
        """ Test that command formatting accepts valid values and raises errors if out of spec. """
        # Normal commands.
        result = aiopjlink.PJLink._format_command('ABCD', '???', pjclass='1')
        self.assertEqual(result, '%1ABCD ???\r')

        result = aiopjlink.PJLink._format_command('EFGH', '1', pjclass='1')
        self.assertEqual(result, '%1EFGH 1\r')

        result = aiopjlink.PJLink._format_command('IJKL', '9999999', pjclass='1')
        self.assertEqual(result, '%1IJKL 9999999\r')

        result = aiopjlink.PJLink._format_command('ABCD', '?', pjclass='2')
        self.assertEqual(result, '%2ABCD ?\r')

        # Bad commands.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            result = aiopjlink.PJLink._format_command('abcd', '?', pjclass='1')
        self.assertEqual(str(err.exception), 'command is not uppercase')

        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            result = aiopjlink.PJLink._format_command('ABCDE', '?', pjclass='1')
        self.assertEqual(str(err.exception), 'command is not 4 bytes')

        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            large_param = 'X' * 129
            result = aiopjlink.PJLink._format_command('ABCD', large_param, pjclass='1')
        self.assertEqual(str(err.exception), 'command param is larger than 128 bytes')

        with self.assertRaises(ValueError) as err:
            result = aiopjlink.PJLink._format_command('ABCD', '?', pjclass='3')
        self.assertEqual(str(err.exception), '\'3\' is not a valid PJClass')

    async def test_response_parsing(self):
        """ Ensure that errors generate the correct responses. """

        # Valid data.
        command, parameter = aiopjlink.PJLink._parse_response(
            data='%1ABCD=5\r',
            expect_command='ABCD',
            expect_pjclass='1')
        self.assertEqual(command, 'ABCD')
        self.assertEqual(parameter, '5')

        # Bad response header.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='#1ABCD=5\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response header')

        # Bad class version.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='%2ABCD=5\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response protocol class')

        # Bad class version.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='%2ABCD=5\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response protocol class')

        # Bad separator.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD/5\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response separator')

        # Bad command length.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCDE/5\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response separator')

        # Bad command length.
        with self.assertRaises(aiopjlink.PJLinkProtocolError) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD=5\r',
                expect_command='XXXX',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unexpected response command')

        # ERR1 - unsupported command
        with self.assertRaises(aiopjlink.PJLinkERR1) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD=ERR1\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unsupported command')

        # ERR2 - out of parameter
        with self.assertRaises(aiopjlink.PJLinkERR2) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD=ERR2\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'out of parameter')

        # NOTE: The following are interpreted as `PJLinkProjectorError`
        # ERR3 - unavailable in the current state
        with self.assertRaises(aiopjlink.PJLinkERR3) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD=ERR3\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'unavailable in the current state')

        # ERR4 - projector or display failure
        with self.assertRaises(aiopjlink.PJLinkERR4) as err:
            aiopjlink.PJLink._parse_response(
                data='%1ABCD=ERR4\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'projector or display failure')

        # Check error parsing is case insensitive (from projector) and error
        # parsing is handled in the same way too (Postel's law).
        with self.assertRaises(aiopjlink.PJLinkERR4) as err:
            aiopjlink.PJLink._parse_response(
                data='%1abcd=err4\r',
                expect_command='ABCD',
                expect_pjclass='1')
        self.assertEqual(str(err.exception), 'projector or display failure')

        # Check that a respone does not fail if there is a CR right after
        # an equals (e.g. §4.12 of the spec)
        command, param = aiopjlink.PJLink._parse_response(
            data='%1INF2=\r',
            expect_command='INF2',
            expect_pjclass='1')
        self.assertEqual(command, 'INF2')
        self.assertEqual(param, '')


class PowerGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink power control behaves as expected. """

    async def test_state_enum_coercion(self):
        """ Do the enums behave logically. """
        # Power commands.
        self.assertEqual(bool(aiopjlink.Power.ON), True)
        self.assertEqual(bool(aiopjlink.Power.OFF), False)
        self.assertTrue(aiopjlink.Power.ON)
        self.assertFalse(aiopjlink.Power.OFF)

        # Power status (truthy).
        self.assertTrue(aiopjlink.Power.State.ON)
        self.assertTrue(aiopjlink.Power.State.WARMING)

        # Power status (falsy).
        self.assertFalse(aiopjlink.Power.State.COOLING)
        self.assertFalse(aiopjlink.Power.State.OFF)

    async def test_power_get(self):
        """ Get power status. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')
            async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:

                # Power off.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=0\r'):
                    status = await client.power.get()
                    self.assertEqual(status, aiopjlink.Power.OFF)

                # Power on.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=1\r'):
                    status = await client.power.get()
                    self.assertEqual(status, aiopjlink.Power.ON)

                # Power cooling.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=2\r'):
                    status = await client.power.get()
                    self.assertEqual(status, aiopjlink.Power.State.COOLING)

                # Power warming.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=3\r'):
                    status = await client.power.get()
                    self.assertEqual(status, aiopjlink.Power.State.WARMING)

                # Unxpected power result.
                async with server.when(b'%1POWR ?\r', respond_with=b'%1POWR=A\r'):
                    with self.assertRaises(ValueError):
                        await client.power.get()

    async def test_power_set(self):
        """ Set power status. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')
            async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:

                # Power ON
                async with server.when(b'%1POWR 1\r', respond_with=b'%1POWR=OK\r'):
                    await client.power.set(aiopjlink.Power.ON)

                # Power OFF
                async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=OK\r'):
                    await client.power.set(aiopjlink.Power.OFF)

                # Power ON
                async with server.when(b'%2POWR 1\r', respond_with=b'%2POWR=OK\r'):
                    await client.power.set(aiopjlink.Power.ON, pjclass=aiopjlink.PJLink.C2)

                # Power OFF
                async with server.when(b'%2POWR 0\r', respond_with=b'%2POWR=OK\r'):
                    await client.power.set(aiopjlink.Power.OFF, pjclass=aiopjlink.PJLink.C2)

                # Power set out of parameter (test duplicated in test_response_parsing)
                with self.assertRaises(aiopjlink.PJLinkERR2) as err:
                    async with server.when(b'%1POWR 3\r', respond_with=b'%1POWR=ERR2\r'):
                        await client.transmit('POWR', '3', pjclass=aiopjlink.PJLink.C1)
                self.assertEqual(str(err.exception), 'out of parameter')

                # Power set unavailable (test duplicated in test_response_parsing)
                with self.assertRaises(aiopjlink.PJLinkERR3) as err:
                    async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=ERR3\r'):
                        await client.power.set(client.power.OFF)
                self.assertEqual(str(err.exception), 'unavailable in the current state')

                # Power set not possible (test duplicated in test_response_parsing)
                with self.assertRaises(aiopjlink.PJLinkERR4) as err:
                    async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=ERR4\r'):
                        await client.power.set(client.power.OFF)
                self.assertEqual(str(err.exception), 'projector or display failure')

                # Unexpected projector reply to a sensible message.
                with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                    async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=UGH\r'):
                        await client.power.set(aiopjlink.Power.OFF)
                self.assertEqual(str(err.exception), 'expected OK response')

                # Expect enums.
                with self.assertRaises(ValueError) as err:
                    async with server.when(b'%1POWR 1\r', respond_with=b'%1POWR=OK\r'):
                        await client.power.set(True)
                self.assertEqual(str(err.exception), 'True is not a valid Power.State')

                with self.assertRaises(ValueError) as err:
                    async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=OK\r'):
                        await client.power.set('off')
                self.assertEqual(str(err.exception), '\'off\' is not a valid Power.State')

                with self.assertRaises(ValueError) as err:
                    async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=OK\r'):
                        await client.power.set(aiopjlink.Power.State.COOLING)
                self.assertEqual(str(err.exception), 'expected Power.State.ON or Power.State.OFF')

                # Accept PJLink strings as enum values.
                async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=OK\r'):
                    await client.power.set('0')

    async def test_power_shortcuts(self):
        """ Set power status. """

        # Start server with no auth.
        async with mock_tcp_pjlink() as server:
            server.open_and_send(b'PJLINK 0\r')
            async with aiopjlink.PJLink(address='127.0.0.1', password=None) as client:

                # Power ON
                async with server.when(b'%1POWR 1\r', respond_with=b'%1POWR=OK\r'):
                    await client.power.turn_on()

                # Power OFF
                async with server.when(b'%1POWR 0\r', respond_with=b'%1POWR=OK\r'):
                    await client.power.turn_off()


class SourcesGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink input source control and enumeration behaves as expected. """

    async def test_enum(self):
        """ Ensure the enumeration matches the spec. """
        self.assertEqual(aiopjlink.Sources.Mode.RGB.value, '1')
        self.assertEqual(aiopjlink.Sources.Mode.VIDEO.value, '2')
        self.assertEqual(aiopjlink.Sources.Mode.DIGITAL.value, '3')
        self.assertEqual(aiopjlink.Sources.Mode.STORAGE.value, '4')
        self.assertEqual(aiopjlink.Sources.Mode.NETWORK.value, '5')
        self.assertEqual(aiopjlink.Sources.Mode.INTERNAL.value, '6')

    async def test_inpt(self):
        """ Check the INPT instructions work. """
        async with mock_client_server_noauth() as (server, client):

            # Get current source.
            async with server.when(b'%1INPT ?\r', respond_with=b'%1INPT=31\r'):
                mode, index = await client.sources.get()
                self.assertEqual(mode, client.sources.Mode.DIGITAL)
                self.assertEqual(index, '1')

            # Set current source.
            async with server.when(b'%1INPT 21\r', respond_with=b'%1INPT=OK\r'):
                await client.sources.set(aiopjlink.Sources.Mode.VIDEO, '1')

    async def test_inst_class1(self):
        """ Test that available sources can be enumerated. """
        async with mock_client_server_noauth() as (server, client):

            # List available clients
            async with server.when(b'%1INST ?\r', respond_with=b'%1INST=11 31 32 41 52 56\r'):
                sources = await client.sources.available()

                # Length
                self.assertEqual(len(sources), 6)

                # RGB 1
                mode, index = sources[0]
                self.assertEqual(mode, aiopjlink.Sources.Mode.RGB)
                self.assertEqual(index, '1')

                # DIGITAL 1
                mode, index = sources[1]
                self.assertEqual(mode, aiopjlink.Sources.Mode.DIGITAL)
                self.assertEqual(index, '1')

                # DIGITAL 2
                mode, index = sources[2]
                self.assertEqual(mode, aiopjlink.Sources.Mode.DIGITAL)
                self.assertEqual(index, '2')

                # STORAGE 1
                mode, index = sources[3]
                self.assertEqual(mode, aiopjlink.Sources.Mode.STORAGE)
                self.assertEqual(index, '1')

                # NETWORK 2
                mode, index = sources[4]
                self.assertEqual(mode, aiopjlink.Sources.Mode.NETWORK)
                self.assertEqual(index, '2')

                # NETWORK 6
                mode, index = sources[5]
                self.assertEqual(mode, aiopjlink.Sources.Mode.NETWORK)
                self.assertEqual(index, '6')

    async def test_inst_innm_class2(self):
        """ Test that available sources and their names can be enumerated. """
        async with mock_client_server_noauth() as (server, client):

            # List available clients (Class 2) - brief as the Class 1 shares the logic.
            async with server.when(b'%2INST ?\r', respond_with=b'%2INST=11 31 32 41 52 56\r'):
                sources = await client.sources.available(pjclass=aiopjlink.PJClass.TWO)
                self.assertEqual(len(sources), 6)

            # Get the names of a display source (values taken from actual projector output).
            async with server.when(b'%2INNM ?31\r', respond_with=b'%2INNM=DVI-D\r'):
                name = await client.sources.get_source_name(aiopjlink.Sources.Mode.DIGITAL, '1')
                self.assertEqual(name, 'DVI-D')

            # Accept integers (because we are liberal in what we accept).
            async with server.when(b'%2INNM ?31\r', respond_with=b'%2INNM=DVI-D\r'):
                name = await client.sources.get_source_name(aiopjlink.Sources.Mode.DIGITAL, 1)
                self.assertEqual(name, 'DVI-D')

            # Reject long numbers (before hitting the server).
            with self.assertRaises(ValueError) as err:
                name = await client.sources.get_source_name(aiopjlink.Sources.Mode.DIGITAL, 11)
            self.assertEqual(
                str(err.exception),
                'index must be a single character (1-9 for Class 1, and 1-9A-Z for Class 2)'
            )

            # Get the names of available.
            async with server.when(b'%2INST ?\r', respond_with=b'%2INST=11\r'):
                async with server.when(b'%2INNM ?11\r', respond_with=b'%2INNM=Computer\r'):
                    sources = await client.sources.available_with_names()
                    self.assertEqual(len(sources), 1)
                    mode, index, name = sources[0]
                    self.assertEqual(mode, aiopjlink.Sources.Mode.RGB)
                    self.assertEqual(index, '1')
                    self.assertEqual(name, 'Computer')

    async def test_ires(self):
        """ Test that the resolution of the current input can be recieved. """
        async with mock_client_server_noauth() as (server, client):

            # Get resolution.
            async with server.when(b'%2IRES ?\r', respond_with=b'%2IRES=100x200\r'):
                x, y = await client.sources.resolution()
                self.assertEqual(x, 100)
                self.assertEqual(y, 200)

            # Handle spec edge cases.
            with self.assertRaises(aiopjlink.PJLinkProjectorError) as err:
                async with server.when(b'%2IRES ?\r', respond_with=b'%2IRES=-\r'):
                    await client.sources.resolution()
            self.assertEqual(str(err.exception), 'no signal input')

            # Handle spec edge cases.
            with self.assertRaises(aiopjlink.PJLinkProjectorError) as err:
                async with server.when(b'%2IRES ?\r', respond_with=b'%2IRES=*\r'):
                    await client.sources.resolution()
            self.assertEqual(str(err.exception), 'unknown signal')

            # Bad response from the projecetor.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%2IRES ?\r', respond_with=b'%2IRES=\r'):
                    x, y = await client.sources.resolution()
            self.assertEqual(str(err.exception), 'unable to parse resolution')

    async def test_rres(self):
        """ Test that the recommended resolution for the current input can be recieved. """
        async with mock_client_server_noauth() as (server, client):

            # Get resolution.
            async with server.when(b'%2RRES ?\r', respond_with=b'%2RRES=1920x1080\r'):
                x, y = await client.sources.recommended_resolution()
                self.assertEqual(x, 1920)
                self.assertEqual(y, 1080)

            # Bad response from the projecetor.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%2RRES ?\r', respond_with=b'%2RRES=\r'):
                    x, y = await client.sources.recommended_resolution()
            self.assertEqual(str(err.exception), 'unable to parse resolution')


class MuteGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink mute controls behave as expected. """

    async def test_status(self):
        """ Mute status is acquired OK. """
        async with mock_client_server_noauth() as (server, client):

            # Video only muted.
            async with server.when(b'%1AVMT ?\r', respond_with=b'%1AVMT=11\r'):
                video, audio = await client.mute.status()
                self.assertEqual(video, True)
                self.assertEqual(audio, False)

            # Audio only muted.
            async with server.when(b'%1AVMT ?\r', respond_with=b'%1AVMT=21\r'):
                video, audio = await client.mute.status()
                self.assertEqual(video, False)
                self.assertEqual(audio, True)

            # Nothing muted.
            async with server.when(b'%1AVMT ?\r', respond_with=b'%1AVMT=30\r'):
                video, audio = await client.mute.status()
                self.assertEqual(video, False)
                self.assertEqual(audio, False)

            # Both audio and video muted.
            async with server.when(b'%1AVMT ?\r', respond_with=b'%1AVMT=31\r'):
                video, audio = await client.mute.status()
                self.assertEqual(video, True)
                self.assertEqual(audio, True)

            # Bad information from projector.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1AVMT ?\r', respond_with=b'%1AVMT=41\r'):
                    video, audio = await client.mute.status()
            self.assertEqual(str(err.exception), 'unexpected mute response')

    async def test_control_api(self):
        """ Mute status can be set OK using expressive methods. """
        async with mock_client_server_noauth() as (server, client):

            # VIDEO
            # %1AVMT 11	%1AVMT=OK	blanking on
            async with server.when(b'%1AVMT 11\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.video(True)
            # %1AVMT 10	%1AVMT=OK	blanking off
            async with server.when(b'%1AVMT 10\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.video(False)

            # AUDIO
            # %1AVMT 21	%1AVMT=OK	audio muting on
            async with server.when(b'%1AVMT 21\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.audio(True)
            # %1AVMT 20	%1AVMT=OK	audio muting off
            async with server.when(b'%1AVMT 20\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.audio(False)

            # BOTH AT ONCE
            # %1AVMT 31	%1AVMT=OK	blanking on and audio muting on
            async with server.when(b'%1AVMT 31\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.both(True)

            # %1AVMT 30	%1AVMT=OK	blanking on and audio muting off
            async with server.when(b'%1AVMT 30\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.both(False)

    async def test_set(self):
        """ Mute status can be set OK using the shortcut set method. """
        async with mock_client_server_noauth() as (server, client):

            # Fully specified (3x condition).
            async with server.when(b'%1AVMT 31\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(True, True)
            async with server.when(b'%1AVMT 30\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(False, False)

            # Fully specified (2x instructions)
            async with server.when(b'%1AVMT 11\r', respond_with=b'%1AVMT=OK\r'):
                async with server.when(b'%1AVMT 20\r', respond_with=b'%1AVMT=OK\r'):
                    await client.mute.set(True, False)

            async with server.when(b'%1AVMT 10\r', respond_with=b'%1AVMT=OK\r'):
                async with server.when(b'%1AVMT 21\r', respond_with=b'%1AVMT=OK\r'):
                    await client.mute.set(False, True)

            # Partially specified (for audio) (1x instruction)
            async with server.when(b'%1AVMT 21\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(video=None, audio=True)
            async with server.when(b'%1AVMT 20\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(video=None, audio=False)

            # Partially specified (for video) (1x instruction)
            async with server.when(b'%1AVMT 11\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(video=True, audio=None)
            async with server.when(b'%1AVMT 10\r', respond_with=b'%1AVMT=OK\r'):
                await client.mute.set(video=False, audio=None)

            # Nothing specified - so no messages sent.
            with self.assertRaises(NoClientMessage):
                async with server.when(b'%1XXXX 11\r', respond_with=b'NOTHING\r'):
                    await client.mute.set(None, None)


class ErrorGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink error reporting behaves as expected. """

    async def test_query(self):
        """ Error status parses results correctly. """
        async with mock_client_server_noauth() as (server, client):

            # No errors!
            async with server.when(b'%1ERST ?\r', respond_with=b'%1ERST=000000\r'):
                errors = await client.errors.query()
                self.assertEqual(errors, {
                    aiopjlink.Errors.Category.FAN: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.LAMP: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.TEMP: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.COVER: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.FILTER: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.OTHER: aiopjlink.Errors.Level.OK,
                })

            # Two warnings and one failure.
            async with server.when(b'%1ERST ?\r', respond_with=b'%1ERST=101020\r'):
                errors = await client.errors.query()
                self.assertEqual(errors, {
                    aiopjlink.Errors.Category.FAN: aiopjlink.Errors.Level.WARN,
                    aiopjlink.Errors.Category.LAMP: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.TEMP: aiopjlink.Errors.Level.WARN,
                    aiopjlink.Errors.Category.COVER: aiopjlink.Errors.Level.OK,
                    aiopjlink.Errors.Category.FILTER: aiopjlink.Errors.Level.ERROR,
                    aiopjlink.Errors.Category.OTHER: aiopjlink.Errors.Level.OK,
                })

            # Bad error from the projector - too many errors.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1ERST ?\r', respond_with=b'%1ERST=0000000\r'):
                    errors = await client.errors.query()
            self.assertEqual(str(err.exception), 'unexpected number of error types reported')

            # Bad error from the projector - unknown error type.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1ERST ?\r', respond_with=b'%1ERST=003000\r'):
                    errors = await client.errors.query()
            self.assertEqual(str(err.exception), 'unknown error level')


class FilterGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink filter reports correctly. """

    async def test_hours(self):
        """ Get the correct filter hours. """
        async with mock_client_server_noauth() as (server, client):

            # Valid filter.
            async with server.when(b'%2FILT ?\r', respond_with=b'%2FILT=100\r'):
                hours = await client.filter.hours()
                self.assertEqual(hours, 100)

            # No filter handled correctly.
            with self.assertRaises(aiopjlink.PJLinkERR1) as err:
                async with server.when(b'%2FILT ?\r', respond_with=b'%2FILT=ERR1\r'):
                    hours = await client.filter.hours()
            self.assertEqual(str(err.exception), 'no filter')

    async def test_replacement_model(self):
        """ Get a list of replacement filter models. """
        async with mock_client_server_noauth() as (server, client):

            # Once replacement.
            async with server.when(b'%2RFIL ?\r', respond_with=b'%2RFIL=SampleFilter\r'):
                models = await client.filter.replacement_models()
                self.assertEqual(len(models), 1)
                self.assertEqual(models[0], 'SampleFilter')

            # Two replacements.
            async with server.when(b'%2RFIL ?\r', respond_with=b'%2RFIL=SampleFilter ELPAF46\r'):
                models = await client.filter.replacement_models()
                self.assertEqual(len(models), 2)
                self.assertEqual(models[0], 'SampleFilter')
                self.assertEqual(models[1], 'ELPAF46')

            # No replacements.
            async with server.when(b'%2RFIL ?\r', respond_with=b'%2RFIL=\r'):
                models = await client.filter.replacement_models()
                self.assertEqual(len(models), 0)


class LampGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink lamps report correctly. """

    async def test_status(self):
        """ Lamp status parses correctly for common values. """
        async with mock_client_server_noauth() as (server, client):

            # Two lamps (from PJLink test excel sheet)
            async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=8253 1 13442 1\r'):
                lamps = await client.lamps.status()
                self.assertEqual(len(lamps), 2)

                hours, state = lamps[0]
                self.assertEqual(hours, 8253)
                self.assertEqual(state, aiopjlink.Lamp.State.ON)

                hours, state = lamps[1]
                self.assertEqual(hours, 13442)
                self.assertEqual(state, aiopjlink.Lamp.State.ON)

            # One lamp off
            async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=8253 0\r'):
                lamps = await client.lamps.status()
                self.assertEqual(len(lamps), 1)
                hours, state = lamps[0]
                self.assertEqual(hours, 8253)
                self.assertEqual(state, aiopjlink.Lamp.State.OFF)

            # No lamps in the projector.
            with self.assertRaises(aiopjlink.PJLinkERR1):
                async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=ERR1\r'):
                    lamps = await client.lamps.status()

            # Unparsable feedback from projector.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=1 0 10\r'):
                    lamps = await client.lamps.status()
            self.assertEqual(str(err.exception), 'unparsable lamp status')

            # Bad feedback from the projector.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=\r'):
                    lamps = await client.lamps.status()
            self.assertEqual(str(err.exception), 'unparsable lamp status')

            # Bad feedback from the projector.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=100 3\r'):
                    lamps = await client.lamps.status()
            self.assertEqual(str(err.exception), 'unparsable lamp status')

    async def test_hours(self):
        """ The hours accelerator gets the correct lamp hours. """
        async with mock_client_server_noauth() as (server, client):

            # Two lamps (from PJLink test excel sheet)
            async with server.when(b'%1LAMP ?\r', respond_with=b'%1LAMP=8253 1 13442 1\r'):
                hours = await client.lamps.hours()
                self.assertEqual(hours, 8253)

    async def test_replacement_model(self):
        """ Get a list of replacement lamp models. """
        async with mock_client_server_noauth() as (server, client):

            # Once replacement.
            async with server.when(b'%2RLMP ?\r', respond_with=b'%2RLMP=SampleLamp\r'):
                models = await client.lamps.replacement_models()
                self.assertEqual(len(models), 1)
                self.assertEqual(models[0], 'SampleLamp')

            # Two replacements.
            async with server.when(b'%2RLMP ?\r', respond_with=b'%2RLMP=SampleLamp RC11\r'):
                models = await client.lamps.replacement_models()
                self.assertEqual(len(models), 2)
                self.assertEqual(models[0], 'SampleLamp')
                self.assertEqual(models[1], 'RC11')

            # No replacements.
            async with server.when(b'%2RLMP ?\r', respond_with=b'%2RLMP=\r'):
                models = await client.lamps.replacement_models()
                self.assertEqual(len(models), 0)


class FreezeGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink freeze behavour activates and reports correctly. """

    async def test_set(self):
        """ Screen freezes and unfreezes. """
        async with mock_client_server_noauth() as (server, client):

            # Expected.
            async with server.when(b'%2FREZ 1\r', respond_with=b'%2FREZ=OK\r'):
                await client.freeze.set(True)
            async with server.when(b'%2FREZ 0\r', respond_with=b'%2FREZ=OK\r'):
                await client.freeze.set(False)

            # Unexpected.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%2FREZ 1\r', respond_with=b'%2FREZ=UGH\r'):
                    await client.freeze.set(True)
            self.assertEqual(str(err.exception), 'expected OK response')

    async def test_get(self):
        """ Getting the current freeze state. """
        async with mock_client_server_noauth() as (server, client):

            # Expected.
            async with server.when(b'%2FREZ ?\r', respond_with=b'%2FREZ=0\r'):
                self.assertEqual(await client.freeze.get(), False)
            async with server.when(b'%2FREZ ?\r', respond_with=b'%2FREZ=1\r'):
                self.assertEqual(await client.freeze.get(), True)

            # Unexpected.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%2FREZ ?\r', respond_with=b'%2FREZ=2\r'):
                    await client.freeze.get()
            self.assertEqual(str(err.exception), 'unexpected freeze state')


class VolumeGroups(unittest.IsolatedAsyncioTestCase):
    """ PJLink volume increments and decrements correctly. """

    async def test_change_volume(self):
        """ Volume turns up and down for speaker and microphone. """
        async with mock_client_server_noauth() as (server, client):

            # Turn up mic.
            async with server.when(b'%2MVOL 1\r', respond_with=b'%2MVOL=OK\r'):
                await client.microphone.turn_up()

            # Turn up speaker.
            async with server.when(b'%2SVOL 1\r', respond_with=b'%2SVOL=OK\r'):
                await client.speaker.turn_up()

            # Turn down mic.
            async with server.when(b'%2MVOL 0\r', respond_with=b'%2MVOL=OK\r'):
                await client.microphone.turn_down()

            # Turn down speaker.
            async with server.when(b'%2SVOL 0\r', respond_with=b'%2SVOL=OK\r'):
                await client.speaker.turn_down()

            # Unexpected.
            with self.assertRaises(aiopjlink.PJLinkUnexpectedResponseParameter) as err:
                async with server.when(b'%2SVOL 1\r', respond_with=b'%2SVOL=2\r'):
                    await client.speaker.turn_up()
            self.assertEqual(str(err.exception), 'expected OK response')


class InfoGroup(unittest.IsolatedAsyncioTestCase):
    """ PJLink projector information queries. """

    async def test_software_version(self):
        """ Get the software version number string. """
        async with mock_client_server_noauth() as (server, client):

            # Version provided.
            async with server.when(b'%2SVER ?\r', respond_with=b'%2SVER=24011273HQWWV105\r'):
                version = await client.info.software_version()
                self.assertEqual(version, '24011273HQWWV105')

            # No version provided.
            async with server.when(b'%2SVER ?\r', respond_with=b'%2SVER=\r'):
                version = await client.info.software_version()
                self.assertEqual(version, '')

    async def test_serial_number(self):
        """ Get the device serial number string. """
        async with mock_client_server_noauth() as (server, client):

            # Version provided.
            async with server.when(b'%2SNUM ?\r', respond_with=b'%2SNUM=XA3C2400119\r'):
                version = await client.info.serial_number()
                self.assertEqual(version, 'XA3C2400119')

            # No version provided.
            async with server.when(b'%2SNUM ?\r', respond_with=b'%2SNUM=\r'):
                version = await client.info.serial_number()
                self.assertEqual(version, '')

    async def test_pjlink_class(self):
        """ Query the PJLink class support. """
        async with mock_client_server_noauth() as (server, client):

            # Class 1 query (default).
            async with server.when(b'%1CLSS ?\r', respond_with=b'%1CLSS=1\r'):
                self.assertEqual(await client.info.pjlink_class(), aiopjlink.PJClass.ONE)

            # Class 2 support from class 1 query (default).
            async with server.when(b'%1CLSS ?\r', respond_with=b'%1CLSS=2\r'):
                self.assertEqual(await client.info.pjlink_class(), aiopjlink.PJClass.TWO)

            # Class 2 support from class 2 query (default).
            async with server.when(b'%2CLSS ?\r', respond_with=b'%2CLSS=2\r'):
                response = await client.info.pjlink_class(pjclass=aiopjlink.PJClass.TWO)
                self.assertEqual(response, aiopjlink.PJClass.TWO)

    async def test_info_other(self):
        """ Query the "other" info. """
        async with mock_client_server_noauth() as (server, client):

            # Test from spec excel sheet.
            async with server.when(b'%1INFO ?\r', respond_with=b'%1INFO=PJLink\r'):
                self.assertEqual(await client.info.other(), 'PJLink')

            # Test actual projector response.
            async with server.when(b'%1INFO ?\r', respond_with=b'%1INFO=105.105.---\r'):
                self.assertEqual(await client.info.other(), '105.105.---')

            # Test no extra info.
            async with server.when(b'%1INFO ?\r', respond_with=b'%1INFO=\r'):
                self.assertEqual(await client.info.other(), '')

    async def test_product_name(self):
        """ Query the product name information. """
        async with mock_client_server_noauth() as (server, client):

            # Test from actual projector.
            async with server.when(b'%1INF2 ?\r', respond_with=b'%1INF2=EPSON PU1007B/PU1007W\r'):
                self.assertEqual(await client.info.product_name(), 'EPSON PU1007B/PU1007W')

            # Test no extra info.
            async with server.when(b'%1INF2 ?\r', respond_with=b'%1INF2=\r'):
                self.assertEqual(await client.info.product_name(), '')

    async def test_manufacturer_name(self):
        """ Query the manufacturer name information. """
        async with mock_client_server_noauth() as (server, client):

            # Test from actual projector.
            async with server.when(b'%1INF1 ?\r', respond_with=b'%1INF1=EPSON\r'):
                self.assertEqual(await client.info.manufacturer_name(), 'EPSON')

            # Test no extra info.
            async with server.when(b'%1INF1 ?\r', respond_with=b'%1INF1=\r'):
                self.assertEqual(await client.info.manufacturer_name(), '')

    async def test_projector_name(self):
        """ Query the projector name information. """
        async with mock_client_server_noauth() as (server, client):

            # Test from actual projector.
            async with server.when(b'%1NAME ?\r', respond_with=b'%1NAME=EBB13648\r'):
                self.assertEqual(await client.info.projector_name(), 'EBB13648')

            # Test no extra info.
            async with server.when(b'%1NAME ?\r', respond_with=b'%1NAME=\r'):
                self.assertEqual(await client.info.projector_name(), '')
